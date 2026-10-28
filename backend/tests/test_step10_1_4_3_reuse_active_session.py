"""Step 10.1.4.3 —— REUSE_ACTIVE Real Runtime Integration / Session Continuity。

10.1.4.2 已证明真实 Tencent Agent Runtime 的完整链路；本 Step 不再依赖真实
Runtime——要证明的是 **Control Plane 自己的 Session 生命周期与连续性语义**：

    Inbound #1 → CREATE_NEW  → CP Session = S1（物化 1 条）
    Inbound #2 → REUSE_ACTIVE → CP Session = S1（不新建，仍是同一条）

核心不变量：
    同一 Identity + 同一 Connection + 同一 external_conversation_id，
    在 Session 仍 active 时，第二次 Interaction 必须复用第一次创建的 CP Session。

不止看「数据库只有一条 Session」的最终结果，还要**直接锁住 SessionResolution
的决策**：用记录型 SessionResolutionExecutor spy（只记录 + 委托真实执行器），
断言决策序列 === [CREATE_NEW, REUSE_ACTIVE]——这证明系统真的执行了 REUSE_ACTIVE，
而不是测试代码恰好没建第二条。

Conversation 边界（8.3 冻结的可执行化）：
    conversation_001 → S1（msg_001 与 msg_002 都落在 S1）
    conversation_002 → S2（msg_003 → CREATE_NEW）

本 Step 明确不做：Agent 内部 conversation continuity / Agent memory / Agent
context persistence / ACP session lifecycle redesign / Runtime connection cache /
Redis / distributed lock / session locking / retry / failover / load balancing /
Interaction persistence / Activity persistence / 新 Session API。

**CP Session Continuity ≠ Agent Context Continuity**：Agent 是否把两次请求理解成
同一个内部 conversation，是 Agent / ACP Runtime 的职责，不属于本 Step。

生产代码零改动：只用现有 RuntimeCoordinator + 六 Executor + 现有 DB/UoW/
Repository；Agent/Channel 用 Test Adapter（与 9.8.3 同底座）。
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
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
from app.domain.session_resolution import CREATE_NEW, REUSE_ACTIVE
from app.domain.user import User
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.delivery_lifecycle_helper import DeliveringCoordinator


# ------------------------------------------------------------------ 测试底座


class _TestAgentAdapter:
    """记录每次 AgentRequest（含 control_plane_session_id），返回真实格式响应。"""

    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []
        self._n = 0

    def send(self, request: AgentRequest, connection: Connection) -> AgentResponse:
        self.requests.append(request)
        self._n += 1
        return AgentResponse(content=f"ack:{self._n}",
                             external_session_id=f"agent-s-{self._n}")


class _TestChannelAdapter:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


class _RecordingSessionResolution(SessionResolutionExecutor):
    """spy：记录每次 SessionResolution 决策，但完全委托真实执行器（不改变语义）。"""

    def __init__(self, uow_factory) -> None:
        super().__init__(uow_factory)
        self.decisions: list[str] = []

    def execute(self, identity_id, connection_id, external_conversation_id):
        decision = super().execute(
            identity_id, connection_id, external_conversation_id)
        self.decisions.append(decision.mode)
        return decision


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))


def _seed(db_engine) -> dict[str, int]:
    """alice(wx_a) @ wecom-1；唯一 active Connection → agent-a。"""
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        identity = uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        conn = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a"))
        # Implementation Package 2 / Step 2：显式授权本文件用到的每个会话键
        for conversation in ("conversation_001", "conversation_002"):
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=channel.id, external_conversation_id=conversation,
                connection_id=conn.id))
        uow.commit()
        return {"channel": channel.id, "identity": identity.id,
                "agent": agent.id, "connection": conn.id}


def _coordinator(db_engine, session_resolution, agent_adapter, channel_adapter) -> RuntimeCoordinator:
    uow = _uow_factory(db_engine)
    return DeliveringCoordinator(RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=session_resolution,
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(agent_adapter=agent_adapter,
                                             uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    ))


def _inbound(channel_id: int, conversation: str, interaction_id: str,
             content: str) -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id=interaction_id,
        external_conversation_id=conversation,
        external_identity_id="wx_a", content=content)


def _count_sessions(db_engine) -> int:
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(SessionModel)).scalar_one()


def _session_rows(db_engine) -> list[SessionModel]:
    with Session(db_engine) as session:
        return session.execute(
            select(SessionModel).order_by(SessionModel.id)).scalars().all()


# ================================================================== Case 1 ——
# First interaction creates session, Second reuses it


def test_case1_first_create_new_then_second_reuse_active(db_engine) -> None:
    """msg_001 → CREATE_NEW（S1）；msg_002（同 conversation）→ REUSE_ACTIVE（S1）。

    锁住 SessionResolution 决策序列 === [CREATE_NEW, REUSE_ACTIVE]，而不是只看
    「数据库只剩一条 Session」的最终结果。
    """
    g = _seed(db_engine)
    resolution = _RecordingSessionResolution(_uow_factory(db_engine))
    agent_adapter = _TestAgentAdapter()
    channel_adapter = _TestChannelAdapter()
    coordinator = _coordinator(db_engine, resolution, agent_adapter,
                               channel_adapter)

    # --- Interaction #1: msg_001 ---
    coordinator.execute(_inbound(
        g["channel"], "conversation_001", "msg_001", content="hello"))

    # 断言 #1：success（未抛错）、delivery 1 次、CP Session 创建且 active
    assert len(channel_adapter.deliveries) == 1
    assert _count_sessions(db_engine) == 1
    rows_after_first = _session_rows(db_engine)
    assert rows_after_first[0].status == "active"
    assert rows_after_first[0].external_conversation_id == "conversation_001"
    session_1_id = rows_after_first[0].id
    # Dispatch #1 携带的就是刚创建的 CP Session
    assert agent_adapter.requests[0].control_plane_session_id == session_1_id

    # --- Interaction #2: msg_002（同 identity+connection+conversation）---
    coordinator.execute(_inbound(
        g["channel"], "conversation_001", "msg_002", content="continue"))

    # 断言 #2：delivery 2 次、Session 仍只有 1 条、两次是同一 id
    assert len(channel_adapter.deliveries) == 2
    assert _count_sessions(db_engine) == 1
    assert _session_rows(db_engine)[0].id == session_1_id
    # session_id(first) == session_id(second) —— 落在 Dispatch 的 cp_session_id 上
    assert agent_adapter.requests[1].control_plane_session_id == session_1_id

    # --- 锁决策：不是「恰好没建第二条」，而是系统真执行了 REUSE_ACTIVE ---
    assert resolution.decisions == [CREATE_NEW, REUSE_ACTIVE]

    # --- 两次 Dispatch 都发生、两次 Delivery 各成功一次 ---
    assert len(agent_adapter.requests) == 2
    assert [d.content for d in channel_adapter.deliveries] == ["ack:1", "ack:2"]
    # 同 external_conversation_id 被保留（出口回显）
    assert {d.external_conversation_id
            for d in channel_adapter.deliveries} == {"conversation_001"}


# ================================================================== Case 2 ——
# Different conversation creates a new session


def test_case2_different_conversation_creates_new_session(db_engine) -> None:
    """conversation_001 → S1（两条 msg 都复用）；conversation_002 → CREATE_NEW → S2。

    把 8.3 冻结的 Conversation 边界变成可执行测试：
        conversation_001 + msg_001 → S1
        conversation_001 + msg_002 → S1（REUSE_ACTIVE）
        conversation_002 + msg_003 → S2（CREATE_NEW）
    """
    g = _seed(db_engine)
    resolution = _RecordingSessionResolution(_uow_factory(db_engine))
    agent_adapter = _TestAgentAdapter()
    channel_adapter = _TestChannelAdapter()
    coordinator = _coordinator(db_engine, resolution, agent_adapter,
                               channel_adapter)

    # msg_001 → conversation_001
    coordinator.execute(_inbound(
        g["channel"], "conversation_001", "msg_001", content="hello"))
    # msg_002 → conversation_001（复用）
    coordinator.execute(_inbound(
        g["channel"], "conversation_001", "msg_002", content="continue"))
    # msg_003 → conversation_002（新会话）
    coordinator.execute(_inbound(
        g["channel"], "conversation_002", "msg_003", content="new"))

    rows = _session_rows(db_engine)
    # 两条 Session：S1(conversation_001)、S2(conversation_002)
    assert len(rows) == 2
    assert {r.external_conversation_id for r in rows} == {
        "conversation_001", "conversation_002"}
    s1 = next(r for r in rows if r.external_conversation_id == "conversation_001")
    s2 = next(r for r in rows if r.external_conversation_id == "conversation_002")
    assert s1.id != s2.id

    # 决策序列：前两个同 conversation → CREATE_NEW 后 REUSE_ACTIVE；
    # 换 conversation → 再次 CREATE_NEW
    assert resolution.decisions == [CREATE_NEW, REUSE_ACTIVE, CREATE_NEW]

    # 三次 Dispatch 都发生，各自带所属 CP Session
    assert len(agent_adapter.requests) == 3
    assert agent_adapter.requests[0].control_plane_session_id == s1.id
    assert agent_adapter.requests[1].control_plane_session_id == s1.id
    assert agent_adapter.requests[2].control_plane_session_id == s2.id

    # 三次 Delivery 各成功一次，conversation 回显正确
    assert len(channel_adapter.deliveries) == 3
    assert [d.content for d in channel_adapter.deliveries] == ["ack:1", "ack:2",
                                                               "ack:3"]
    assert [d.external_conversation_id for d in channel_adapter.deliveries] == [
        "conversation_001", "conversation_001", "conversation_002"]
