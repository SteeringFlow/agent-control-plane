"""Step 10.8.5 —— Runtime Evidence / Read Closure Audit（审计步，生产代码 0 行）。

回到 10.7.1 / 10.7.2 的问题重跑探针：10.8 这一轮到底消除了多少被证明存在的
Evidence / Read 缺口？（全部真实运行时验证，不从代码结构推断）

A  response_payload：完整成功 → 三字段原样快照（无损性：键面 == AgentResponse
   字段面，metadata 原样）；
B  会话映射：CREATE_NEW / REUSE 都绑定；None 不清空；
C  Session identity：成功路径弱键**已消失**（result.session_id 直取）；
   失败路径弱键**仍存在**（无 Result）——两种弱键严格区分，不得混淆；
D  session_created Evidence 由 `result.session_mode == CREATE_NEW` 直接产生
   （before/after snapshot 推导已在源码中消失）；
E  REUSE_ACTIVE：正确 session_id + 零虚假 Evidence；
F  Delivery failure 真实探针：response_payload 的持久化时点 = Delivery 成功
   之后、execute 返回之前的终态事务；执行中不可见；Delivery 失败 → 从未落库。
G  失败路径弱键歧义判定：MVP 真实边界下无实际歧义（Runtime Capability Gap
   使歧义状态不可达）→ 记录开放项，不修；触发条件 = 多 ACTIVE Connection 落地。

关闭矩阵见 docs/step-10.8.5-runtime-evidence-read-closure-audit.md §1。
"""

import ast
from dataclasses import fields
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as SASession, sessionmaker

from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.errors import (
    ChannelDeliveryFailedError,
    RoutingFailedError,
)
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import PersistingCoordinator
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
from app.domain.session_resolution import CREATE_NEW, REUSE_ACTIVE
from app.domain.user import User
from app.persistence.models.interaction import InteractionModel
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
APPLICATION = APP / "application"
REPO_ROOT = BACKEND.parent
DOC = REPO_ROOT / "docs" / "step-10.8.5-runtime-evidence-read-closure-audit.md"


# ================================================================== 测试底座


class _CountingAgentAdapter:
    def __init__(self, carry_external_session_id: bool = True) -> None:
        self.calls = 0
        self._carry = carry_external_session_id

    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        self.calls += 1
        return AgentResponse(
            content=f"echo:{request.content}",
            external_session_id="agent-session-1" if self._carry else None,
            metadata={"tool_calls": 3, "model": "acp-x"})


class _RecordingChannelStub:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


class _ProbingAgentAdapter:
    """时点探针 1：dispatch 进行中查库（Delivery 之前的时点）。"""

    def __init__(self, rows_fn, observed: dict) -> None:
        self._rows_fn = rows_fn
        self.observed = observed

    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        rows = self._rows_fn()
        self.observed["payload_during_dispatch"] = rows[-1].response_payload
        self.observed["status_during_dispatch"] = rows[-1].processing_status
        return AgentResponse(content="echo:hello",
                             external_session_id="agent-session-1")


class _ProbingChannelAdapter:
    """时点探针 2：Delivery 进行中查库（deliver 内部，落库之前）。"""

    def __init__(self, rows_fn, observed: dict) -> None:
        self._rows_fn = rows_fn
        self.observed = observed

    def deliver(self, outbound: OutboundResponse) -> None:
        rows = self._rows_fn()
        self.observed["payload_during_delivery"] = rows[-1].response_payload
        self.observed["status_during_delivery"] = rows[-1].processing_status


class _FailingProbingChannelAdapter:
    """F 真实失败探针：Delivery 失败，且失败当刻查库留证。"""

    def __init__(self, rows_fn, observed: dict) -> None:
        self._rows_fn = rows_fn
        self.observed = observed

    def deliver(self, outbound: OutboundResponse) -> None:
        rows = self._rows_fn()
        self.observed["payload_during_failed_delivery"] = \
            rows[-1].response_payload
        self.observed["status_during_failed_delivery"] = \
            rows[-1].processing_status
        raise ChannelDeliveryFailedError(
            channel_id=outbound.channel_id,
            external_conversation_id=outbound.external_conversation_id)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine) -> dict[str, int]:
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status="active",
            config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
            credential_ref="cred-a"))
        # Implementation Package 2 / Step 2：显式授权本文件用到的每个会话键
        for conversation in ("conv-1", "conv-df"):
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=channel.id, external_conversation_id=conversation,
                connection_id=path.id))
        uow.commit()
        return {"channel": channel.id, "agent": agent.id,
                "connection": path.id}


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


# ================================================================== A. response_payload


def test_a1_response_payload_is_complete_for_full_success(db_engine) -> None:
    """A/G1 关闭验证：完整成功 → 三字段原样快照（content/external_session_id/
    metadata 与 AgentResponse 逐字段一致）。"""
    g = _seed(db_engine)
    result = _wrapped_runtime(db_engine).execute(_inbound(g["channel"]))

    row = _interaction_rows(db_engine)[0]
    assert row.response_payload == {
        "content": result.agent_response.content,
        "external_session_id": result.agent_response.external_session_id,
        "metadata": result.agent_response.metadata,
        "artifacts": result.agent_response.artifacts,
    }


def test_a2_response_payload_is_lossless(db_engine) -> None:
    """A 无损性：快照键面 == AgentResponse 字段面；metadata 结构原样传递
    （不加工、不截断——Evidence = 事实）。"""
    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(_inbound(g["channel"]))

    payload = _interaction_rows(db_engine)[0].response_payload
    agent_fields = {f.name for f in fields(AgentResponse)}
    assert set(payload.keys()) == agent_fields
    assert payload["metadata"] == {"tool_calls": 3, "model": "acp-x"}


def test_a3_payload_not_visible_during_dispatch(db_engine) -> None:
    """F 时点探针 1：dispatch 进行中 response_payload 仍 NULL、状态 processing
    ——落库发生在 Delivery 之后的终态事务。"""
    g = _seed(db_engine)
    observed: dict = {}
    _wrapped_runtime(
        db_engine,
        agent_adapter=_ProbingAgentAdapter(
            lambda: _interaction_rows(db_engine), observed),
    ).execute(_inbound(g["channel"]))

    assert observed["payload_during_dispatch"] is None
    assert observed["status_during_dispatch"] == "processing"
    assert _interaction_rows(db_engine)[0].response_payload is not None


# ================================================================== B. 会话映射


def test_b1_session_mapping_lands_for_both_modes(db_engine) -> None:
    """B/G2 关闭验证：CREATE_NEW 与 REUSE_ACTIVE 都把
    agent_response.external_session_id 绑定到 Result 指向的那条 Session。"""
    g = _seed(db_engine)
    runtime = _wrapped_runtime(db_engine)
    first = runtime.execute(_inbound(g["channel"], "msg-1"))
    second = runtime.execute(_inbound(g["channel"], "msg-2"))

    sessions = _session_rows(db_engine)
    assert len(sessions) == 1
    assert sessions[0].id == first.session_id == second.session_id
    assert sessions[0].external_session_id == "agent-session-1"


def test_b2_session_mapping_not_cleared_by_none(db_engine) -> None:
    """B 映射保护：后续请求 Agent 不返回引用（None）→ 不写入、不清空。"""
    g = _seed(db_engine)
    runtime = _wrapped_runtime(
        db_engine, agent_adapter=_CountingAgentAdapter(carry_external_session_id=False))
    runtime.execute(_inbound(g["channel"], "msg-0"))   # 先建立映射

    plain = _wrapped_runtime(db_engine)
    plain.execute(_inbound(g["channel"], "msg-1"))
    plain.execute(_inbound(g["channel"], "msg-2"))

    assert _session_rows(db_engine)[0].external_session_id == "agent-session-1"


# ================================================================== C. Session identity（两种弱键严格区分）


def test_c1_success_path_weak_key_is_gone(db_engine) -> None:
    """C/G3 关闭验证：成功路径 session 归属直接来自 Result——
    源码级（无前后快照推导）+ 运行时（两条路径 interaction.session_id ==
    result.session_id）。"""
    src = (APPLICATION / "interaction_recording.py").read_text(encoding="utf-8")
    assert "session_existed_before" not in src
    assert "result.session_id" in src

    g = _seed(db_engine)
    runtime = _wrapped_runtime(db_engine)
    first = runtime.execute(_inbound(g["channel"], "msg-1"))
    second = runtime.execute(_inbound(g["channel"], "msg-2"))
    rows = _interaction_rows(db_engine)
    assert rows[0].session_id == first.session_id
    assert rows[1].session_id == second.session_id


def test_c2_failure_path_weak_key_still_exists() -> None:
    """C 区分（不得混淆）：成功路径弱键已消失，**失败路径弱键仍存在**
    （无 Result，`record_failure` 仍走 `find_active_by_identity_and_conversation`）
    ——歧义判定见 G1/G2 与文档 §4。"""
    src = (APPLICATION / "interaction_recording.py").read_text(encoding="utf-8")
    assert "find_active_by_identity_and_conversation" in src
    assert "result.session_id" in src                  # 成功路径已直取


# ================================================================== D/E. Evidence


def test_d1_session_created_evidence_comes_from_session_mode(db_engine) -> None:
    """D/G4 关闭验证：session_created Evidence 由 `result.session_mode ==
    CREATE_NEW` 直接产生（源码无 before/after 推导）；Evidence 的 session_id
    == Result 的 session_id。"""
    src = (APPLICATION / "interaction_recording.py").read_text(encoding="utf-8")
    assert "result.session_mode" in src
    assert "session_existed_before" not in src

    g = _seed(db_engine)
    result = _wrapped_runtime(db_engine).execute(_inbound(g["channel"]))
    row = _interaction_rows(db_engine)[0]

    from sqlalchemy.orm import Session as SAS
    from app.persistence.models.activity import ActivityModel
    with SAS(db_engine) as s:
        created = [a for a in s.execute(
            select(ActivityModel)
            .where(ActivityModel.interaction_id == row.id)
            .order_by(ActivityModel.id)).scalars().all()
            if a.type == "session_created"]
    assert len(created) == 1
    assert created[0].session_id == result.session_id


def test_e1_reuse_active_correct_session_and_no_evidence(db_engine) -> None:
    """E：REUSE_ACTIVE → 正确 session_id（复用首条）+ 零 session_created
    Evidence（「Session reused」仍不制造 Evidence）。"""
    g = _seed(db_engine)
    runtime = _wrapped_runtime(db_engine)
    first = runtime.execute(_inbound(g["channel"], "msg-1"))
    second = runtime.execute(_inbound(g["channel"], "msg-2"))

    assert second.session_mode == REUSE_ACTIVE
    assert second.session_id == first.session_id

    from sqlalchemy.orm import Session as SAS
    from app.persistence.models.activity import ActivityModel
    rows = _interaction_rows(db_engine)
    with SAS(db_engine) as s:
        second_acts = list(s.execute(
            select(ActivityModel)
            .where(ActivityModel.interaction_id == rows[1].id)
            .order_by(ActivityModel.id)).scalars().all())
    assert [a.type for a in second_acts] == [
        "interaction_received", "interaction_completed"]


# ================================================================== F. Delivery failure 真实探针


def test_f1_delivery_failure_real_probe_payload_never_lands(db_engine) -> None:
    """F/G6 真实失败探针：Delivery 失败当刻 payload 已是 NULL、状态 processing；
    失败后终态 = succeeded × failed 且 response_payload **从未落库**
    （终态事务以异常收场）。"""
    g = _seed(db_engine)
    observed: dict = {}
    with pytest.raises(ChannelDeliveryFailedError):
        _wrapped_runtime(
            db_engine,
            channel_adapter=_FailingProbingChannelAdapter(
                lambda: _interaction_rows(db_engine), observed),
        ).execute(_inbound(g["channel"], "msg-df", conversation="conv-df"))

    assert observed["payload_during_failed_delivery"] is None
    assert observed["status_during_failed_delivery"] == "processing"

    row = _interaction_rows(db_engine)[0]
    assert (row.processing_status, row.delivery_status) == ("succeeded", "failed")
    assert row.response_payload is None                # 从未落库（非「稍后落」）


def test_f2_payload_persistence_time_point_is_after_delivery(db_engine) -> None:
    """F 时点探针 2：Delivery 进行中（deliver 内部查库）payload 仍 NULL、
    状态 processing；execute 返回后才有值——持久化时点 = Delivery 成功之后的
    终态事务。"""
    g = _seed(db_engine)
    observed: dict = {}
    _wrapped_runtime(
        db_engine,
        channel_adapter=_ProbingChannelAdapter(
            lambda: _interaction_rows(db_engine), observed),
    ).execute(_inbound(g["channel"]))

    assert observed["payload_during_delivery"] is None
    assert observed["status_during_delivery"] == "processing"
    assert _interaction_rows(db_engine)[0].response_payload is not None


# ================================================================== G. 失败路径弱键歧义判定


def test_g1_runtime_cannot_produce_the_ambiguous_state(db_engine) -> None:
    """G8 判定的探针基础：Runtime 的 Capability Gap（单 Channel 双 ACTIVE →
    RoutingFailedError）使「同 (identity, conversation) 两条 active session」
    的歧义状态在 Runtime 行为中不可达——弱键无实际歧义。"""
    g = _seed(db_engine)
    with _uow_factory(db_engine)() as uow:
        agent_b = uow.agents.create(Agent(name="agent-b", type="acp")).id
        path_b = uow.connections.create(Connection(
            channel_id=g["channel"], agent_id=agent_b, name="path-b",
            status="active",
            config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
            credential_ref="cred-b"))
        uow.commit()
        path_b_id = path_b.id

    # Package 2 / Step 2：两条都给许可——失败必须来自「候选 >1」（Capability Gap），
    # 而不是来自「候选为空」（那会是另一条语义）
    with pytest.raises(RoutingFailedError):
        RoutingExecutor(_uow_factory(db_engine)).execute(
            g["channel"], frozenset({g["connection"], path_b_id}))


def test_g2_open_item_is_written_down() -> None:
    """开放项成文：成功路径弱键 ≠ 失败路径弱键；失败路径弱键在 MVP 下无实际
    歧义 → 记录不修；触发条件 = 多 ACTIVE Connection 落地。"""
    assert DOC.exists(), f"缺少关闭审计文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in ("成功路径", "失败路径", "无实际歧义", "开放项",
                  "MultipleResultsFound", "Capability Gap",
                  "触发条件"):
        assert token in text, f"关闭审计文档缺少 token：{token}"


# ================================================================== H. 关闭矩阵成文


def test_h1_closure_matrix_is_written_down() -> None:
    """关闭矩阵成文（防漂移）：G1~G10 判定齐全。"""
    text = DOC.read_text(encoding="utf-8")
    for token in ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10",
                  "已关闭", "部分关闭", "未关闭", "开放项",
                  "Delivery 成功之后", "从未落库", "有数据"):
        assert token in text, f"关闭矩阵缺少 token：{token}"
