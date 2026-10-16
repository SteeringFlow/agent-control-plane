"""Step 10.4.2 —— Runtime Interaction Persistence Vertical Slice。

把 10.4.1 的审计结论变成运行时事实：一次完整 Runtime 执行产生一条
Interaction + 一组 Activity，并正确记录成功 / 执行失败 / Delivery 失败。

实验变量：PersistingCoordinator（10.4.1 裁决挂点：Runtime 调用边界的
Protocol 装饰器）+ 既有 RuntimeCoordinator（零改动）+ 真实 ACPAdapter /
echo Agent / recording Channel Adapter。

验收（10.4.2 §八，17 条全部落地为断言）。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import sessionmaker

from app.adapters.agents.acp import ACPAdapter
from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.errors import (
    AccessAuthorizationDeniedError,
    AgentDispatchFailedError,
    AgentResponseInvalidError,
    ApplicationError,
    ChannelDeliveryFailedError,
    IdentityResolutionFailedError,
    RoutingFailedError,
    SessionResolutionFailedError,
)
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import (
    STAGE_FAILURE_MAP,
    PersistingCoordinator,
)
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.agent import Agent
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session import Session
from app.domain.session_resolution import REUSE_ACTIVE
from app.domain.user import User
from app.persistence.models.activity import ActivityModel
from app.persistence.models.interaction import InteractionModel
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
DEAD_ENDPOINT = "http://127.0.0.1:1"  # 无监听端口 → transport 失败


# ================================================================== 测试底座


class _EchoAgentAdapter:
    def send(self, request, connection) -> AgentResponse:
        return AgentResponse(content=f"echo:{request.content}",
                             external_session_id="agent-session-1")


class _FailingChannelAdapter:
    """Delivery 必失败（transport 语义）——验证 delivery failure ≠ processing failure。"""

    def deliver(self, outbound: OutboundResponse) -> None:
        raise ChannelDeliveryFailedError(
            channel_id=outbound.channel_id,
            external_conversation_id=outbound.external_conversation_id)


class _RecordingChannelAdapter:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine, endpoint: str | None = None,
          with_connection: bool = True, status: str = "active") -> dict[str, int]:
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        if with_connection:
            path = uow.connections.create(Connection(
                channel_id=channel.id, agent_id=agent.id, name="path-a",
                status=status,
                config={"endpoint": endpoint or "http://agent.internal",
                        "timeout_s": 5.0},
                credential_ref="cred-a"))
            # Implementation Package 2 / Step 2：显式授权该会话（default-deny 前提）
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=channel.id, external_conversation_id="user-A",
                connection_id=path.id))
        uow.commit()
        return {"channel": channel.id, "agent": agent.id}


def _wrapped_runtime(db_engine, agent_adapter=None, channel_adapter=None):
    """既有 RuntimeCoordinator（零改动）包上 PersistingCoordinator。"""
    uow = _uow_factory(db_engine)
    inner = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=agent_adapter or _EchoAgentAdapter(),
            uow_factory=uow),
        delivery=DeliveryExecutor(
            channel_adapter=channel_adapter or _RecordingChannelAdapter()),
    )
    return PersistingCoordinator(inner=inner, uow_factory=_uow_factory(db_engine))


def _inbound(channel_id: int, interaction: str = "i-1",
             conversation: str = "user-A", identity: str = "wx_a",
             content: str = "hello") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id=interaction,
        external_conversation_id=conversation,
        external_identity_id=identity, content=content)


def _interaction_rows(db_engine) -> list[InteractionModel]:
    from sqlalchemy.orm import Session as SASession
    with SASession(db_engine) as s:
        return list(s.execute(select(InteractionModel)
                              .order_by(InteractionModel.id)).scalars().all())


def _activity_rows(db_engine, interaction_id: int | None = None) -> list[ActivityModel]:
    from sqlalchemy.orm import Session as SASession
    with SASession(db_engine) as s:
        stmt = select(ActivityModel).order_by(ActivityModel.id)
        if interaction_id is not None:
            stmt = stmt.where(ActivityModel.interaction_id == interaction_id)
        return list(s.execute(stmt).scalars().all())


def _session_rows(db_engine) -> list[SessionModel]:
    from sqlalchemy.orm import Session as SASession
    with SASession(db_engine) as s:
        return list(s.execute(select(SessionModel)).scalars().all())


# ================================================================== A. Wrapper 单元


def test_a1_lifecycle_pending_to_processing_observable(db_engine) -> None:
    """生命周期可观察：inner 执行中 interaction 已是 PROCESSING
    （PENDING → PROCESSING → 终态的真实跃迁，非一锤子写入）。

    （10.8.3/10.8.4 翻转：Runtime 边界现返回 RuntimeExecutionResult——桩
    inner 也必须给出合法结果，否则 wrapper 的成功记录无从下手。）"""
    g = _seed(db_engine)
    with _uow_factory(db_engine)() as uow:
        ident = uow.identities.find_by_channel_and_external_id(
            g["channel"], "wx_a")
        session = uow.sessions.create(Session(
            identity_id=ident.id, channel_id=g["channel"], connection_id=None,
            agent_id=g["agent"], external_conversation_id="user-A"))
        uow.commit()
    observed = {}

    class _ProbingInner:
        def execute(self, inbound) -> RuntimeExecutionResult:
            rows = _interaction_rows(db_engine)
            observed["status_inside"] = rows[-1].processing_status
            return RuntimeExecutionResult(
                session_mode=REUSE_ACTIVE, session_id=session.id,
                agent_response=AgentResponse(content="echo"))

    PersistingCoordinator(
        inner=_ProbingInner(), uow_factory=_uow_factory(db_engine),
    ).execute(_inbound(g["channel"]))

    assert observed["status_inside"] == "processing"
    row = _interaction_rows(db_engine)[-1]
    assert row.processing_status == "succeeded"


def test_a2_failure_map_covers_all_runtime_typed_errors() -> None:
    """六类 Runtime typed error 全覆盖；stage 值覆盖五段
    （session_creation 由成功路径 Evidence 覆盖，见 B1）。"""
    assert set(STAGE_FAILURE_MAP) == {
        IdentityResolutionFailedError, AccessAuthorizationDeniedError,
        RoutingFailedError,
        SessionResolutionFailedError, AgentDispatchFailedError,
        AgentResponseInvalidError, ChannelDeliveryFailedError,
    }
    stages = {v[0] for v in STAGE_FAILURE_MAP.values()}
    assert stages == {"identity_resolution", "authorization", "routing",
                      "session_resolution",
                      "agent_dispatch", "delivery"}
    # retryable 语义：transport/timeout 类 True；契约/配置类 False
    assert STAGE_FAILURE_MAP[AgentDispatchFailedError][2] is True
    assert STAGE_FAILURE_MAP[ChannelDeliveryFailedError][2] is True
    assert STAGE_FAILURE_MAP[RoutingFailedError][2] is False
    assert STAGE_FAILURE_MAP[AgentResponseInvalidError][2] is False


# ================================================================== B. 全链验收


def test_b1_success_create_new_records_interaction_and_evidence(db_engine) -> None:
    """成功 + CREATE_NEW：终态正确、session FK 回填、session_creation Evidence。"""
    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(_inbound(g["channel"], "i-1"))

    sessions = _session_rows(db_engine)
    assert len(sessions) == 1
    rows = _interaction_rows(db_engine)
    assert len(rows) == 1
    row = rows[0]
    assert row.processing_status == "succeeded"
    assert row.delivery_status == "delivered"
    assert row.session_id == sessions[0].id          # Interaction ↔ Session FK 正确
    assert row.failure_stage is None and row.failure_code is None
    assert row.retryable is False
    assert row.direction == "inbound"
    assert row.external_interaction_id == "i-1"
    assert row.request_payload["content"] == "hello"

    acts = _activity_rows(db_engine, row.id)
    types = [(a.type, a.stage, a.status) for a in acts]
    assert ("interaction_received", None, "recorded") in types
    assert ("session_created", "session_creation", "succeeded") in types  # CREATE_NEW Evidence
    assert ("interaction_completed", "delivery", "succeeded") in types
    created = [a for a in acts if a.type == "session_created"][0]
    assert created.extra_metadata == {"session_id": sessions[0].id}


def test_b2_reuse_active_produces_no_session_creation_evidence(db_engine) -> None:
    """REUSE_ACTIVE 不凭空制造 session_creation Evidence（Evidence = 事实）。"""
    g = _seed(db_engine)
    runtime = _wrapped_runtime(db_engine)
    runtime.execute(_inbound(g["channel"], "i-1"))
    runtime.execute(_inbound(g["channel"], "i-2"))          # 同 identity+conversation

    assert len(_session_rows(db_engine)) == 1               # REUSE：不重复创建
    rows = _interaction_rows(db_engine)
    assert len(rows) == 2
    second = rows[-1]
    assert second.processing_status == "succeeded"
    assert second.session_id == _session_rows(db_engine)[0].id
    second_acts = _activity_rows(db_engine, second.id)
    assert all(a.type != "session_created" for a in second_acts)  # 无虚假 Evidence
    assert any(a.type == "interaction_completed" for a in second_acts)


def test_b3_identity_failure_recorded(db_engine) -> None:
    """Identity 失败：FAILED + identity_resolution + delivery=PENDING + 无 session。"""
    g = _seed(db_engine)
    with pytest.raises(IdentityResolutionFailedError):
        _wrapped_runtime(db_engine).execute(
            _inbound(g["channel"], identity="wx_unknown"))

    row = _interaction_rows(db_engine)[0]
    assert row.processing_status == "failed"
    assert row.failure_stage == "identity_resolution"
    assert row.failure_code == "IDENTITY_RESOLUTION_FAILED"
    assert row.retryable is False
    assert row.delivery_status == "pending"
    assert row.session_id is None                            # Session 建立前失败
    acts = _activity_rows(db_engine, row.id)
    failure = [a for a in acts if a.type == "stage_failure"][0]
    assert failure.stage == "identity_resolution"
    assert failure.status == "failed"
    assert failure.extra_metadata == {"failure_code": "IDENTITY_RESOLUTION_FAILED",
                                      "retryable": False}


def test_b4_routing_failure_recorded(db_engine) -> None:
    """Routing 失败（**有授权但无 active Connection**）：FAILED + routing + ROUTING_FAILED。

    Package 2 / Step 2 修订：原用例用「没有任何 Connection」制造 Routing 失败，
    但现在那会先被 Authorization 拒绝（`UNAUTHORIZED`）——失败点变了，语义也就变了。
    要验证「Routing 失败的落库形状」，必须让授权成立、路径不可用：
    授权一条 **inactive** Connection（决策矩阵第 2 行）。
    """
    g = _seed(db_engine, status="inactive")
    with pytest.raises(RoutingFailedError):
        _wrapped_runtime(db_engine).execute(_inbound(g["channel"]))

    row = _interaction_rows(db_engine)[0]
    assert row.processing_status == "failed"
    assert row.failure_stage == "routing"
    assert row.failure_code == "ROUTING_FAILED"
    assert row.delivery_status == "pending"
    assert row.session_id is None


def test_b5_dispatch_failure_recorded_with_session_link(db_engine) -> None:
    """Dispatch 失败：FAILED + agent_dispatch + AGENT_DISPATCH_FAILED +
    retryable=True；Session 已建立 → interaction 回填 session_id。"""
    g = _seed(db_engine, endpoint=DEAD_ENDPOINT)
    with pytest.raises(AgentDispatchFailedError):
        _wrapped_runtime(db_engine, agent_adapter=ACPAdapter()).execute(
            _inbound(g["channel"]))

    row = _interaction_rows(db_engine)[0]
    assert row.processing_status == "failed"
    assert row.failure_stage == "agent_dispatch"
    assert row.failure_code == "AGENT_DISPATCH_FAILED"
    assert row.retryable is True
    assert row.delivery_status == "pending"
    assert row.session_id is not None                        # 已到达 Session 之后
    assert len(_session_rows(db_engine)) == 1


def test_b6_delivery_failure_is_not_processing_failure(db_engine) -> None:
    """核心区分：Agent 成功 + Delivery 失败 →
    processing=SUCCEEDED + delivery=FAILED + failure_stage=delivery。"""
    g = _seed(db_engine)
    with pytest.raises(ChannelDeliveryFailedError):
        _wrapped_runtime(
            db_engine, channel_adapter=_FailingChannelAdapter(),
        ).execute(_inbound(g["channel"]))

    row = _interaction_rows(db_engine)[0]
    assert row.processing_status == "succeeded"              # Agent 已成功
    assert row.delivery_status == "failed"                   # 渠道投递失败
    assert row.failure_stage == "delivery"
    assert row.failure_code == "CHANNEL_DELIVERY_FAILED"
    assert row.retryable is True
    assert row.session_id is not None


def test_b7_activity_is_append_only() -> None:
    """append-only：schema 无 updated_at；Adapter 无 update/delete 语义。"""
    cols = {c.name for c in inspect(ActivityModel).columns}
    assert "updated_at" not in cols
    for rel in ("app/persistence/repositories/activity_record.py",
                "app/repositories/activity_record.py"):
        src = (BACKEND / rel).read_text(encoding="utf-8")
        for forbidden in ("def update", "def delete", "def save"):
            assert forbidden not in src, f"{rel} 出现 {forbidden}"


def test_b8_runtime_boundary_unaware_of_wrapper() -> None:
    """Runtime Core 零感知：coordinator/六 executor 不 import wrapper 与记录
    仓库——persistence 完全留在调用边界（10.4.1 裁决的运行时事实）。

    AST import 检查（docstring 中的边界解释文字不算）。"""
    guarded = ("app/application/coordinator.py", "app/application/runtime.py",
               "app/application/identity_resolution.py",
               "app/application/routing.py",
               "app/application/session_resolution.py",
               "app/application/session_creation.py",
               "app/application/agent_dispatch.py",
               "app/application/delivery.py")
    forbidden_roots = ("app.persistence", "app.repositories.interaction_record",
                       "app.repositories.activity_record",
                       "app.application.interaction_recording")
    for rel in guarded:
        tree = ast.parse((BACKEND / rel).read_text(encoding="utf-8"),
                         filename=rel)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module)
        hit = [r for r in roots
               if any(r == f or r.startswith(f + ".") for f in forbidden_roots)]
        assert not hit, f"{rel} 触碰持久化边界：{hit}"


def test_b9_wrapper_composes_with_inbound_service(db_engine) -> None:
    """装配点验证（12.9.4.6）：persistence phase API 作为 **intake** 传入
    ChannelInboundService（只入队），Worker 承接执行——webhook 链路同样
    产生 Interaction 记录（终态由 Worker 落定）。"""
    from app.application.channel_inbound import ChannelInboundService
    from app.application.worker import Worker
    from app.repositories.channel_inbound import InboundChannelAdapter

    g = _seed(db_engine)
    uow = _uow_factory(db_engine)
    runtime = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=_EchoAgentAdapter(), uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=_RecordingChannelAdapter()),
    )
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=uow)
    service = ChannelInboundService(
        uow_factory=uow, intake=phases,
        inbound_adapter=_StaticInboundAdapter(g["channel"]))

    interaction_id = service.handle_message(
        g["channel"], signature="s", timestamp="t", nonce="n", body="b")
    assert interaction_id is not None
    assert worker.process(interaction_id) is True
    rows = _interaction_rows(db_engine)
    assert len(rows) == 1
    assert rows[0].processing_status == "succeeded"


class _StaticInboundAdapter:
    """最小 InboundChannelAdapter 形状（装配点验证用，非 wire 测试）。"""

    def __init__(self, channel_id: int) -> None:
        self._channel_id = channel_id

    def verify_handshake(self, channel, **kwargs) -> str:  # noqa: ARG002
        return "echostr"

    def parse_message(self, channel, **kwargs):  # noqa: ARG002
        return InboundInteraction(
            channel_id=self._channel_id, external_interaction_id="webhook-1",
            external_conversation_id="user-A",
            external_identity_id="wx_a", content="via-webhook")


# 防未使用 import 误删（ApplicationError 供读者理解 wrapper 的异常契约）
_ = ApplicationError
_ = SessionResolutionFailedError
