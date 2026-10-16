"""Step 10.1.2 —— Real ACP Agent Adapter Implementation（第一真实 Agent Adapter）。

实验变量严格控制：Channel = Test Channel；Runtime Kernel = 既有
coordinator/dispatch/session/routing/identity（**零改动**）；Agent = **真实 HTTP
ACP Agent**（本地 ThreadingHTTPServer，真实 TCP/HTTP 传输，不是 in-memory double）；
Adapter = **真实 ACPAdapter**（app/adapters/agents/acp.py）。

仓库无 ACP wire spec（10.1.1 已确认）→ 本次用「最小 ACP HTTP binding」做架构反证：
    POST {endpoint}  body {"message", "session_id"}
    headers: Content-Type/Accept json + X-Acp-Credential-Ref
    200 → {"content", "session_id"}
真实外部 ACP-compatible Agent 接入时只需在 acp.py 内校准 wire（Frozen Port 不动）。

验收标准：
A  最小真实调用 AgentRequest → ACPAdapter → 真实 HTTP Agent → AgentResponse 成功；
B  Response normalization：ACP 原始响应稳定归一化成 AgentResponse(content,
   external_session_id, metadata)；
C  Session 回流：None → ACP session → external_session_id，后续调用能复用该 ID
   （不建 AgentSession/SessionMapping 表，只观察真实行为）；
D  Connection ownership：Adapter 只使用 Connection.credential_ref / Connection.config；
E  Port 不变：send(request, connection)；
F  Runtime Kernel 不变：coordinator/dispatch/domain 不改即承载真实 Adapter
   （全链 E2E 证明）；
G  失败映射：transport → AGENT_DISPATCH_FAILED；invalid ACP response →
   AGENT_RESPONSE_INVALID；具体 ACP 错误留在 Adapter 内部，不泄漏新错误码。
"""

import inspect
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import pytest

from app.adapters.agents.acp import ACPAdapter
from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.errors import AgentDispatchFailedError, AgentResponseInvalidError
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.agent import Agent
from app.domain.agent_request import AgentRequest
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.routing_result import RoutingResult
from app.domain.user import User
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND_DIR = Path(__file__).resolve().parents[1]
ACP_PY = BACKEND_DIR / "app" / "adapters" / "agents" / "acp.py"

# ------------------------------------------------------------------ 真实 HTTP ACP Agent（测试底座）


class _AcpHTTPServer(ThreadingHTTPServer):
    """真实 HTTP 端口上的最小 ACP-compatible Agent（有会话存储 + 凭据校验）。"""

    daemon_threads = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.expected_credential = "cred-acp-test"
        self.sessions: dict[str, int] = {}          # session_id -> turn count
        self.received: list[dict] = []              # 收到的请求记录
        host, port = self.server_address[:2]
        self.url = f"http://{host}:{port}"


class _AcpAgentHandler(BaseHTTPRequestHandler):
    """最小 ACP binding 服务端：POST /messages 单端点。"""

    def do_POST(self) -> None:  # noqa: N802
        srv = self.server
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"error": "bad json"})
            return
        if not isinstance(body, dict):
            self._send(400, {"error": "not object"})
            return

        credential = self.headers.get("X-Acp-Credential-Ref")
        if credential != srv.expected_credential:
            self._send(401, {"error": "unauthorized"})
            return

        message = body.get("message")
        session_id = body.get("session_id")
        if not isinstance(message, str) \
                or not isinstance(session_id, (str, type(None))):
            self._send(400, {"error": "bad payload"})
            return
        srv.received.append({
            "credential": credential, "message": message, "session_id": session_id})

        # ---- Agent session 生命周期（真实行为，供 C 观察）----
        if session_id is None:
            session_id = f"agent-session-{len(srv.sessions) + 1}"
            srv.sessions[session_id] = 0
        elif session_id not in srv.sessions:
            self._send(409, {"error": "unknown session"})
            return
        srv.sessions[session_id] += 1
        turn = srv.sessions[session_id]

        if message == "__blank__":
            payload = {"content": "", "session_id": session_id}
        elif message == "__no_content__":
            payload = {"session_id": session_id}
        elif message == "__garbage__":
            self._send_raw(200, b"this-is-not-json", "text/plain")
            return
        elif message == "__non_dict__":
            payload = [1, 2]  # 200 但非 JSON object
        else:
            payload = {"content": f"ack:{message}:turn{turn}",
                       "session_id": session_id}
        self._send(200, payload)

    def _send(self, code: int, obj) -> None:
        self._send_raw(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _send_raw(self, code: int, data: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # 静音访问日志
        pass


@pytest.fixture()
def acp_server():
    """真实 HTTP ACP Agent（随机端口，随用随建）。"""
    server = _AcpHTTPServer(("127.0.0.1", 0), _AcpAgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture()
def dead_acp_url():
    """一个没有监听端口的 URL（transport 失败用）。"""
    server = _AcpHTTPServer(("127.0.0.1", 0), _AcpAgentHandler)
    url = server.url
    server.server_close()
    return url


# ------------------------------------------------------------------ 工厂

def _connection(acp_server, credential: str = "cred-acp-test",
                **config_overrides) -> Connection:
    config = {"endpoint": acp_server.url, "timeout_s": 5.0}
    config.update(config_overrides)
    return Connection(channel_id=1, agent_id=1, name="acp-path", id=10,
                      config=config, credential_ref=credential)


def _request(content: str = "hello",
             external_session_id: str | None = None) -> AgentRequest:
    return AgentRequest(agent_id=1, control_plane_session_id=7,
                        content=content, external_session_id=external_session_id)


# ------------------------------------------------------------------ 本地 Test Channel（F 用）

class _TestChannelAdapter:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))


def _seed(db_engine, endpoint: str, credential: str = "cred-acp-test") -> dict[str, int]:
    """channel(wecom-1) → active Connection → agent-a；Connection 带真实连接信息。"""
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent_a = uow.agents.create(Agent(name="agent-a", type="acp"))
        agent_b = uow.agents.create(Agent(name="agent-b", type="acp"))
        id_a = uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        conn_a = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent_a.id, name="path-a",
            config={"endpoint": endpoint, "timeout_s": 5.0},
            credential_ref=credential))
        uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent_b.id, name="path-b",
            status="inactive"))
        # Implementation Package 2 / Step 2：显式授权该会话（default-deny 前提）
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="conversation-acp",
            connection_id=conn_a.id))
        uow.commit()
        return {"alice": alice.id, "channel": channel.id, "identity_a": id_a.id,
                "agent_a": agent_a.id, "agent_b": agent_b.id,
                "connection_a": conn_a.id}


def _dispatch(db_engine, adapter) -> AgentDispatchExecutor:
    return AgentDispatchExecutor(agent_adapter=adapter, uow_factory=_uow_factory(db_engine))


def _coordinator(db_engine, adapter, channel_adapter) -> RuntimeCoordinator:
    uow = _uow_factory(db_engine)
    return RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(agent_adapter=adapter, uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )


def _inbound(channel_id: int, content: str = "hello",
             conversation: str = "conversation-acp") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id="i-1",
        external_conversation_id=conversation,
        external_identity_id="wx_a", content=content)


# ================================================================== A 最小真实调用


def test_a_minimal_real_call_over_http(acp_server) -> None:
    """AgentRequest → 真实 HTTP POST → 真实 Agent → AgentResponse。"""
    adapter = ACPAdapter()
    response = adapter.send(_request("hello"), _connection(acp_server))
    assert response.content == "ack:hello:turn1"
    assert response.external_session_id == "agent-session-1"
    assert response.metadata is None
    # 真实收到：正确凭据头 + 请求体
    record = acp_server.received[-1]
    assert record == {"credential": "cred-acp-test",
                      "message": "hello", "session_id": None}


def test_a_runtime_kernel_full_loop_with_real_agent(db_engine, acp_server) -> None:
    """F：coordinator/dispatch/domain **零改动**承载真实 ACPAdapter + 真实 Agent——
    Test Channel → Runtime Kernel → Real ACPAdapter → Real HTTP Agent 全链成功。"""
    g = _seed(db_engine, acp_server.url)
    adapter = ACPAdapter()
    channel_adapter = _TestChannelAdapter()
    _coordinator(db_engine, adapter, channel_adapter).execute(
        _inbound(g["channel"], content="hello"))
    outbound = channel_adapter.deliveries[-1]
    assert outbound.content == "ack:hello:turn1"          # 真实 Agent 的回复
    assert outbound.channel_id == g["channel"]            # 出口 = 入口
    assert outbound.external_conversation_id == "conversation-acp"
    # CREATE_NEW：真实建了 CP Session（Runtime 自己的 Session 逻辑未被绕过）
    with Session(db_engine) as session:
        rows = session.execute(select(func.count()).select_from(SessionModel)
                               ).scalar_one()
    assert rows == 1
    assert acp_server.sessions == {"agent-session-1": 1}


# ================================================================== B Response normalization


def test_b_acp_response_normalized_into_contract(acp_server) -> None:
    """ACP 原始响应（含协议专用 key）稳定归一化成 AgentResponse 三字段。"""
    adapter = ACPAdapter()
    response = adapter.send(_request("some question"), _connection(acp_server))
    assert isinstance(response.content, str)
    assert response.content == "ack:some question:turn1"
    assert isinstance(response.external_session_id, str)
    assert response.metadata is None
    # external_session_id 出现在 AgentResponse 字段面（契约类校验）
    assert hasattr(response, "external_session_id")


# ================================================================== C Session 回流


def test_c_session_round_trip_adapter_level(acp_server) -> None:
    """None → ACP session → external_session_id → 后续调用复用（不建映射表）。"""
    adapter = ACPAdapter()
    conn = _connection(acp_server)
    first = adapter.send(_request("first"), conn)
    assert first.external_session_id == "agent-session-1"

    second = adapter.send(_request("second", first.external_session_id), conn)
    assert second.external_session_id == "agent-session-1"
    assert acp_server.sessions == {"agent-session-1": 2}   # 同一个 Agent session
    assert second.content == "ack:second:turn2"            # 会话内续聊
    # 服务端看到的两次请求：第一次无 session → 第二次带回同一 id
    assert acp_server.received[0]["session_id"] is None
    assert acp_server.received[1]["session_id"] == "agent-session-1"


def test_c_session_round_trip_through_dispatch(db_engine, acp_server) -> None:
    """external_session_id 经 AgentDispatchExecutor 原样穿过 Port（Kernel 不劫持）。"""
    g = _seed(db_engine, acp_server.url)
    executor = _dispatch(db_engine, ACPAdapter())
    rr = RoutingResult(connection_id=g["connection_a"], agent_id=g["agent_a"])
    r1 = executor.execute(rr, control_plane_session_id=1, content="one")
    r2 = executor.execute(rr, control_plane_session_id=1, content="two",
                          external_session_id=r1.external_session_id)
    assert r2.external_session_id == r1.external_session_id == "agent-session-1"
    assert acp_server.sessions == {"agent-session-1": 2}


def test_c_unknown_session_id_fails_closed(acp_server) -> None:
    """带未知 external_session_id → Agent 409 → AGENT_DISPATCH_FAILED（不猜测）。"""
    adapter = ACPAdapter()
    with pytest.raises(AgentDispatchFailedError):
        adapter.send(_request("hi", "ghost-session"), _connection(acp_server))


# ================================================================== D Connection ownership


def test_d_adapter_uses_only_connection_fields(acp_server) -> None:
    """凭据/endpoint 全部来自传入的 Connection：换凭据即鉴权失败，不读全局。"""
    adapter = ACPAdapter()
    ok = adapter.send(_request("hi"), _connection(acp_server, "cred-acp-test"))
    assert ok.content.startswith("ack:")
    with pytest.raises(AgentDispatchFailedError):
        adapter.send(_request("hi"), _connection(acp_server, "wrong-credential"))
    # 服务端只收到了携带正确凭据的那一次请求（第二次 401 未记录）
    assert len(acp_server.received) == 1
    assert acp_server.received[0]["credential"] == "cred-acp-test"


def test_d_endpoint_is_per_connection_config(acp_server) -> None:
    """endpoint 来自 Connection.config，而非任何全局配置。"""
    adapter = ACPAdapter()
    # 缺 endpoint → Fail Closed（不发网络请求）
    with pytest.raises(AgentDispatchFailedError):
        adapter.send(_request("hi"), Connection(
            channel_id=1, agent_id=1, name="no-config", id=2,
            config={}, credential_ref="cred-acp-test"))


# ================================================================== E Port 不变


def test_e_adapter_satisfies_frozen_port() -> None:
    from app.repositories.agent_adapter import AgentAdapter

    adapter = ACPAdapter()
    assert isinstance(adapter, AgentAdapter)  # runtime_checkable Port 鸭子检查
    params = set(inspect.signature(ACPAdapter.send).parameters)
    assert {"request", "connection"} <= params


# ================================================================== G 失败映射


def test_g_transport_failure_maps_to_dispatch_failed(dead_acp_url) -> None:
    """连接被拒（transport）→ AgentDispatchFailedError（AGENT_DISPATCH_FAILED）。"""
    adapter = ACPAdapter()
    conn = _connection_for_url(dead_acp_url)
    with pytest.raises(AgentDispatchFailedError):
        adapter.send(_request("hi"), conn)


def _connection_for_url(url: str) -> Connection:
    return Connection(channel_id=1, agent_id=1, name="dead", id=3,
                      config={"endpoint": url, "timeout_s": 2.0},
                      credential_ref="cred-acp-test")


def test_g_transport_failure_through_dispatch(db_engine, dead_acp_url) -> None:
    """Dispatch 层同样只见 AGENT_DISPATCH_FAILED。"""
    g = _seed(db_engine, dead_acp_url)
    executor = _dispatch(db_engine, ACPAdapter())
    with pytest.raises(AgentDispatchFailedError):
        executor.execute(RoutingResult(connection_id=g["connection_a"],
                                       agent_id=g["agent_a"]),
                         control_plane_session_id=1, content="hi")


def test_g_invalid_acp_response_maps_to_response_invalid(db_engine, acp_server) -> None:
    """ACP 200 但无可用内容 → 归一化 content="" → Dispatch 契约校验 →
    AgentResponseInvalidError（AGENT_RESPONSE_INVALID）。"""
    g = _seed(db_engine, acp_server.url)
    executor = _dispatch(db_engine, ACPAdapter())
    with pytest.raises(AgentResponseInvalidError):
        executor.execute(RoutingResult(connection_id=g["connection_a"],
                                       agent_id=g["agent_a"]),
                         control_plane_session_id=1, content="__blank__")


def test_g_acp_protocol_anomalies_all_stay_dispatch_failed(acp_server) -> None:
    """协议层异常（非 JSON / 非 object / 缺 content / 坏 session_id）一律
    AGENT_DISPATCH_FAILED，且具体 ACP 错误不外泄为新错误码。"""
    adapter = ACPAdapter()
    for content in ("__garbage__", "__non_dict__", "__no_content__"):
        with pytest.raises(AgentDispatchFailedError):
            adapter.send(_request(content), _connection(acp_server))
    with pytest.raises(AgentDispatchFailedError):  # 401 鉴权失败
        adapter.send(_request("hi"), _connection(acp_server, "bad-cred"))


def test_g_no_new_error_types_in_adapter_module() -> None:
    """acp.py 不定义任何新异常类型：错误面 = 既有 ApplicationError 家族。"""
    src = ACP_PY.read_text(encoding="utf-8")
    import ast
    tree = ast.parse(src)
    classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert classes == {"ACPAdapter"}, classes
    import app.adapters.agents.acp as acp_mod
    from app.application.errors import (AgentDispatchFailedError,
                                        AgentResponseInvalidError,
                                        ApplicationError)
    # Adapter 只依赖既有错误族；且不会自己抛出 AgentResponseInvalidError
    # （空内容归一化后由 Dispatch 契约校验抛）—— 见 test_g_* 全链测试。
    assert acp_mod.AgentDispatchFailedError is AgentDispatchFailedError
    assert issubclass(AgentDispatchFailedError, ApplicationError)
    assert issubclass(AgentResponseInvalidError, ApplicationError)


def test_g_adapter_blank_reply_normalized_not_guessed(acp_server) -> None:
    """空内容：归一化为 content=""（不猜），留给契约校验判 AGENT_RESPONSE_INVALID。"""
    response = ACPAdapter().send(_request("__blank__"), _connection(acp_server))
    assert response.content == ""
