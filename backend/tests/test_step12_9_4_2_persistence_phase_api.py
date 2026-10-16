"""Step 12.9.4.2 —— Persistence Phase API。

PersistingCoordinator 内部混合的 create / mark_processing / record_* 被拆成显式
phase operations 之后，本文件锁定这些 primitive 的真实语义：

A  phase API 面：create_pending / claim_processing / record_success / record_failure
B  create_pending    → PENDING 行 + received Evidence（独立短事务）
C  claim_processing  → CAS 原子 claim：先到者 True、后到者 False（防双执行核心）
D  record_success / record_failure → phase 3 公开可用
E  execute()         → 退化为 phase 组合，同步路径终态不变（回归）

刻意不测（12.9.4.2 范围外，遵守本步边界）：
❌ Worker  ❌ intake 路径改造  ❌ 移除 Runtime.execute  ❌ RuntimeCoordinator 改动
❌ 部署切换
"""

import inspect

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.application.errors import IdentityResolutionFailedError
from app.application.interaction_recording import PersistingCoordinator
from app.domain.activity_record import (
    ACTIVITY_COMPLETED,
    ACTIVITY_RECEIVED,
    STAGE_DELIVERY,
)
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.interaction_record import DELIVERY_DELIVERED, PROCESSING_SUCCEEDED
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session import Session as SessionEntity
from app.domain.session_resolution import REUSE_ACTIVE
from app.domain.user import User
from app.persistence.models.activity import ActivityModel
from app.persistence.repositories.interaction_record import (
    SqlAlchemyInteractionRecordRepository,
)
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork


# ================================================================== 测试底座


class _StubRuntime:
    """极简 inner：只记录被调用次数并回显给定 Result（不触碰真实 Runtime 六段）。"""

    def __init__(self, result: RuntimeExecutionResult | None = None) -> None:
        self.result = result
        self.calls = 0

    def execute(self, inbound: InboundInteraction) -> RuntimeExecutionResult:
        self.calls += 1
        return self.result


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine, with_session: bool = False) -> dict:
    """最小种子：user / channel / identity（+ 可选 session，满足 interactions FK）。"""
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        identity = uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        session_id = None
        if with_session:
            session_id = uow.sessions.create(SessionEntity(
                identity_id=identity.id, channel_id=channel.id,
                external_conversation_id="conv-1", status="active")).id
        uow.commit()
        return {"channel": channel.id, "session": session_id}


def _inbound(channel_id: int) -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id="msg-1",
        external_conversation_id="conv-1", external_identity_id="wx_a",
        content="你好")


def _coordinator(db_engine, inner=None) -> PersistingCoordinator:
    return PersistingCoordinator(inner or _StubRuntime(), _uow_factory(db_engine))


def _row(db_engine, interaction_id: int) -> dict:
    """经 Port 读回一行（不绕过仓储）。"""
    with _uow_factory(db_engine)() as uow:
        rec = uow.interactions.get_by_id(interaction_id)
        assert rec is not None
        return {
            "processing_status": rec.processing_status,
            "delivery_status": rec.delivery_status,
            "failure_stage": rec.failure_stage,
            "failure_code": rec.failure_code,
            "retryable": rec.retryable,
            "request_payload": rec.request_payload,
            "response_payload": rec.response_payload,
        }


def _activities(db_engine, interaction_id: int):
    with Session(db_engine) as s:
        rows = s.execute(select(ActivityModel).where(
            ActivityModel.interaction_id == interaction_id)).scalars().all()
        return [(r.type, r.stage, r.status) for r in rows]


# ================================================================== A. phase API 面


def test_a1_phase_operations_are_public() -> None:
    """四个 phase operation 成为可独立调用的公开方法（未来 Worker 直接复用）。"""
    for name in ("create_pending", "claim_processing", "record_success",
                 "record_failure"):
        assert callable(getattr(PersistingCoordinator, name, None)), \
            f"PersistingCoordinator 缺少 phase operation：{name}"


# ================================================================== B. create_pending


def test_b1_create_pending_persists_a_pending_row(db_engine) -> None:
    """phase 1：PENDING 行 + received Evidence；提交后即为既成事实（可 ACK）。"""
    g = _seed(db_engine)
    coord = _coordinator(db_engine)
    interaction_id = coord.create_pending(_inbound(g["channel"]))

    row = _row(db_engine, interaction_id)
    assert row["processing_status"] == "pending"        # 尚未 claim
    assert row["delivery_status"] == "pending"
    assert row["request_payload"]["content"] == "你好"
    assert (ACTIVITY_RECEIVED, None, "recorded") in _activities(db_engine,
                                                                interaction_id)


# ================================================================== C. claim_processing（CAS）


def test_c1_first_claim_wins(db_engine) -> None:
    """首次 claim → True，行进入 PROCESSING。"""
    g = _seed(db_engine)
    coord = _coordinator(db_engine)
    interaction_id = coord.create_pending(_inbound(g["channel"]))

    assert coord.claim_processing(interaction_id) is True
    assert _row(db_engine, interaction_id)["processing_status"] == "processing"


def test_c2_second_claim_loses(db_engine) -> None:
    """核心：第二个调用方（未来第二个 Worker）claim 同一行 → False，不得执行。

    无条件赋值会让两个 Worker 都「成功」→ 双执行（12.9.3.2 §3）；CAS 让后到者
    拿到 False 从而停止，这是「一次且仅一次执行」的唯一依据。
    """
    g = _seed(db_engine)
    coord = _coordinator(db_engine)
    interaction_id = coord.create_pending(_inbound(g["channel"]))

    assert coord.claim_processing(interaction_id) is True
    assert coord.claim_processing(interaction_id) is False
    assert _row(db_engine, interaction_id)["processing_status"] == "processing"


def test_c3_claim_on_terminal_row_loses(db_engine) -> None:
    """已是终态的行不可被重新 claim（不复活已完成 / 已失败的交互）。"""
    g = _seed(db_engine)
    coord = _coordinator(db_engine)
    interaction_id = coord.create_pending(_inbound(g["channel"]))
    coord.claim_processing(interaction_id)
    with _uow_factory(db_engine)() as uow:
        uow.interactions.record_outcome(
            interaction_id, processing_status=PROCESSING_SUCCEEDED,
            delivery_status=DELIVERY_DELIVERED)
        uow.commit()

    assert coord.claim_processing(interaction_id) is False


def test_c4_claim_is_a_single_conditional_update() -> None:
    """实现形状守卫：claim 必须是条件化 UPDATE（CAS），不是「读回再赋值」。

    「先 `_require` 读回、再给字段赋值」是非原子的读-改-写——两个调用方都会成功，
    正是双执行的来源。这里锁定实现确实带 PENDING 条件且以 rowcount 判定。
    """
    src = inspect.getsource(
        SqlAlchemyInteractionRecordRepository.claim_processing)
    assert "where(" in src
    assert "PROCESSING_PENDING" in src          # 条件：仅当仍是 PENDING
    assert "rowcount" in src                    # 判定：以受影响行数为准


# ================================================================== D. phase 3 outcome


def test_d1_record_failure_is_a_public_phase(db_engine) -> None:
    """失败终态：Delivery 之前 STOP → processing=FAILED × delivery=PENDING。"""
    g = _seed(db_engine)
    coord = _coordinator(db_engine)
    interaction_id = coord.create_pending(_inbound(g["channel"]))
    coord.claim_processing(interaction_id)

    coord.record_failure(
        interaction_id, _inbound(g["channel"]),
        IdentityResolutionFailedError(channel_id=g["channel"],
                                      external_identity_id="wx_a"))

    row = _row(db_engine, interaction_id)
    assert row["processing_status"] == "failed"
    assert row["delivery_status"] == "pending"
    assert row["failure_stage"] == "identity_resolution"
    assert row["failure_code"] == "IDENTITY_RESOLUTION_FAILED"
    assert row["retryable"] is False


def test_d2_record_success_is_a_public_phase(db_engine) -> None:
    """成功终态：processing=SUCCEEDED × delivery=DELIVERED + 响应快照。"""
    g = _seed(db_engine, with_session=True)
    coord = _coordinator(db_engine)
    interaction_id = coord.create_pending(_inbound(g["channel"]))
    coord.claim_processing(interaction_id)

    coord.record_success(interaction_id, RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=g["session"],
        agent_response=AgentResponse(content="ok")))

    row = _row(db_engine, interaction_id)
    assert row["processing_status"] == "succeeded"
    assert row["delivery_status"] == "delivered"
    assert row["response_payload"]["content"] == "ok"
    assert (ACTIVITY_COMPLETED, STAGE_DELIVERY, "succeeded") in _activities(
        db_engine, interaction_id)


# ================================================================== E. execute 组合回归


def test_e1_execute_composes_create_claim_execute_outcome(db_engine) -> None:
    """同步路径回归：execute() = 四 phase 组合 → inner 恰好执行一次 + 终态落定。"""
    g = _seed(db_engine, with_session=True)
    inner = _StubRuntime(RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=g["session"],
        agent_response=AgentResponse(content="ok")))
    coord = PersistingCoordinator(inner, _uow_factory(db_engine))

    result = coord.execute(_inbound(g["channel"]))

    assert inner.calls == 1                        # 没有被双执行
    assert result.agent_response.content == "ok"   # Result 原样返回
    with _uow_factory(db_engine)() as uow:
        rec = uow.interactions.find_by_channel_and_external_id(
            g["channel"], "msg-1")
    assert rec is not None
    assert rec.processing_status == "succeeded"
    assert rec.delivery_status == "delivered"
    assert rec.session_id == g["session"]
