"""Step 10.2.2 Layer C —— 本地 fake WeCom API 全链 E2E（不连真实 WeCom）。

    WeCom webhook（合法 wire，Layer 3 同底座）
        ↓ WeComAdapter（入站）
    InboundInteraction
        ↓ RuntimeCoordinator（既有六段，零改动）
    OutboundResponse
        ↓ DeliveryExecutor
        ↓ WeComAdapter.deliver（出站，双 Port 同实例）
    Fake WeCom API（gettoken + message/send）

覆盖：
    ✓ Runtime → Delivery → WeComAdapter → Fake WeCom API 全链成功
    ✓ recipient / agentid / content 映射在真实 HTTP 上验证
    ✓ 每次交付独立获取 access_token（无缓存，10.2.2 范围控制的正验证）
    ✓ errcode!=0 → ChannelDeliveryFailedError（processing SUCCESS ≠ delivery）
    ✓ outbound 对象零修改
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.channels.wecom import (
    WeComAdapter,
    compute_signature,
    encrypt_message,
)
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.channel_inbound import ChannelInboundService
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.errors import ChannelDeliveryFailedError
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import PersistingCoordinator
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.application.worker import Worker
from app.api.runtime.channels import build_channel_inbound_api
from app.domain.agent import Agent
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

TOKEN = "e2e-token"
AES_KEY = "d" * 43
CORP_ID = "wwcorp-e2e2"
CORPSECRET = "corp-secret-e2e2"
AGENTID = 1000002

TEXT_XML = (
    "<xml><ToUserName><![CDATA[wwcorp-e2e2]]></ToUserName>"
    "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
    "<CreateTime>1348831860</CreateTime>"
    "<MsgType><![CDATA[text]]></MsgType>"
    "<Content><![CDATA[请总结今天的会话要点]]></Content>"
    "<MsgId>2001</MsgId>"
    "<AgentID>1</AgentID></xml>"
)


# ================================================================== Fake WeCom API


class _FakeWeComServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.token_requests: list[dict] = []
        self.send_requests: list[dict] = []
        self.send_errcode: int | None = None      # 编程失败模式
        self.access_token = "fake-access-token-1"
        host, port = self.server_address[:2]
        self.api_base = f"http://{host}:{port}"


class _FakeWeComHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802  # /cgi-bin/gettoken
        srv = self.server
        q = parse_qs(urlsplit(self.path).query)
        srv.token_requests.append({"corpid": q.get("corpid", [""])[0],
                                   "corpsecret": q.get("corpsecret", [""])[0]})
        if q.get("corpid", [""])[0] != CORP_ID or \
                q.get("corpsecret", [""])[0] != CORPSECRET:
            self._send(200, {"errcode": 40013, "errmsg": "invalid corpid"})
            return
        self._send(200, {"errcode": 0, "errmsg": "ok",
                         "access_token": srv.access_token, "expires_in": 7200})

    def do_POST(self) -> None:  # noqa: N802  # /cgi-bin/message/send
        srv = self.server
        q = parse_qs(urlsplit(self.path).query)
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"errcode": -1, "errmsg": "bad json"})
            return
        srv.send_requests.append({"access_token": q.get("access_token", [""])[0],
                                  "payload": payload})
        if srv.send_errcode is not None:
            self._send(200, {"errcode": srv.send_errcode,
                             "errmsg": "fake business failure"})
            return
        self._send(200, {"errcode": 0, "errmsg": "ok", "msgid": "MSG-1"})

    def _send(self, code: int, obj) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # 静音
        pass


@pytest.fixture()
def wecom_api():
    server = _FakeWeComServer(("127.0.0.1", 0), _FakeWeComHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ================================================================== 装配


class _EchoAgentAdapter:
    def send(self, request, connection) -> AgentResponse:
        return AgentResponse(content=f"echo:{request.content}",
                             external_session_id="agent-session-1")


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine, wecom_api) -> int:
    """channel.config = 入站三键 + 出站 corpsecret/agentid + api_base→fake。"""
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(
            name="wecom-1", type="wecom",
            config={
                "token": TOKEN, "encoding_aes_key": AES_KEY, "corp_id": CORP_ID,
                "corpsecret": CORPSECRET, "agentid": AGENTID,
                "api_base": wecom_api.api_base, "timeout_s": 5.0,
            }))
        agent_a = uow.agents.create(Agent(name="agent-a", type="acp"))
        uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="zhangsan"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent_a.id, name="path-a",
            status="active", config={"timeout_s": 5.0}, credential_ref="cred-e2e2"))
        # Implementation Package 2 / Step 2：单聊会话键 = FromUserName
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="zhangsan",
            connection_id=path.id))
        uow.commit()
        return channel.id


def _build_runtime(db_engine, channel_adapter) -> RuntimeCoordinator:
    uow = _uow_factory(db_engine)
    return RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=_EchoAgentAdapter(), uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )


def _build_app(db_engine, runtime, inbound_adapter) -> TestClient:
    # 12.9.4.6：intake 只入队，执行权归 Worker（与生产组合根同形）
    uow = _uow_factory(db_engine)
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=uow)
    service = ChannelInboundService(
        uow_factory=uow, intake=phases,
        inbound_adapter=inbound_adapter)
    app = FastAPI()
    app.include_router(build_channel_inbound_api(service, worker))
    return TestClient(app)


# 与 10.2.1 同名工厂（router 工厂在此文件内引入以保持测试自包含）
from app.api.runtime.channels import build_channel_inbound_api  # noqa: E402


# ================================================================== E2E


def test_c1_full_outbound_chain_via_runtime(db_engine, wecom_api) -> None:
    """RuntimeCoordinator → DeliveryExecutor → WeComAdapter.deliver → Fake WeCom API。"""
    channel_id = _seed(db_engine, wecom_api)
    wecom = WeComAdapter(uow_factory=_uow_factory(db_engine))
    from app.application.delivery import build_outbound_response
    from app.domain.inbound_interaction import InboundInteraction
    inbound = InboundInteraction(
        channel_id=channel_id, external_interaction_id="i-1",
        external_conversation_id="zhangsan", external_identity_id="zhangsan",
        content="请总结今天的会话要点")
    # P1 / C1：投递是**显式**的第 7 段（execute() 只到 Dispatch）。
    coordinator = _build_runtime(db_engine, wecom)
    result = coordinator.execute(inbound)
    coordinator.deliver(build_outbound_response(inbound, result.agent_response))

    # Fake API 真实收到 message/send：recipient/agentid/content 映射正确
    assert len(wecom_api.send_requests) == 1
    sent = wecom_api.send_requests[-1]
    assert sent["payload"] == {
        "touser": "zhangsan", "msgtype": "text",
        "agentid": AGENTID, "text": {"content": "echo:请总结今天的会话要点"},
    }
    assert sent["access_token"] == wecom_api.access_token
    # token 真实按 corpid/corpsecret 获取
    assert wecom_api.token_requests == [{"corpid": CORP_ID, "corpsecret": CORPSECRET}]


def test_c2_each_delivery_fetches_fresh_token_no_cache(db_engine, wecom_api) -> None:
    """两次交付 → 两次 gettoken（不缓存，10.2.2 范围控制的正验证）。"""
    channel_id = _seed(db_engine, wecom_api)
    wecom = WeComAdapter(uow_factory=_uow_factory(db_engine))
    outbound = OutboundResponse(channel_id=channel_id,
                                external_conversation_id="zhangsan",
                                content="first")
    wecom.deliver(outbound)
    wecom.deliver(OutboundResponse(channel_id=channel_id,
                                   external_conversation_id="zhangsan",
                                   content="second"))
    assert len(wecom_api.token_requests) == 2
    assert len(wecom_api.send_requests) == 2
    # outbound 零修改（frozen 契约的行为侧验证）
    assert outbound.content == "first" and outbound.channel_id == channel_id


def test_c3_webhook_full_loop_wecom_to_wecom(db_engine, wecom_api) -> None:
    """webhook 入站（合法 wire）→ Runtime → 出站 → Fake WeCom API：完整渠道闭环。"""
    channel_id = _seed(db_engine, wecom_api)
    wecom = WeComAdapter(uow_factory=_uow_factory(db_engine))
    client = _build_app(db_engine, _build_runtime(db_engine, wecom), wecom)

    encrypt = encrypt_message(TEXT_XML, AES_KEY, CORP_ID)
    resp = client.post(
        f"/api/channels/{channel_id}/wecom/webhook",
        params={"msg_signature": compute_signature(TOKEN, "1409659813", "n", encrypt),
                "timestamp": "1409659813", "nonce": "n"},
        content=(f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
                 f"<Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>").encode("utf-8"))
    assert resp.status_code == 200
    assert len(wecom_api.send_requests) == 1
    assert wecom_api.send_requests[-1]["payload"]["touser"] == "zhangsan"
    assert wecom_api.send_requests[-1]["payload"]["text"]["content"] == \
        "echo:请总结今天的会话要点"


def test_c4_send_errcode_failure_maps_to_delivery_failed(db_engine, wecom_api) -> None:
    """errcode!=0 → ChannelDeliveryFailedError（processing SUCCESS ≠ delivery）。"""
    channel_id = _seed(db_engine, wecom_api)
    wecom = WeComAdapter(uow_factory=_uow_factory(db_engine))
    wecom_api.send_errcode = 81013
    with pytest.raises(ChannelDeliveryFailedError):
        wecom.deliver(OutboundResponse(channel_id=channel_id,
                                       external_conversation_id="zhangsan",
                                       content="hi"))
    # 12.9.4.6：执行已移出入站链路——delivery 失败不再经 handle_message 冒泡，
    # 改由 **Worker.process** 冒泡；入站用合法 wire（解析先于出站发生），
    # 失败点仍真正落在 delivery 阶段。
    runtime = _build_runtime(db_engine, wecom)
    uow = _uow_factory(db_engine)
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=uow)
    service = ChannelInboundService(
        uow_factory=uow, intake=phases, inbound_adapter=wecom)
    from app.domain.inbound_interaction import InboundInteraction
    encrypt = encrypt_message(TEXT_XML, AES_KEY, CORP_ID)
    interaction_id = service.handle_message(
        channel_id,
        signature=compute_signature(TOKEN, "1409659813", "n", encrypt),
        timestamp="1409659813", nonce="n",
        body=(f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
              f"<Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>"))
    assert interaction_id is not None
    with pytest.raises(ChannelDeliveryFailedError):
        worker.process(interaction_id)


def test_c5_token_auth_failure_maps_to_delivery_failed(db_engine, wecom_api) -> None:
    """gettoken errcode!=0（corpsecret 错）→ ChannelDeliveryFailedError。"""
    _seed(db_engine, wecom_api)
    bad = WeComAdapter(uow_factory=lambda: _UoWWithSecret(
        "wrong-secret", wecom_api.api_base))
    with pytest.raises(ChannelDeliveryFailedError):
        bad.deliver(OutboundResponse(channel_id=_CHANNEL_ID,
                                     external_conversation_id="zhangsan",
                                     content="hi"))
    # token 请求确实发出（corpid 正确、secret 错误被 Fake API 拒绝）
    assert wecom_api.token_requests[-1] == {"corpid": CORP_ID,
                                            "corpsecret": "wrong-secret"}


_CHANNEL_ID = 1


class _UoWWithSecret:
    def __init__(self, secret: str, api_base: str) -> None:
        self._channel = Channel(
            name="wecom", type="wecom", id=_CHANNEL_ID,
            config={"corp_id": CORP_ID, "corpsecret": secret,
                    "agentid": AGENTID, "api_base": api_base, "timeout_s": 5.0})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def channels(self):
        return self

    def get_by_id(self, channel_id: int):
        return self._channel if channel_id == _CHANNEL_ID else None
