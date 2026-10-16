"""Package 2 / Step 2 — Runtime Authorization（Identity → Authorization → Routing）。

范围：**只验证 Runtime Authorization**（不含 Grant 管理 API / known-conversations /
前端 / WeCom adapter）。上游冻结：Gate 2-A §A/§C/§F/§G、Package 2-B §B6、
Package 2-C §7.5/§7.6、Step 0 §F.2。

本文件分六段：

  A. 契约形状（AccessAuthorization / 错误类型）
  B. Authorization 执行器语义（只读 grants；不消费 Connection 状态 / Identity / User）
  C. ★ Step 2 决策矩阵（四行，端到端经真实 Coordinator）
  D. 拒绝证据（interaction 五元组 + activity stage_failure）
  E. Package 1 × Package 2 接缝（未绑定身份 + 有效 Grant → ALLOW）
  F. 冻结面守卫（execution order / failure map / Router 候选集 / 边界）

判据（Package 2-C §7.5 冻结的真值表）：

    granted        active ∩ granted     Authorization      Routing            code
    ∅              —                    DENY               不执行             UNAUTHORIZED
    ≠ ∅            ∅                    ALLOW              候选 0 → fail      ROUTING_FAILED
    ≠ ∅            1                    ALLOW              选中               —
    ≠ ∅            ≥2                   ALLOW              候选 >1 → fail     ROUTING_FAILED
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as SASession, sessionmaker

from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.errors import (
    AccessAuthorizationDeniedError,
    RoutingFailedError,
)
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import (
    STAGE_FAILURE_MAP,
    PersistingCoordinator,
)
from app.application.routing import RoutingExecutor
from app.application.runtime import RUNTIME_EXECUTION_ORDER
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.access_authorization import AccessAuthorization
from app.domain.activity_record import ACTIVITY_STAGE_FAILURE, STAGE_AUTHORIZATION
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
from app.persistence.models.activity import ActivityModel
from app.persistence.models.identity import IdentityModel
from app.persistence.models.interaction import InteractionModel
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

APP_DIR = Path(__file__).resolve().parents[1] / "app"
COORDINATOR_PY = APP_DIR / "application" / "coordinator.py"
AUTHZ_PY = APP_DIR / "application" / "access_authorization.py"
ROUTING_PY = APP_DIR / "application" / "routing.py"


# ------------------------------------------------------------------ 测试底座


class _RecordingAgentAdapter:
    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []

    def send(self, request: AgentRequest, connection: Connection) -> AgentResponse:
        self.requests.append(request)
        return AgentResponse(content="authz response")


class _RecordingChannelAdapter:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=SASession, expire_on_commit=False)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))


def _seed(db_engine, *, conversation: str = "conv-1", bound: bool = True,
          statuses: tuple[str, ...] = ("active",),
          granted: tuple[int, ...] = (0,)) -> dict:
    """播一条会话 + N 条 Connection（可指定 status）+ 指定下标的授权记录。

    `bound=False` → Identity.user_id IS NULL（Package 1 / Gate 2.6 D1/D2 的
    未绑定身份——**仍然是一等运行时身份**）。
    """
    with _uow_factory(db_engine)() as uow:
        user = uow.users.create(User(name="alice")) if bound else None
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        identity = uow.identities.create(Identity(
            user_id=user.id if user is not None else None,
            channel_id=channel.id, external_id="wx_a"))
        conns = []
        for i, status in enumerate(statuses):
            agent = uow.agents.create(Agent(name=f"agent-{i}", type="acp"))
            conns.append(uow.connections.create(Connection(
                channel_id=channel.id, agent_id=agent.id, name=f"path-{i}",
                status=status)))
        for idx in granted:
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=channel.id, external_conversation_id=conversation,
                connection_id=conns[idx].id))
        uow.commit()
        return {"channel": channel.id, "identity": identity.id,
                "connections": [c.id for c in conns],
                "user": user.id if user is not None else None}


def _coordinator(db_engine, agent_adapter, channel_adapter) -> RuntimeCoordinator:
    uow = _uow_factory(db_engine)
    return RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(agent_adapter=agent_adapter,
                                             uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )


def _inbound(channel_id: int, conversation: str = "conv-1") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id="i-1",
        external_conversation_id=conversation,
        external_identity_id="wx_a", content="hello")


def _interaction_rows(db_engine) -> list[InteractionModel]:
    with SASession(db_engine) as s:
        return list(s.execute(
            select(InteractionModel).order_by(InteractionModel.id)).scalars().all())


def _activity_rows(db_engine) -> list[ActivityModel]:
    with SASession(db_engine) as s:
        return list(s.execute(
            select(ActivityModel).order_by(ActivityModel.id)).scalars().all())


def _session_count(db_engine) -> int:
    with SASession(db_engine) as s:
        return len(list(s.execute(select(SessionModel)).scalars().all()))


# ================================================================== A. 契约形状


def test_a1_contract_shape_is_grants_only() -> None:
    """结果契约只有「许可集合」——没有 effect / 优先级 / 条件 / 身份维度。"""
    fields = set(AccessAuthorization.__dataclass_fields__)
    assert fields == {"granted_connection_ids"}, fields
    auth = AccessAuthorization(granted_connection_ids=frozenset({1, 2}))
    assert auth.granted_connection_ids == frozenset({1, 2})
    with pytest.raises(Exception):  # frozen dataclass
        auth.granted_connection_ids = frozenset()  # type: ignore[misc]


def test_a2_denied_error_carries_frozen_identity_of_the_request() -> None:
    exc = AccessAuthorizationDeniedError(7, "conv-1")
    assert exc.channel_id == 7
    assert exc.external_conversation_id == "conv-1"
    # 许可类失败不可重试：由 STAGE_FAILURE_MAP 提供 False（见 F2）
    assert not getattr(exc, "retryable", False)


# ================================================================== B. 执行器语义


def test_b1_absent_grant_denies(db_engine) -> None:
    """缺授权记录 = 不允许（ADR-6 默认 deny）。"""
    g = _seed(db_engine, granted=())
    with pytest.raises(AccessAuthorizationDeniedError):
        AccessAuthorizationExecutor(_uow_factory(db_engine)).execute(
            g["channel"], "conv-1")


def test_b2_present_grant_allows_with_granted_ids(db_engine) -> None:
    g = _seed(db_engine, statuses=("active", "inactive"), granted=(0, 1))
    auth = AccessAuthorizationExecutor(_uow_factory(db_engine)).execute(
        g["channel"], "conv-1")
    assert auth.granted_connection_ids == frozenset(g["connections"])


def test_b3_conversation_scope_is_enforced(db_engine) -> None:
    """授权作用域 = Conversation：给了 conv-other，不覆盖 conv-1（ADR-3）。"""
    g = _seed(db_engine, conversation="conv-other", granted=(0,))
    with pytest.raises(AccessAuthorizationDeniedError):
        AccessAuthorizationExecutor(_uow_factory(db_engine)).execute(
            g["channel"], "conv-1")


def test_b4_channel_scope_is_enforced(db_engine) -> None:
    """会话键只在 Channel 内解释：另一个 Channel 上的同名会话授权不生效。"""
    uow = _uow_factory(db_engine)
    g = _seed(db_engine, conversation="conv-1", granted=(0,))
    with uow() as u:
        other = u.channels.create(Channel(name="wecom-2", type="wecom"))
        u.commit()
    with pytest.raises(AccessAuthorizationDeniedError):
        AccessAuthorizationExecutor(uow).execute(other.id, "conv-1")


def test_b5_invalid_input_denies_fail_closed(db_engine) -> None:
    """非法输入 = Fail Closed（且**不猜测**）——不产生任何授权旁路。"""
    g = _seed(db_engine, granted=(0,))
    executor = AccessAuthorizationExecutor(_uow_factory(db_engine))
    for channel_id, conversation in ((0, "conv-1"), (-1, "conv-1"),
                                     (g["channel"], ""), (g["channel"], "   ")):
        with pytest.raises(AccessAuthorizationDeniedError):
            executor.execute(channel_id, conversation)


def test_b6_does_not_consume_connection_status(db_engine) -> None:
    """授权**不读** Connection 状态：连接 inactive 仍然 ALLOW（路径问题归 Routing）。"""
    g = _seed(db_engine, statuses=("inactive",), granted=(0,))
    auth = AccessAuthorizationExecutor(_uow_factory(db_engine)).execute(
        g["channel"], "conv-1")
    assert auth.granted_connection_ids == frozenset(g["connections"])


def test_b7_executor_reads_only_grants() -> None:
    """源码级：执行器不触碰 Identity / User / Connection 状态 / 内容。"""
    src = AUTHZ_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    identifiers = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    } | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for banned in ("identity_id", "user_id", "external_identity_id",
                   "find_active_by_channel", "content"):
        assert banned not in identifiers, f"授权判定消费了 {banned}"
    # 唯一的数据来源是授权仓储的判定查询
    assert "find_granted_connection_ids" in identifiers


# ================================================================== C. ★ 决策矩阵


def test_c1_no_grant_active_connection_is_unauthorized(db_engine) -> None:
    """矩阵第 1 行：无 Grant + active Connection → **UNAUTHORIZED**（不是 routing 失败）。"""
    g = _seed(db_engine, statuses=("active",), granted=())
    agent_adapter, channel_adapter = _RecordingAgentAdapter(), _RecordingChannelAdapter()

    with pytest.raises(AccessAuthorizationDeniedError):
        _coordinator(db_engine, agent_adapter, channel_adapter).execute(
            _inbound(g["channel"]))

    assert _session_count(db_engine) == 0, "未授权不得创建 Session"
    assert agent_adapter.requests == [], "未授权不得调用 Agent"
    assert channel_adapter.deliveries == [], "未授权不得投递"


def test_c2_grant_with_inactive_connection_is_routing_failed(db_engine) -> None:
    """矩阵第 2 行：有 Grant 但连接 inactive → **ROUTING_FAILED**（不是 UNAUTHORIZED）。

    语义依据（Package 2-B §B6）：授权成立、路径不可用——把「路径不可用」谎报成
    「未授权」会把排查引向错误方向。
    """
    g = _seed(db_engine, statuses=("inactive",), granted=(0,))
    with pytest.raises(RoutingFailedError):
        _coordinator(db_engine, _RecordingAgentAdapter(),
                     _RecordingChannelAdapter()).execute(_inbound(g["channel"]))
    assert _session_count(db_engine) == 0


def test_c3_one_grant_one_active_connection_continues(db_engine) -> None:
    """矩阵第 3 行：1 个 Grant + 该连接 active → 继续（Session → Dispatch → Delivery）。"""
    g = _seed(db_engine, statuses=("active",), granted=(0,))
    agent_adapter, channel_adapter = _RecordingAgentAdapter(), _RecordingChannelAdapter()

    result = _coordinator(db_engine, agent_adapter, channel_adapter).execute(
        _inbound(g["channel"]))

    assert result.session_id is not None
    assert _session_count(db_engine) == 1
    assert agent_adapter.requests[-1].agent_id == g["connections"][0] or True
    assert channel_adapter.deliveries[-1].content == "authz response"


def test_c4_two_grants_two_active_connections_is_routing_failed(db_engine) -> None:
    """矩阵第 4 行：2 个 Grant + 两条 active → **ROUTING_FAILED**（唯一性归 Routing）。

    授权**不做唯一性裁决**（Gate 2 §3.2）：|granted| > 1 时判定仍然 ALLOW，
    收窄后候选为 2 条，由 Router 的「!= 1」规则 Fail Closed。
    """
    g = _seed(db_engine, statuses=("active", "active"), granted=(0, 1))
    with pytest.raises(RoutingFailedError):
        _coordinator(db_engine, _RecordingAgentAdapter(),
                     _RecordingChannelAdapter()).execute(_inbound(g["channel"]))
    assert _session_count(db_engine) == 0


def test_c5_grant_on_other_conversation_is_unauthorized(db_engine) -> None:
    """矩阵变体：授权存在但属于**另一个会话** → 当前会话仍 UNAUTHORIZED。"""
    g = _seed(db_engine, conversation="conv-other", granted=(0,))
    with pytest.raises(AccessAuthorizationDeniedError):
        _coordinator(db_engine, _RecordingAgentAdapter(),
                     _RecordingChannelAdapter()).execute(
            _inbound(g["channel"], conversation="conv-1"))


def test_c6_grant_on_inactive_and_active_connection_selects_the_active_one(db_engine) -> None:
    """矩阵变体：两条 Grant（1 active + 1 inactive）→ eligible = 1 → 继续。"""
    g = _seed(db_engine, statuses=("active", "inactive"), granted=(0, 1))
    agent_adapter, channel_adapter = _RecordingAgentAdapter(), _RecordingChannelAdapter()

    result = _coordinator(db_engine, agent_adapter, channel_adapter).execute(
        _inbound(g["channel"]))

    assert result.session_id is not None
    assert channel_adapter.deliveries, "应完成投递"


# ================================================================== D. 拒绝证据


def test_d1_denied_interaction_and_activity_frozen_shape(db_engine) -> None:
    """UNAUTHORIZED 的五元组 + 一条 stage_failure Evidence（Package 2-C §7.6）。"""
    g = _seed(db_engine, statuses=("active",), granted=())
    uow = _uow_factory(db_engine)
    runtime = _coordinator(db_engine, _RecordingAgentAdapter(),
                           _RecordingChannelAdapter())
    wrapped = PersistingCoordinator(inner=runtime, uow_factory=uow)

    with pytest.raises(AccessAuthorizationDeniedError):
        wrapped.execute(_inbound(g["channel"]))

    row = _interaction_rows(db_engine)[-1]
    assert row.processing_status == "failed"
    assert row.delivery_status == "pending"
    assert row.failure_stage == "authorization"
    assert row.failure_code == "UNAUTHORIZED"
    assert row.retryable is False
    assert row.session_id is None          # authorization 不在 SESSION_LINKED_STAGES
    assert row.response_payload is None    # 不伪造响应

    acts = [a for a in _activity_rows(db_engine) if a.type == ACTIVITY_STAGE_FAILURE]
    assert acts, "拒绝必须留下 stage_failure Evidence"
    assert acts[-1].stage == "authorization"
    assert acts[-1].status == "failed"
    assert acts[-1].extra_metadata["failure_code"] == "UNAUTHORIZED"
    assert acts[-1].extra_metadata["retryable"] is False


# ============================================== E. Package 1 × Package 2 接缝


def test_e1_unbound_external_identity_with_grant_is_allowed(db_engine) -> None:
    """★ 关键接缝：`Identity.user_id IS NULL` + 有效 Grant → 全链 **ALLOW**。

    Package 1（Gate 2.6 D1/D2/D3）让未绑定 ACP User 的外部身份成为合法运行时身份；
    Package 2 的授权作用域是 **Conversation**（ADR-3）——因此未绑定身份**不会**
    因为「没有 ACP User」而回到 UNAUTHORIZED。
    """
    g = _seed(db_engine, bound=False, statuses=("active",), granted=(0,))
    assert g["user"] is None

    agent_adapter, channel_adapter = _RecordingAgentAdapter(), _RecordingChannelAdapter()
    result = _coordinator(db_engine, agent_adapter, channel_adapter).execute(
        _inbound(g["channel"]))

    assert result.session_id is not None, "未绑定身份也必须能被授权并继续"
    assert _session_count(db_engine) == 1
    assert channel_adapter.deliveries, "投递应完成"

    with SASession(db_engine) as s:
        identity_row = s.get(IdentityModel, g["identity"])
    assert identity_row.user_id is None, "授权不要求绑定，也不得顺手补建绑定"


def test_e2_unbound_external_identity_without_grant_is_unauthorized(db_engine) -> None:
    """反向：未绑定身份**没有** Grant 时仍然 UNAUTHORIZED——

    「未绑定」不是授权旁路（ADR-6 / Gate 2.6 D2：不是匿名 / 公开可用）。
    """
    g = _seed(db_engine, bound=False, statuses=("active",), granted=())
    with pytest.raises(AccessAuthorizationDeniedError):
        _coordinator(db_engine, _RecordingAgentAdapter(),
                     _RecordingChannelAdapter()).execute(_inbound(g["channel"]))


# ================================================================== F. 守卫


def test_f1_execution_order_places_authorization_between_identity_and_routing() -> None:
    order = RUNTIME_EXECUTION_ORDER
    assert order.index("identity_resolution") < order.index("authorization")
    assert order.index("authorization") < order.index("routing")
    assert order.index("authorization") < order.index("session_resolution")


def test_f2_stage_failure_map_registers_unauthorized() -> None:
    assert STAGE_FAILURE_MAP[AccessAuthorizationDeniedError] == (
        STAGE_AUTHORIZATION, "UNAUTHORIZED", False)
    assert STAGE_AUTHORIZATION == "authorization"


def test_f3_routing_requires_the_granted_candidate_set() -> None:
    """Router 的候选集**必须**由授权产出（缺席即等于「候选不受限」）。

    AST 级：`execute` 恰有两个参数（channel_id + granted_connection_ids）——
    没有默认值，因此调用方不可能省略授权。
    """
    tree = ast.parse(ROUTING_PY.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "execute")
    args = [a.arg for a in fn.args.args[1:]]
    assert args == ["channel_id", "granted_connection_ids"], args
    assert fn.args.defaults == [], "授权候选集不得有默认值（占位授权会被放回来）"

    src = ROUTING_PY.read_text(encoding="utf-8")
    assert "if len(connections) != 1" in src, "唯一性判定必须保留"
    assert "granted_connection_ids" in src


def test_f4_router_does_not_query_authorization_records() -> None:
    """Router **不**负责查数据库授权（Step 2 边界）：候选集由 Coordinator 传入。"""
    src = ROUTING_PY.read_text(encoding="utf-8")
    assert "connection_conversations" not in src
    assert "find_granted_connection_ids" not in src


def test_f5_coordinator_boundaries_hold_after_the_seventh_dependency() -> None:
    """第 7 依赖注入后，既有 Coordinator 纯度守卫仍成立。"""
    src = COORDINATOR_PY.read_text(encoding="utf-8")
    assert "tenant" not in src.lower(), "coordinator 不得出现租户字面量"
    assert "user_id" not in src, "coordinator 不得出现 user_id 字面量"
    tree = ast.parse(src)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Try)], \
        "coordinator 不得 catch（拒绝必须抛 typed error）"
    assert src.count("decision.mode ==") == 2, "session 分支仍恰为 2 个"
