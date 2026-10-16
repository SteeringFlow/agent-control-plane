"""Step 10.3.1 Layer B+C —— Multi-Agent Full Runtime E2E + Session isolation。

实验变量（与 10.1.2 同一纪律）：

    Channel A/B（两条独立 Access Path，同一种 ACPAdapter）
        ↓ RuntimeCoordinator（既有六段，零改动）
    Agent A = 真实 HTTP ACP runtime（fake，本地独立 server）
    Agent B = 真实 HTTP ACP runtime（fake，本地独立 server）
        ↓
    RecordingChannelAdapter（记录 Delivery，验证 A/B 不串）

B：请求 A → Agent A 收到（Agent B 永远收不到 A）；请求 B → Agent B 收到。
C：二次请求各自 REUSE_ACTIVE，Session A/B 不串（CP 侧 + Agent 侧双重证据）。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.adapters.agents.acp import ACPAdapter
from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork


# ================================================================== 两个独立 ACP runtime（fake HTTP server）


class _AcpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.expected_credential = "cred-x"
        self.received: list[dict] = []      # {"credential","message","session_id"}
        self.turns: dict[str, int] = {}     # session_id -> turn
        host, port = self.server_address[:2]
        self.url = f"http://{host}:{port}"


class _AcpHandler(BaseHTTPRequestHandler):
    """最小 ACP binding：POST /messages {"message","session_id"} → ack + session。"""

    def do_POST(self) -> None:  # noqa: N802
        srv = self.server
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"error": "bad json"})
            return
        record = {"credential": self.headers.get("X-Acp-Credential-Ref"),
                  "message": body.get("message"),
                  "session_id": body.get("session_id")}
        if record["credential"] != srv.expected_credential:
            self._send(401, {"error": "unauthorized"})
            return
        srv.received.append(record)
        sid = record["session_id"] or f"session-of-{srv.expected_credential}"
        srv.turns[sid] = srv.turns.get(sid, 0) + 1
        self._send(200, {"content": f"ack[{srv.expected_credential}]:"
                                    f"{record['message']}:turn{srv.turns[sid]}",
                         "session_id": sid})

    def _send(self, code: int, obj) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # 静音
        pass


import pytest  # noqa: E402


@pytest.fixture()
def agent_a_server():
    server = _AcpServer(("127.0.0.1", 0), _AcpHandler)
    server.expected_credential = "cred-a"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture()
def agent_b_server():
    server = _AcpServer(("127.0.0.1", 0), _AcpHandler)
    server.expected_credential = "cred-b"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ================================================================== 装配


class RecordingChannelAdapter:
    """Delivery 记录器：A/B 的 OutboundResponse 全部落在这里供断言。"""

    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine, agent_a_url: str, agent_b_url: str) -> dict[str, int]:
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        ch_a = uow.channels.create(Channel(name="wecom-a", type="wecom"))
        ch_b = uow.channels.create(Channel(name="wecom-b", type="wecom"))
        agent_a = uow.agents.create(Agent(name="agent-a", type="acp"))
        agent_b = uow.agents.create(Agent(name="agent-b", type="acp"))
        uow.identities.create(Identity(
            user_id=alice.id, channel_id=ch_a.id, external_id="wx_a"))
        uow.identities.create(Identity(
            user_id=alice.id, channel_id=ch_b.id, external_id="wx_b"))
        path_a = uow.connections.create(Connection(
            channel_id=ch_a.id, agent_id=agent_a.id, name="path-a",
            status="active",
            config={"endpoint": agent_a_url, "timeout_s": 5.0},
            credential_ref="cred-a"))
        path_b = uow.connections.create(Connection(
            channel_id=ch_b.id, agent_id=agent_b.id, name="path-b",
            status="active",
            config={"endpoint": agent_b_url, "timeout_s": 5.0},
            credential_ref="cred-b"))
        # Implementation Package 2 / Step 2：每条路径显式授权自己的会话
        for channel, conversation, path in ((ch_a, "user-A", path_a),
                                            (ch_b, "user-B", path_b)):
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=channel.id, external_conversation_id=conversation,
                connection_id=path.id))
        uow.commit()
        return {"channel_a": ch_a.id, "channel_b": ch_b.id,
                "agent_a": agent_a.id, "agent_b": agent_b.id}


def _coordinator(db_engine, channel_adapter) -> RuntimeCoordinator:
    uow = _uow_factory(db_engine)
    return RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=ACPAdapter(), uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )


def _inbound(channel_id: int, content: str, interaction: str,
             conversation: str, identity: str) -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id=interaction,
        external_conversation_id=conversation,
        external_identity_id=identity, content=content)


def _sessions(db_engine) -> list[SessionModel]:
    from sqlalchemy.orm import Session as SASession
    with SASession(db_engine) as s:
        return list(s.execute(select(SessionModel)).scalars().all())


# ================================================================== B：Full Runtime E2E（两条路径）


def test_b1_request_a_reaches_only_agent_a(
        db_engine, agent_a_server, agent_b_server) -> None:
    g = _seed(db_engine, agent_a_server.url, agent_b_server.url)
    sink = RecordingChannelAdapter()
    _coordinator(db_engine, sink).execute(_inbound(
        g["channel_a"], "A 的请求", "i-1", "user-A", "wx_a"))

    assert len(agent_a_server.received) == 1
    assert agent_a_server.received[0]["message"] == "A 的请求"
    assert agent_a_server.received[0]["credential"] == "cred-a"
    assert agent_b_server.received == []            # Agent B 永远收不到 A
    assert sink.deliveries[-1].content == "ack[cred-a]:A 的请求:turn1"
    assert sink.deliveries[-1].channel_id == g["channel_a"]
    assert sink.deliveries[-1].external_conversation_id == "user-A"


def test_b2_request_b_reaches_only_agent_b(
        db_engine, agent_a_server, agent_b_server) -> None:
    g = _seed(db_engine, agent_a_server.url, agent_b_server.url)
    sink = RecordingChannelAdapter()
    _coordinator(db_engine, sink).execute(_inbound(
        g["channel_b"], "B 的请求", "i-1", "user-B", "wx_b"))

    assert len(agent_b_server.received) == 1
    assert agent_b_server.received[0]["message"] == "B 的请求"
    assert agent_b_server.received[0]["credential"] == "cred-b"
    assert agent_a_server.received == []            # Agent A 永远收不到 B
    assert sink.deliveries[-1].content == "ack[cred-b]:B 的请求:turn1"
    assert sink.deliveries[-1].channel_id == g["channel_b"]


def test_b3_interleaved_requests_stay_isolated(
        db_engine, agent_a_server, agent_b_server) -> None:
    """A,B,A,B 交错 —— 两条 Access Path 全程零串扰。"""
    g = _seed(db_engine, agent_a_server.url, agent_b_server.url)
    sink = RecordingChannelAdapter()
    runtime = _coordinator(db_engine, sink)
    plan = [("a", "A-1"), ("b", "B-1"), ("a", "A-2"), ("b", "B-2")]
    for i, (key, content) in enumerate(plan, start=1):
        runtime.execute(_inbound(g[f"channel_{key}"], content, f"i-{i}",
                                 f"user-{key.upper()}", f"wx_{key}"))

    assert [r["message"] for r in agent_a_server.received] == ["A-1", "A-2"]
    assert [r["message"] for r in agent_b_server.received] == ["B-1", "B-2"]
    assert [d.channel_id for d in sink.deliveries] == [
        g["channel_a"], g["channel_b"], g["channel_a"], g["channel_b"]]
    assert [d.content for d in sink.deliveries] == [
        "ack[cred-a]:A-1:turn1", "ack[cred-b]:B-1:turn1",
        "ack[cred-a]:A-2:turn2", "ack[cred-b]:B-2:turn2"]


# ================================================================== C：Session isolation


def test_c1_second_request_a_reuses_session_a_not_b(
        db_engine, agent_a_server, agent_b_server) -> None:
    """Channel A 二次请求 → REUSE_ACTIVE → Session A（不重复创建、归属 agent A）。

    注：RuntimeCoordinator（9.8.2 冻结）调用 dispatch 只传三参，
    AgentResponse.external_session_id 的回流不在 Runtime 主链路上——
    那是 Runtime/Interaction persistence 的未来演进，10.3.1 不改。
    """
    g = _seed(db_engine, agent_a_server.url, agent_b_server.url)
    runtime = _coordinator(db_engine, RecordingChannelAdapter())
    runtime.execute(_inbound(g["channel_a"], "A-1", "i-1", "user-A", "wx_a"))
    runtime.execute(_inbound(g["channel_a"], "A-2", "i-2", "user-A", "wx_a"))

    assert [r["message"] for r in agent_a_server.received] == ["A-1", "A-2"]
    # CP 侧：channel A 恰好 1 条 session，归属 agent A
    rows = [s for s in _sessions(db_engine) if s.channel_id == g["channel_a"]]
    assert len(rows) == 1
    assert rows[0].agent_id == g["agent_a"]
    assert rows[0].external_conversation_id == "user-A"
    assert rows[0].status == "active"


def test_c2_second_request_b_reuses_session_b_not_a(
        db_engine, agent_a_server, agent_b_server) -> None:
    g = _seed(db_engine, agent_a_server.url, agent_b_server.url)
    runtime = _coordinator(db_engine, RecordingChannelAdapter())
    runtime.execute(_inbound(g["channel_b"], "B-1", "i-1", "user-B", "wx_b"))
    runtime.execute(_inbound(g["channel_b"], "B-2", "i-2", "user-B", "wx_b"))

    assert [r["message"] for r in agent_b_server.received] == ["B-1", "B-2"]
    rows = [s for s in _sessions(db_engine) if s.channel_id == g["channel_b"]]
    assert len(rows) == 1
    assert rows[0].agent_id == g["agent_b"]
    assert rows[0].external_conversation_id == "user-B"


def test_c3_sessions_never_cross_between_paths(
        db_engine, agent_a_server, agent_b_server) -> None:
    """A、B 各自建立 session 后交错使用 —— 全局恰 2 条 session，归属互斥。"""
    g = _seed(db_engine, agent_a_server.url, agent_b_server.url)
    runtime = _coordinator(db_engine, RecordingChannelAdapter())
    for i, (key, conv) in enumerate([("a", "user-A"), ("b", "user-B"),
                                     ("a", "user-A"), ("b", "user-B")],
                                    start=1):
        runtime.execute(_inbound(g[f"channel_{key}"], f"msg-{i}", f"i-{i}",
                                 conv, f"wx_{key}"))

    rows = _sessions(db_engine)
    assert len(rows) == 2                       # REUSE_ACTIVE 生效：不重复创建
    assert {(r.channel_id, r.agent_id) for r in rows} == {
        (g["channel_a"], g["agent_a"]), (g["channel_b"], g["agent_b"])}
    assert {r.external_conversation_id for r in rows} == {"user-A", "user-B"}
    # Agent 侧隔离：各自 runtime 只见过自己的会话
    assert agent_a_server.turns == {"session-of-cred-a": 2}
    assert agent_b_server.turns == {"session-of-cred-b": 2}
