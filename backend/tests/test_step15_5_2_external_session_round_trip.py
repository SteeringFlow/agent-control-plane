"""Step 15.5.2 —— external_session_id Round-Trip Closure（全链路 Stateful 验收）。

修复（方案 A，15.5.2 冻结边界）：
- SessionResolution 增加**只读输出** external_session_id（REUSE_ACTIVE 时从
  已复用 Session 原样带出；CREATE_NEW 恒 None）；
- Coordinator 把它透传给 dispatch.execute（第 4 实参）；
- AgentRequest / AgentResponse / ACPAdapter / Dispatch / Persistence / schema
  / API / wire **零改动**。

本测试证明真正的 round-trip closure（不是"手动把 X 塞给 executor"的旧测法）：

    Turn 1   Agent received session_id = None   → Agent returned X
    Turn 2   Agent received session_id = X      → Agent returned X（REUSE_ACTIVE）
    Turn 3   Agent received session_id = X      → Agent returned X（REUSE_ACTIVE）

    DB：sessions = 1，external_session_id = X

链路 = 真实 HTTP Stateful ACP Agent（ThreadingHTTPServer）+ 真实 ACPAdapter +
完整 RuntimeCoordinator + PersistingCoordinator（10.8.4 绑定写入点）。
Agent 收到的 session_id 即 wire 上的 `{"session_id": request.external_session_id}`
（acp.py minimal binding），因此服务端记录就是 AgentRequest 携带该引用的机器证据。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as SASession, sessionmaker

from app.adapters.agents.acp import ACPAdapter
from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import PersistingCoordinator
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
from app.persistence.models.interaction import InteractionModel
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

CREDENTIAL = "cred-acp-roundtrip"
EXTERNAL_SESSION_X = "agent-session-1"   # Turn 1 由 Stateful Agent 签发


# ------------------------------------------------------------------ Stateful ACP Agent


class _StatefulAcpServer(ThreadingHTTPServer):
    """真实 HTTP 端口上的最小 ACP-compatible Agent（会话存储 + 请求记录）。"""

    daemon_threads = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.expected_credential = CREDENTIAL
        self.sessions: dict[str, int] = {}      # session_id -> turn count
        self.received_session_ids: list = []    # 每轮收到的 session_id（机器证据）
        host, port = self.server_address[:2]
        self.url = f"http://{host}:{port}"


class _StatefulAcpHandler(BaseHTTPRequestHandler):
    """Stateful 行为（用户验收标准）：首轮 None → 签发 X；之后收到 X → 续同一会话。"""

    def do_POST(self) -> None:  # noqa: N802
        srv = self.server
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        if self.headers.get("X-Acp-Credential-Ref") != srv.expected_credential:
            self._send(401, {"error": "unauthorized"})
            return

        session_id = body.get("session_id")
        srv.received_session_ids.append(session_id)

        if session_id is None:
            session_id = EXTERNAL_SESSION_X
            srv.sessions[session_id] = 0
        elif session_id not in srv.sessions:
            self._send(409, {"error": "unknown session"})
            return
        srv.sessions[session_id] += 1
        turn = srv.sessions[session_id]

        self._send(200, {"content": f"ack:{body.get('message')}:turn{turn}",
                         "session_id": session_id})

    def _send(self, code: int, obj) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # 静音访问日志
        pass


@pytest.fixture()
def stateful_agent():
    server = _StatefulAcpServer(("127.0.0.1", 0), _StatefulAcpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ------------------------------------------------------------------ 全链路底座


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine, agent_url: str) -> int:
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status="active",
            config={"endpoint": agent_url, "timeout_s": 5.0},
            credential_ref=CREDENTIAL))
        # Implementation Package 2 / Step 2：显式授权该会话（default-deny 前提）
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="conversation-rt",
            connection_id=path.id))
        uow.commit()
        return channel.id


class _SinkChannelAdapter:
    def deliver(self, outbound: OutboundResponse) -> None:  # noqa: ARG002
        pass


def _full_chain(db_engine) -> PersistingCoordinator:
    """生产同形：RuntimeCoordinator（六段真实 Executor）+ PersistingCoordinator。"""
    uow = _uow_factory(db_engine)
    inner = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=ACPAdapter(), uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=_SinkChannelAdapter()),
    )
    return PersistingCoordinator(inner=inner, uow_factory=_uow_factory(db_engine))


def _inbound(channel_id: int, n: int) -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id=f"msg-{n}",
        external_conversation_id="conversation-rt",
        external_identity_id="wx_a", content=f"hello-{n}")


# ------------------------------------------------------------------ Round-Trip Closure


def test_external_session_id_round_trip_closure(db_engine, stateful_agent) -> None:
    """3 轮全链路：None → X → X → X；DB sessions = 1；Turn 2/3 = REUSE_ACTIVE。"""
    channel_id = _seed(db_engine, stateful_agent.url)
    runtime = _full_chain(db_engine)

    r1 = runtime.execute(_inbound(channel_id, 1))
    r2 = runtime.execute(_inbound(channel_id, 2))
    r3 = runtime.execute(_inbound(channel_id, 3))

    x = EXTERNAL_SESSION_X

    # ---- Turn 级：session mode 序列（机器证据）----
    assert r1.session_mode == "CREATE_NEW"
    assert r2.session_mode == "REUSE_ACTIVE"
    assert r3.session_mode == "REUSE_ACTIVE"

    # ---- Agent 每轮收到的 session_id（wire 证据 = AgentRequest 携带值）----
    assert stateful_agent.received_session_ids == [None, x, x]

    # ---- Agent 每轮返回的 external_session_id（响应级事实）----
    assert r1.agent_response.external_session_id == x
    assert r2.agent_response.external_session_id == x
    assert r3.agent_response.external_session_id == x

    # ---- Stateful Agent 内部：同一会话续聊（turn 计数 1→2→3）----
    assert stateful_agent.sessions == {x: 3}
    assert r1.agent_response.content.endswith("turn1")
    assert r2.agent_response.content.endswith("turn2")
    assert r3.agent_response.content.endswith("turn3")

    # ---- DB：sessions = 1，external_session_id = X ----
    with SASession(db_engine) as s:
        rows = s.execute(select(SessionModel)).scalars().all()
        assert len(rows) == 1
        assert rows[0].external_session_id == x

        # ---- DB：3 条 interactions 全部归属同一 Session ----
        interactions = s.execute(
            select(InteractionModel).order_by(InteractionModel.id)).scalars().all()
        assert len(interactions) == 3
        assert {i.session_id for i in interactions} == {rows[0].id}
        # response_payload 快照每轮都带回 X（10.7.2 B 原样投影）
        for i in interactions:
            assert i.response_payload["external_session_id"] == x
