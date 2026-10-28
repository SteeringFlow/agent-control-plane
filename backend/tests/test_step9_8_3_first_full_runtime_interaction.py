"""Step 9.8.3 —— First Full Runtime Interaction（第一条完整 Inbound → Outbound 闭环）。

前面的 9.2~9.8.2 分别证明组件正确 / 边界正确 / 顺序正确 / 状态传递正确；9.8.3
回答一个新问题：**一条消息真的从 Channel 进入，能否完整穿过整个 Control Plane 再
回到 Channel？**

    Test Channel Input
        ↓ InboundInteraction
        ↓ RuntimeCoordinator.execute()
        ↓ OutboundResponse
        ↓ Test Channel Output

仍不是真实 Channel/Agent 集成（不碰 ACP/WeCom/HTTP/OAuth/Webhook/Streaming）——
用 Test Agent Adapter + Test Channel Adapter。重点测试整个 **Runtime Control Flow**。

范围边界：**不持久化 Interaction/Activity**——interactions/activities 表在 schema
里存在，但「表存在 ≠ Runtime 现在必须使用它们」；Audit 持久化由真实需求驱动。

E2E ① REUSE_ACTIVE（预置 session）：conversation 回显、cp_session_id == 复用 id、
session 计数不变；
E2E ② CREATE_NEW：session +1、created.id == cp_session_id、outbound.channel_id ==
inbound.channel_id、conversation 回显；
边界闭环：Identity Isolation / Routing Isolation / Session Isolation / Agent
Session Reference / Outbound Boundary（出口不带 user/identity/connection/agent/cp）；
失败链：Identity/Routing → STOP、Dispatch → NO Delivery、Delivery → Dispatch 已成功
后 FAILED（processing ≠ delivery 语义）。
"""

import ast
import dataclasses
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.errors import (
    AgentDispatchFailedError,
    ChannelDeliveryFailedError,
    IdentityResolutionFailedError,
    RoutingFailedError,
)
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.agent import Agent
from app.domain.agent_request import AgentRequest
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.delivery_lifecycle_helper import DeliveringCoordinator

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ 测试底座


class _TestAgentAdapter:
    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []
        self.last_response: AgentResponse | None = None

    def send(self, request: AgentRequest, connection: Connection) -> AgentResponse:
        self.requests.append(request)
        self.last_response = AgentResponse(
            content="test response", external_session_id="agent-s-1")
        return self.last_response


class _FailingAgentAdapter:
    def send(self, request: AgentRequest, connection: Connection) -> AgentResponse:
        raise RuntimeError("agent transport down")


class _TestChannelAdapter:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


class _FailingChannelAdapter:
    def deliver(self, outbound: OutboundResponse) -> None:
        raise RuntimeError("channel transport down")


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))


# 本文件用到的会话键（Package 2 / Step 2 起成功路径需要显式授权）
_GRANTED_CONVERSATIONS = ("conversation-X", "conversation-Y", "convA", "convR",
                          "convS", "convB", "convD")


def _seed(db_engine) -> dict[str, int]:
    """alice(wx_a)/bob(wx_b) @ wecom-1；active conn → agentA（agentB inactive）。"""
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        alice = uow.users.create(User(name="alice"))
        bob = uow.users.create(User(name="bob"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent_a = uow.agents.create(Agent(name="agent-a", type="acp"))
        agent_b = uow.agents.create(Agent(name="agent-b", type="acp"))
        id_a = uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        id_b = uow.identities.create(Identity(
            user_id=bob.id, channel_id=channel.id, external_id="wx_b"))
        conn_a = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent_a.id, name="path-a"))
        uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent_b.id, name="path-b",
            status="inactive"))
        # Implementation Package 2 / Step 2：显式授权该 Channel 上被本文件用到的
        # 每个会话键（default-deny 前提；不做「授权全部会话」的隐式放宽）
        for conversation in _GRANTED_CONVERSATIONS:
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=channel.id, external_conversation_id=conversation,
                connection_id=conn_a.id))
        uow.commit()
        return {"alice": alice.id, "bob": bob.id, "channel": channel.id,
                "identity_a": id_a.id, "identity_b": id_b.id,
                "agent_a": agent_a.id, "agent_b": agent_b.id,
                "connection_a": conn_a.id}


def _coordinator(db_engine, agent_adapter, channel_adapter) -> RuntimeCoordinator:
    uow = _uow_factory(db_engine)
    return DeliveringCoordinator(RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(agent_adapter=agent_adapter,
                                             uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    ))


def _inbound(channel_id: int, external_id: str = "wx_a",
             conversation: str = "conversation-X",
             content: str = "hello") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id="i-1",
        external_conversation_id=conversation,
        external_identity_id=external_id, content=content)


def _count_sessions(db_engine) -> int:
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(SessionModel)
        ).scalar_one()


def _run_full(db_engine, inbound, agent_adapter=None, channel_adapter=None):
    """Test Channel Input → RuntimeCoordinator → Test Channel Output。"""
    agent_adapter = agent_adapter or _TestAgentAdapter()
    channel_adapter = channel_adapter or _TestChannelAdapter()
    _coordinator(db_engine, agent_adapter, channel_adapter).execute(inbound)
    return agent_adapter, channel_adapter


# ================================================================== E2E ① REUSE_ACTIVE


def test_e2e_reuse_active_full_loop(db_engine) -> None:
    g = _seed(db_engine)
    # 预置 Active Session（等价于此前已跑过一次 CREATE_NEW）
    existing = SessionCreationExecutor(_uow_factory(db_engine)).execute(
        identity_id=g["identity_a"], channel_id=g["channel"],
        connection_id=g["connection_a"], agent_id=g["agent_a"],
        external_conversation_id="conversation-X")
    before = _count_sessions(db_engine)

    inbound = _inbound(g["channel"])
    agent_adapter, channel_adapter = _run_full(db_engine, inbound)

    # REUSE：不新建
    assert _count_sessions(db_engine) == before
    # AgentRequest 复用 existing session
    request = agent_adapter.requests[-1]
    assert request.control_plane_session_id == existing.id
    # 闭环回显
    outbound = channel_adapter.deliveries[-1]
    assert inbound.external_conversation_id == outbound.external_conversation_id
    assert outbound.content == "test response"


# ================================================================== E2E ② CREATE_NEW


def test_e2e_create_new_full_loop(db_engine) -> None:
    g = _seed(db_engine)
    before = _count_sessions(db_engine)

    inbound = _inbound(g["channel"])
    agent_adapter, channel_adapter = _run_full(db_engine, inbound)

    assert _count_sessions(db_engine) == before + 1
    cp_session_id = agent_adapter.requests[-1].control_plane_session_id
    # created session 就是 Dispatch 的 cp session
    with Session(db_engine) as session:
        row = session.get(SessionModel, cp_session_id)
    assert row is not None
    assert row.identity_id == g["identity_a"]
    assert row.agent_id == g["agent_a"]
    # 出口 = 入口（channel_id / conversation 回显）
    outbound = channel_adapter.deliveries[-1]
    assert outbound.channel_id == inbound.channel_id
    assert outbound.external_conversation_id == inbound.external_conversation_id


def test_e2e_message_round_trip_content(db_engine) -> None:
    """input = "hello" → 完整穿过 → 拿到 Agent 的 "test response"。"""
    g = _seed(db_engine)
    agent_adapter, channel_adapter = _run_full(
        db_engine, _inbound(g["channel"], content="hello"))
    assert agent_adapter.requests[-1].content == "hello"
    assert channel_adapter.deliveries[-1].content == "test response"


# ================================================================== 边界闭环


def test_identity_isolation_no_cross_user(db_engine) -> None:
    """channel + wx_a → alice 的 Identity；绝不落到 wx_b（bob）。"""
    g = _seed(db_engine)
    _, channel_adapter = _run_full(db_engine, _inbound(g["channel"], "wx_a", "convA"))
    outbound = channel_adapter.deliveries[-1]
    assert outbound.external_conversation_id == "convA"
    # 会话绑定 identity_a（不是 identity_b）
    with Session(db_engine) as session:
        row = session.execute(
            select(SessionModel).where(
                SessionModel.external_conversation_id == "convA")
        ).scalar_one()
    assert row.identity_id == g["identity_a"]
    assert row.identity_id != g["identity_b"]


def test_routing_isolation_no_cross_agent(db_engine) -> None:
    """Channel A → Connection A → Agent A；绝不变成 Agent B。"""
    g = _seed(db_engine)
    agent_adapter, _ = _run_full(db_engine, _inbound(g["channel"], "wx_a", "convR"))
    request = agent_adapter.requests[-1]
    assert request.agent_id == g["agent_a"]
    assert request.agent_id != g["agent_b"]


def test_session_isolation_by_conversation(db_engine) -> None:
    """Identity A + conversation X ≠ Identity A + conversation Y（两条独立流）。"""
    g = _seed(db_engine)
    _, channel_adapter = _run_full(
        db_engine, _inbound(g["channel"], "wx_a", "conversation-X"))
    _, channel_adapter2 = _run_full(
        db_engine, _inbound(g["channel"], "wx_a", "conversation-Y"))
    cp_ids = {o.external_conversation_id for o in channel_adapter.deliveries}
    cp_ids2 = {o.external_conversation_id for o in channel_adapter2.deliveries}
    assert cp_ids == {"conversation-X"}
    assert cp_ids2 == {"conversation-Y"}
    with Session(db_engine) as session:
        rows = session.execute(select(SessionModel)).scalars().all()
    assert len(rows) == 2
    assert {r.external_conversation_id for r in rows} == {
        "conversation-X", "conversation-Y"}
    assert rows[0].id != rows[1].id


def test_agent_session_reference_not_confused(db_engine) -> None:
    """CP Session id ≠ Agent external_session_id：两者共存但绝不混淆。"""
    g = _seed(db_engine)
    agent_adapter, _ = _run_full(db_engine, _inbound(g["channel"], "wx_a", "convS"))
    request = agent_adapter.requests[-1]
    assert agent_adapter.last_response is not None
    assert isinstance(request.control_plane_session_id, int)
    assert isinstance(agent_adapter.last_response.external_session_id, str)
    assert request.control_plane_session_id != \
        agent_adapter.last_response.external_session_id


def test_outbound_boundary_no_internal_ids(db_engine) -> None:
    """出口只有 channel/conversation/content/metadata/artifacts——不带任何内部 id。

    ACP V0.3 Phase 1：+ `artifacts`（Agent 产出的**引用**）。它携带的是
    Agent 侧引用，**不是** Control Plane 的内部 id —— 故下方的禁带清单不变。
    """
    g = _seed(db_engine)
    _, channel_adapter = _run_full(db_engine, _inbound(g["channel"], "wx_a", "convB"))
    outbound = channel_adapter.deliveries[-1]
    fields = {f.name for f in dataclasses.fields(outbound)}
    assert fields == {"channel_id", "external_conversation_id", "content",
                      "metadata", "artifacts"}
    for banned in ("user_id", "identity_id", "connection_id", "agent_id",
                   "control_plane_session_id"):
        assert banned not in fields, f"出口不应携带 {banned}"


# ================================================================== 失败链（真实执行器）


def test_identity_failure_stops_whole_chain(db_engine) -> None:
    g = _seed(db_engine)
    with pytest.raises(IdentityResolutionFailedError):
        _run_full(db_engine, _inbound(g["channel"], external_id="wx_unknown"))
    # 无任何后继（不碰 Session/Dispatch/Delivery）


def test_routing_failure_stops_whole_chain(db_engine) -> None:
    """★ 决策矩阵第 2 行：**有授权但连接 inactive** → `ROUTING_FAILED`，不是 UNAUTHORIZED。

    Package 2 / Step 2：为了让失败真的来自 Routing，必须显式授权那条 inactive
    Connection——否则会先被 Authorization 拒绝（那验证的是另一条语义）。
    """
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        u = uow.users.create(User(name="carol"))
        ch = uow.channels.create(Channel(name="no-active-1", type="wecom"))
        ag = uow.agents.create(Agent(name="only-b", type="acp"))
        uow.identities.create(Identity(
            user_id=u.id, channel_id=ch.id, external_id="wx_a"))
        path = uow.connections.create(Connection(
            channel_id=ch.id, agent_id=ag.id, name="inactive-path", status="inactive"))
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=ch.id, external_conversation_id="conversation-X",
            connection_id=path.id))
        uow.commit()
        channel_id = ch.id
    with pytest.raises(RoutingFailedError):
        _run_full(db_engine, _inbound(channel_id, "wx_a"))


def test_dispatch_failure_means_no_delivery(db_engine) -> None:
    g = _seed(db_engine)
    with pytest.raises(AgentDispatchFailedError):
        _run_full(db_engine, _inbound(g["channel"]),
                  agent_adapter=_FailingAgentAdapter())
    # NO Delivery（channel 什么都没收到）


def test_delivery_failure_after_dispatch_success(db_engine) -> None:
    g = _seed(db_engine)
    agent_adapter = _TestAgentAdapter()
    with pytest.raises(ChannelDeliveryFailedError):
        _run_full(db_engine, _inbound(g["channel"], "wx_a", "convD"),
                  agent_adapter=agent_adapter, channel_adapter=_FailingChannelAdapter())
    # Dispatch 已成功（请求已发）→ 才进入 Delivery 并失败：processing ≠ delivery 语义
    assert agent_adapter.requests, "Dispatch 应已成功"
    assert agent_adapter.requests[-1].content == "hello"


# ================================================================== 范围边界：不持久化 Interaction


def test_runtime_does_not_use_interaction_or_activity_persistence() -> None:
    """Runtime 组件（coordinator + 六 executor）不触碰 interactions/activities 持久化。"""
    modules = [
        "app/application/coordinator.py", "app/application/identity_resolution.py",
        "app/application/routing.py", "app/application/session_resolution.py",
        "app/application/session_creation.py", "app/application/agent_dispatch.py",
        "app/application/delivery.py",
    ]
    for rel in modules:
        src = (BACKEND_DIR / rel).read_text(encoding="utf-8")
        roots: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                roots.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module)
        assert not any(r.startswith("app.persistence") for r in roots), \
            f"{rel} 不应 import persistence"
        forbidden_names = {"InteractionModel", "ActivityModel",
                           "InteractionRepository", "ActivityRepository",
                           "interactions", "activities"}
        used = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
        assert not (used & forbidden_names), f"{rel} 不应引用 {used & forbidden_names}"
