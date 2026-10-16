"""Step 10.2.1 Layer 3 —— 真实 HTTP 边界测试（本地构造合法 WeCom wire，不连真实 WeCom）。

    FastAPI（build_channel_inbound_api）
        ↓ POST /api/channels/{id}/wecom/webhook
    ChannelInboundService
        ↓ WeComAdapter（真实 wire：SHA1/AES/XML）
    InboundInteraction
        ↓
    RuntimeCoordinator（既有六段，零改动）
        ↓ Test Agent Adapter（echo）→ Delivery（sink）

覆盖（10.2.1 §测试分层 · Layer 3）：
    ✓ GET 握手 200 明文回显 / 403 坏签名
    ✓ POST text → 200 空体 + Runtime 全链真实执行（CP Session 物化 + Delivery 回复）
    ✓ POST 坏签名 403（Runtime 未被触发）
    ✓ POST 非 text → 200 acknowledge-ignore（Runtime 未被触发）
    ✓ POST 畸形 400 / 未知渠道 500
    ✓ 失败语义表（10.2.1 裁决：入站协议错误 ≠ Runtime Failure Model）
"""

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
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.agent_adapter import AgentAdapter

TOKEN = "http-test-token"
AES_KEY = "b" * 43
CORP_ID = "wwcorp-e2e"
TIMESTAMP = "1409659813"
NONCE = "nonce-e2e"

TEXT_XML = (
    "<xml><ToUserName><![CDATA[wwcorp-e2e]]></ToUserName>"
    "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
    "<CreateTime>1348831860</CreateTime>"
    "<MsgType><![CDATA[text]]></MsgType>"
    "<Content><![CDATA[给我一份薄壁件夹持建议]]></Content>"
    "<MsgId>1001</MsgId>"
    "<AgentID>1</AgentID></xml>"
)
IMAGE_XML = (
    "<xml><ToUserName><![CDATA[wwcorp-e2e]]></ToUserName>"
    "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
    "<CreateTime>1348831860</CreateTime>"
    "<MsgType><![CDATA[image]]></MsgType>"
    "<PicUrl><![CDATA[http://example/p.jpg]]></PicUrl>"
    "<MediaId><![CDATA[m-1]]></MediaId>"
    "<MsgId>1002</MsgId>"
    "<AgentID>1</AgentID></xml>"
)


# ------------------------------------------------------------------ Test 组件


class _EchoAgentAdapter:
    """echo Agent：回复 echo:<content>（真实 AgentAdapter Port 形状）。"""

    def __init__(self) -> None:
        self.received: list[str] = []

    def send(self, request, connection) -> AgentResponse:
        self.received.append(request.content)
        return AgentResponse(content=f"echo:{request.content}",
                             external_session_id="agent-session-1")


class _SinkChannelAdapter:
    """Delivery sink：记录 OutboundResponse（真实 ChannelAdapter Port 形状）。"""

    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine) -> int:
    """channel(wecom) → active Connection → agent-a；channel.config 带验签材料。"""
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(
            name="wecom-1", type="wecom",
            config={"token": TOKEN, "encoding_aes_key": AES_KEY, "corp_id": CORP_ID}))
        agent_a = uow.agents.create(Agent(name="agent-a", type="acp"))
        uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="zhangsan"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent_a.id, name="path-a",
            status="active", config={"timeout_s": 5.0}, credential_ref="cred-e2e"))
        # Implementation Package 2 / Step 2：单聊会话键 = FromUserName（10.2.1 裁决）
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="zhangsan",
            connection_id=path.id))
        uow.commit()
        return channel.id


def _build_app(db_engine, agent_adapter, channel_adapter) -> TestClient:
    """完整装配：真实 WeComAdapter + 完整 RuntimeCoordinator + 工厂 router。"""
    uow = _uow_factory(db_engine)
    runtime = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=agent_adapter, uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )
    # 12.9.4.6：intake 只入队，执行权归 Worker（与生产组合根同形）
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=uow)
    service = ChannelInboundService(
        uow_factory=uow, intake=phases, inbound_adapter=WeComAdapter())
    app = FastAPI()
    app.include_router(build_channel_inbound_api(service, worker))
    return TestClient(app)


def _handshake_query(plaintext_echo: str, *, timestamp=TIMESTAMP, nonce=NONCE) -> dict:
    encrypt = encrypt_message(plaintext_echo, AES_KEY, CORP_ID)
    return {
        "msg_signature": compute_signature(TOKEN, timestamp, nonce, encrypt),
        "timestamp": timestamp, "nonce": nonce, "echostr": encrypt,
    }


def _message_request(plaintext_xml: str, *, timestamp=TIMESTAMP, nonce=NONCE) -> dict:
    encrypt = encrypt_message(plaintext_xml, AES_KEY, CORP_ID)
    body = (
        f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
        "<AgentID><![CDATA[1]]></AgentID></xml>"
    )
    return {
        "params": {
            "msg_signature": compute_signature(TOKEN, timestamp, nonce, encrypt),
            "timestamp": timestamp, "nonce": nonce,
        },
        "content": body.encode("utf-8"),
    }


def _session_count(db_engine) -> int:
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(SessionModel)).scalar_one()


# ================================================================== A. GET 握手


def test_a1_handshake_returns_plaintext(db_engine) -> None:
    """GET 握手 → 200 + 明文 echostr（WeCom 校验通过的唯一应答形态）。"""
    channel_id = _seed(db_engine)
    client = _build_app(db_engine, _EchoAgentAdapter(), _SinkChannelAdapter())
    resp = client.get(
        f"/api/channels/{channel_id}/wecom/webhook",
        params=_handshake_query("echo-plaintext-42"))
    assert resp.status_code == 200
    assert resp.text == "echo-plaintext-42"


def test_a2_handshake_bad_signature_403(db_engine) -> None:
    """GET 坏签名 → 403（不可信请求，Runtime 未被触发）。"""
    channel_id = _seed(db_engine)
    client = _build_app(db_engine, _EchoAgentAdapter(), _SinkChannelAdapter())
    params = _handshake_query("echo")
    resp = client.get(
        f"/api/channels/{channel_id}/wecom/webhook",
        params={**params, "msg_signature": "0" * 40})
    assert resp.status_code == 403
    assert _session_count(db_engine) == 0


# ================================================================== B. POST text → Runtime 全链


def test_b1_text_message_runs_full_runtime(db_engine) -> None:
    """POST text → 200 空体 + Runtime 六段真实执行（CP Session 物化 + Delivery 回复）。"""
    channel_id = _seed(db_engine)
    agent_adapter, sink = _EchoAgentAdapter(), _SinkChannelAdapter()
    client = _build_app(db_engine, agent_adapter, sink)

    resp = client.post(f"/api/channels/{channel_id}/wecom/webhook",
                       **_message_request(TEXT_XML))
    assert resp.status_code == 200
    assert resp.text == ""                        # acknowledge 空体

    # Runtime 真的执行了：Agent 收到归一化内容
    assert agent_adapter.received == ["给我一份薄壁件夹持建议"]
    # Delivery 真的收到 Agent 回复（channel_id/会话 ID 原样回流）
    assert len(sink.deliveries) == 1
    outbound = sink.deliveries[-1]
    assert outbound.channel_id == channel_id
    assert outbound.external_conversation_id == "zhangsan"   # 单聊裁决（10.2.1）
    assert outbound.content == "echo:给我一份薄壁件夹持建议"
    # CP Session 真实物化（CREATE_NEW 分支）
    assert _session_count(db_engine) == 1


def test_b2_second_text_reuses_session(db_engine) -> None:
    """同一 (identity, connection, conversation) 第二条消息 → REUSE_ACTIVE（不建第二条）。"""
    channel_id = _seed(db_engine)
    client = _build_app(db_engine, _EchoAgentAdapter(), _SinkChannelAdapter())
    client.post(f"/api/channels/{channel_id}/wecom/webhook",
                **_message_request(TEXT_XML))
    client.post(f"/api/channels/{channel_id}/wecom/webhook",
                **_message_request(TEXT_XML.replace("1001", "1003")))
    assert _session_count(db_engine) == 1


# ================================================================== C. 失败语义


def test_c1_post_bad_signature_403_and_runtime_not_triggered(db_engine) -> None:
    """POST 坏签名 → 403；Runtime / Agent / Delivery 全部未被触发。"""
    channel_id = _seed(db_engine)
    agent_adapter, sink = _EchoAgentAdapter(), _SinkChannelAdapter()
    client = _build_app(db_engine, agent_adapter, sink)
    req = _message_request(TEXT_XML)
    resp = client.post(f"/api/channels/{channel_id}/wecom/webhook",
                       params={**req["params"], "msg_signature": "f" * 40},
                       content=req["content"])
    assert resp.status_code == 403
    assert agent_adapter.received == []
    assert sink.deliveries == []
    assert _session_count(db_engine) == 0


def test_c2_non_text_message_acknowledged_and_ignored(db_engine) -> None:
    """POST image → 200 空体（acknowledge-ignore），Runtime 未被触发（MVP 只支持 text）。"""
    channel_id = _seed(db_engine)
    agent_adapter, sink = _EchoAgentAdapter(), _SinkChannelAdapter()
    client = _build_app(db_engine, agent_adapter, sink)
    resp = client.post(f"/api/channels/{channel_id}/wecom/webhook",
                       **_message_request(IMAGE_XML))
    assert resp.status_code == 200
    assert resp.text == ""
    assert agent_adapter.received == []
    assert sink.deliveries == []
    assert _session_count(db_engine) == 0


def test_c3_malformed_body_400(db_engine) -> None:
    """POST 畸形 body → 400（可信渠道但格式非法）。"""
    channel_id = _seed(db_engine)
    client = _build_app(db_engine, _EchoAgentAdapter(), _SinkChannelAdapter())
    resp = client.post(
        f"/api/channels/{channel_id}/wecom/webhook",
        params={"msg_signature": "s", "timestamp": TIMESTAMP, "nonce": NONCE},
        content=b"not-xml",
    )
    assert resp.status_code == 400
    assert _session_count(db_engine) == 0


def test_c4_unknown_channel_500(db_engine) -> None:
    """回调指向不存在的渠道 → 500（服务端配置错误，不隐藏为 404 之外的语义混淆）。"""
    client = _build_app(db_engine, _EchoAgentAdapter(), _SinkChannelAdapter())
    req = _message_request(TEXT_XML)
    resp = client.post("/api/channels/99999/wecom/webhook",
                       params=req["params"], content=req["content"])
    assert resp.status_code == 500


def test_c5_handshake_unknown_channel_500(db_engine) -> None:
    """握手指向未知渠道 → 500。"""
    client = _build_app(db_engine, _EchoAgentAdapter(), _SinkChannelAdapter())
    resp = client.get("/api/channels/99999/wecom/webhook",
                      params=_handshake_query("echo"))
    assert resp.status_code == 500
