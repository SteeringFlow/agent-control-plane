"""Step 10.8.4 —— Persistence Result Wiring（接线收口）。

解决 10.4.1 遗留至今的两个实际缺口：

    RuntimeExecutionResult
      ├── agent_response → interactions.response_payload（原样快照，10.7.2 B）
      │                  → sessions.external_session_id（会话映射绑定）
      ├── session_id     → interactions.session_id（直接取 Result，不再推导）
      └── session_mode   → session_created Evidence 判定（直接观察）

用户指定的四件事（全部落地）：
1. PersistingCoordinator 接住 RuntimeExecutionResult（同签名返回）；
2. agent_response 原样映射到 interaction.response_payload；
3. 使用 Result 的 session_id（成功路径删除 wrapper 的弱键推导）；
4. agent_response.external_session_id 更新对应 Session 的会话映射。

重新验证（用户两组路径）：
- CREATE_NEW  → session_id 直接来自 Result / external_session_id 落 Session /
                response_payload 落 Interaction / session_created Evidence；
- REUSE_ACTIVE → session_id 直接来自 Result / 不产生新 Session /
                response_payload 落 Interaction / 零虚假 Evidence。

边界（用户明确守住，本步不做）：
- 有承载数据 ≠ 实现 Retry：不引入 RetryService / Replay API / Processing
  lease / In-flight lock / Response dedup state / Delivery queue / Outbox；
- 失败路径无 Result（异常传播）→ response_payload 保持 NULL、session 归属
  仍走弱键（开放点）——因此 **Case B（succeeded×failed）的 response_payload
  仍为 NULL**（10.7.2 B2 矩阵的诚实修订）。
"""

import ast
from dataclasses import fields
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SASession, sessionmaker

from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.errors import (
    AgentDispatchFailedError,
    ChannelDeliveryFailedError,
)
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import PersistingCoordinator
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.activity_record import (
    ACTIVITY_COMPLETED,
    ACTIVITY_RECEIVED,
    ACTIVITY_SESSION_CREATED,
    ACTIVITY_STAGE_FAILURE,
)
from app.domain.agent import Agent
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session_resolution import CREATE_NEW, REUSE_ACTIVE
from app.domain.user import User
from app.persistence.models.activity import ActivityModel
from app.persistence.models.interaction import InteractionModel
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
APPLICATION = APP / "application"
REPO_ROOT = BACKEND.parent
DOC = REPO_ROOT / "docs" / "step-10.8.4-persistence-result-wiring.md"


# ================================================================== 测试底座


class _CountingAgentAdapter:
    """固定响应；可选「不再返回 external_session_id」以验证映射不被清空。"""

    def __init__(self, carry_external_session_id: bool = True) -> None:
        self.calls = 0
        self._carry = carry_external_session_id

    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        self.calls += 1
        return AgentResponse(
            content=f"echo:{request.content}",
            external_session_id="agent-session-1" if self._carry else None)


class _RecordingChannelStub:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine) -> dict[str, int]:
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        ident = uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status="active",
            config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
            credential_ref="cred-a"))
        # Implementation Package 2 / Step 2：显式授权本文件用到的每个会话键
        for conversation in ("conv-1", "conv-dp", "conv-df"):
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=channel.id, external_conversation_id=conversation,
                connection_id=path.id))
        uow.commit()
        return {"channel": channel.id, "agent": agent.id,
                "identity": ident.id}


def _wrapped_runtime(db_engine, agent_adapter=None, channel_adapter=None):
    uow = _uow_factory(db_engine)
    inner = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=agent_adapter or _CountingAgentAdapter(),
            uow_factory=uow),
        delivery=DeliveryExecutor(
            channel_adapter=channel_adapter or _RecordingChannelStub()),
    )
    return PersistingCoordinator(inner=inner, uow_factory=_uow_factory(db_engine))


def _inbound(channel_id: int, interaction: str = "msg-1",
             conversation: str = "conv-1") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id=interaction,
        external_conversation_id=conversation,
        external_identity_id="wx_a", content="hello")


def _interaction_rows(db_engine) -> list[InteractionModel]:
    with SASession(db_engine) as s:
        return list(s.execute(select(InteractionModel)
                              .order_by(InteractionModel.id)).scalars().all())


def _session_rows(db_engine) -> list[SessionModel]:
    with SASession(db_engine) as s:
        return list(s.execute(select(SessionModel)
                              .order_by(SessionModel.id)).scalars().all())


def _activity_rows(db_engine, interaction_id: int) -> list[ActivityModel]:
    with SASession(db_engine) as s:
        return list(s.execute(
            select(ActivityModel)
            .where(ActivityModel.interaction_id == interaction_id)
            .order_by(ActivityModel.id)).scalars().all())


def _py_files(root: Path):
    return sorted(p for p in root.rglob("*.py"))


# ================================================================== A. 四件事落地


def test_a1_wrapper_catches_and_returns_the_result(db_engine) -> None:
    """四件事 ①：wrapper 接住 RuntimeExecutionResult 并**原样返回**——
    观察层不改执行，结果就是 Runtime 的结果。"""
    g = _seed(db_engine)
    runtime = _wrapped_runtime(db_engine)
    result = runtime.execute(_inbound(g["channel"], "msg-1"))

    assert isinstance(result, RuntimeExecutionResult)
    assert result.session_mode == CREATE_NEW
    assert result.agent_response.content == "echo:hello"


def test_a2_agent_response_maps_to_response_payload(db_engine) -> None:
    """四件事 ②：agent_response **原样**映射到 interaction.response_payload
    （四字段快照：content / external_session_id / metadata / artifacts，
    不加工不截断；V0.2 加性 artifacts）。"""
    g = _seed(db_engine)
    result = _wrapped_runtime(db_engine).execute(_inbound(g["channel"]))

    row = _interaction_rows(db_engine)[0]
    assert row.response_payload == {
        "content": "echo:hello",
        "external_session_id": "agent-session-1",
        "metadata": None,
        "artifacts": None,
    }
    assert row.processing_status == "succeeded"       # 快照只属成功终态
    assert row.delivery_status == "delivered"


def test_a3_session_id_comes_from_result_not_rederivation(db_engine) -> None:
    """四件事 ③：成功路径的 session 归属直接取 Result——
    interaction.session_id == result.session_id == DB 中的真实 Session。"""
    g = _seed(db_engine)
    result = _wrapped_runtime(db_engine).execute(_inbound(g["channel"]))

    rows = _interaction_rows(db_engine)
    assert rows[0].session_id == result.session_id
    assert rows[0].session_id == _session_rows(db_engine)[0].id
    # 成功路径不再有「执行前后快照推导」（源码级锁定，见 C3）


def test_a4_external_session_id_binds_the_session(db_engine) -> None:
    """四件事 ④：agent_response.external_session_id 更新对应 Session 的
    会话映射（10.3.1 §4 / 10.7.2 B4 遗留缺口的关闭）。"""
    g = _seed(db_engine)
    result = _wrapped_runtime(db_engine).execute(_inbound(g["channel"]))

    session = _session_rows(db_engine)[0]
    assert session.id == result.session_id
    assert session.external_session_id == "agent-session-1"


# ================================================================== B. 两组路径重新验证


def test_b1_create_new_path_end_to_end(db_engine) -> None:
    """CREATE_NEW：session_id 直接来自 Result → external_session_id 落 Session
    → response_payload 落 Interaction → session_created Evidence 直证。"""
    g = _seed(db_engine)
    result = _wrapped_runtime(db_engine).execute(_inbound(g["channel"]))

    assert result.session_mode == CREATE_NEW
    row = _interaction_rows(db_engine)[0]
    session = _session_rows(db_engine)[0]
    assert row.session_id == session.id == result.session_id
    assert session.external_session_id == "agent-session-1"
    assert row.response_payload["content"] == "echo:hello"

    acts = _activity_rows(db_engine, row.id)
    created = [a for a in acts if a.type == ACTIVITY_SESSION_CREATED]
    assert len(created) == 1                          # Evidence 直证（非推导）
    assert created[0].session_id == session.id


def test_b2_reuse_active_path_end_to_end(db_engine) -> None:
    """REUSE_ACTIVE：session_id 直接来自 Result → 不产生新 Session →
    response_payload 落 Interaction → 零虚假 session_created Evidence。"""
    g = _seed(db_engine)
    runtime = _wrapped_runtime(db_engine)
    first = runtime.execute(_inbound(g["channel"], "msg-1"))
    second = runtime.execute(_inbound(g["channel"], "msg-2"))

    assert first.session_mode == CREATE_NEW
    assert second.session_mode == REUSE_ACTIVE
    assert second.session_id == first.session_id      # 不产生新 Session

    rows = _interaction_rows(db_engine)
    assert len(rows) == 2
    assert len(_session_rows(db_engine)) == 1         # Session 仍只有一条
    for row in rows:
        assert row.session_id == first.session_id     # 都来自 Result
        assert row.response_payload is not None       # 两条都落快照

    reuse_acts = _activity_rows(db_engine, rows[1].id)
    assert [a.type for a in reuse_acts] == [
        ACTIVITY_RECEIVED, ACTIVITY_COMPLETED]        # REUSE 零虚假 Evidence


def test_b3_reuse_without_external_session_id_keeps_existing_binding(db_engine) -> None:
    """映射一旦建立就是事实：后续请求 Agent 不再返回 external_session_id
    （None）→ 不写入、不清空既有映射。"""
    g = _seed(db_engine)
    runtime = _wrapped_runtime(
        db_engine, agent_adapter=_CountingAgentAdapter(carry_external_session_id=False))
    runtime.execute(_inbound(g["channel"], "msg-0"))   # 先建立映射

    runtime_no_carry = _wrapped_runtime(db_engine)
    runtime_no_carry.execute(_inbound(g["channel"], "msg-1"))
    runtime_no_carry.execute(_inbound(g["channel"], "msg-2"))

    session = _session_rows(db_engine)[0]
    assert session.external_session_id == "agent-session-1"  # 未被清空
    rows = _interaction_rows(db_engine)
    assert len(rows) == 3
    for row in rows:                                   # response_payload 照常落地
        assert row.response_payload is not None


# ================================================================== C. 边界锁死


def test_c1_no_retry_or_idempotency_infrastructure() -> None:
    """有承载数据 ≠ 实现 Retry：全 app 树无 RetryService / Replay / Outbox /
    Delivery queue / lease / lock / dedup / idempotency 符号与执行入口。"""
    forbidden_tokens = ("retryservice", "replayservice", "outbox",
                        "deliveryqueue", "processinglease", "inflightlock",
                        "responsededup", "idempot", "dedup")
    for path in _py_files(APP):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                lower = node.name.lower()
                for token in forbidden_tokens:
                    assert token not in lower, \
                        f"{path.name} 出现 Retry/幂等符号：{node.name}"
        src = path.read_text(encoding="utf-8")
        for forbidden in ("def retry(", "def replay(", "def redeliver("):
            assert forbidden not in src, f"{path.name} 出现执行入口：{forbidden}"


def test_c2_failure_model_and_delivery_semantics_untouched(db_engine) -> None:
    """失败模型不变：dispatch 前失败 → response_payload 保持 NULL（响应从未
    诞生，不可伪造）；Delivery 失败仍 succeeded × failed（Delivery 语义零改动）。"""
    g = _seed(db_engine)

    class _FailingAgent:
        def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
            raise AgentDispatchFailedError()

    with pytest.raises(AgentDispatchFailedError):
        _wrapped_runtime(db_engine, agent_adapter=_FailingAgent()).execute(
            _inbound(g["channel"], "msg-dp", conversation="conv-dp"))

    rows = _interaction_rows(db_engine)
    dp = rows[0]
    assert (dp.processing_status, dp.delivery_status) == ("failed", "pending")
    assert dp.response_payload is None                # 响应从未诞生（无 Result）
    assert dp.session_id is not None                  # dispatch 失败在 Session
    #   建立之后 → 失败路径弱键回填（10.4.2 既有行为，开放点保持）


def test_c3_success_path_no_longer_rederives_session() -> None:
    """源码级锁定：wrapper 成功路径不再有「执行前后快照推导」——
    session 事实全部来自 Result；弱键只剩失败路径（无 Result，开放点）。"""
    src = (APPLICATION / "interaction_recording.py").read_text(encoding="utf-8")
    assert "session_existed_before" not in src
    assert "result.session_id" in src
    assert "result.session_mode" in src
    assert "find_active_by_identity_and_conversation" in src  # 仅失败路径


def test_c4_case_b_response_payload_stays_null(db_engine) -> None:
    """诚实修订：Case B（Agent 成功 + Delivery 失败）→ Result 不存在
    （异常传播）→ response_payload 仍 NULL；session 归属走弱键（开放点）。
    （10.7.2 B2 矩阵的原「Case B 有值」一行据此修订。）"""
    from app.application.errors import ChannelDeliveryFailedError

    class _FailingChannel:
        def deliver(self, outbound: OutboundResponse) -> None:
            raise ChannelDeliveryFailedError(
                channel_id=outbound.channel_id,
                external_conversation_id=outbound.external_conversation_id)

    g = _seed(db_engine)
    with pytest.raises(ChannelDeliveryFailedError):
        _wrapped_runtime(db_engine, channel_adapter=_FailingChannel()).execute(
            _inbound(g["channel"], "msg-df", conversation="conv-df"))

    row = _interaction_rows(db_engine)[0]
    assert (row.processing_status, row.delivery_status) == ("succeeded", "failed")
    assert row.response_payload is None               # 失败路径无 Result
    assert row.retryable is True


def test_c5_runtime_and_boundaries_untouched() -> None:
    """Runtime Core 本次零改动：RUNTIME_EXECUTION_ORDER / Protocol 签名 /
    coordinator 不写库 / STAGE_FAILURE_MAP 均保持 10.8.3 状态。"""
    from app.application.runtime import RUNTIME_EXECUTION_ORDER
    assert RUNTIME_EXECUTION_ORDER == (
        "identity_resolution", "authorization", "routing", "session_resolution",
        "session_creation", "agent_dispatch", "delivery",
    )
    coordinator_src = (APPLICATION / "coordinator.py").read_text(encoding="utf-8")
    # 15.5.2 翻转：Coordinator 透传 external_session_id（只读实参）；
    # Runtime 仍不写库——写入点唯一在 wrapper（bind_external_session）。
    assert "bind_external_session" not in coordinator_src
    assert "response_payload" not in coordinator_src
    runtime_src = (APPLICATION / "runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(runtime_src, filename="runtime.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "execute":
            assert ast.unparse(node.returns) == "RuntimeExecutionResult"
            break


# ================================================================== D. 契约成文


def test_d1_contract_is_written_down() -> None:
    """接线契约成文（防漂移）。"""
    assert DOC.exists(), f"缺少接线文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in ("四件事",
                  "response_payload",
                  "sessions.external_session_id",
                  "不再由 wrapper",
                  "CREATE_NEW",
                  "REUSE_ACTIVE",
                  "原样快照",
                  "有承载数据 ≠ 实现 Retry",
                  "Case B",
                  "10.5.3",
                  "10.7.2"):
        assert token in text, f"接线文档缺少 token：{token}"
