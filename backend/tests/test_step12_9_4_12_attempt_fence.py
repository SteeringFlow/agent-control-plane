"""Step 12.9.4.12 —— Minimal Attempt Fence（最小围栏实现）。

锁定四个 primitive 的 fence 语义（12.9.4.11 §1–§4 的实现）：

A  Schema / Claim：`PENDING → PROCESSING` 与 `processing_started_at = DB now`
   在**同一条**条件化 UPDATE 内；app 绝不生成 claim 时间戳（Worker 读回持有）。
B  Outcome fence：`record_outcome(expected_started_at=...)` →
   COMMITTED（等式命中）/ REJECTED（ownership lost，**不抛异常**）；
   无 fence 的既有调用形状（同步/种子路径）不受影响。
C  reclaim_stale_processing：单条条件化 UPDATE / 有界 / 只回收
   （→ PENDING + started_at=NULL），不 claim、不执行。
D  **核心回归（orphan recovery 最关键测试）**：
       Worker A claim T1 → reclaim → Worker B claim T2
       → A outcome(T1) → REJECTED（B ownership untouched）
       → B outcome(T2) → COMMITTED
   旧 attempt 可以执行完，但**绝不能写穿**新 attempt。
E  Worker 端到端：正常路径 fence 恒过；被回收的孤儿可被重新 pickup 执行。
"""

import inspect
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.application.interaction_recording import PersistingCoordinator
from app.application.worker import Worker
from app.domain.activity_record import ACTIVITY_STAGE_FAILURE
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.interaction_record import (
    DELIVERY_DELIVERED,
    OutcomeCommit,
    PROCESSING_SUCCEEDED,
)
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session import Session as SessionEntity
from app.domain.session_resolution import REUSE_ACTIVE
from app.domain.user import User
from app.persistence.models.activity import ActivityModel
from app.persistence.models.interaction import InteractionModel
from app.persistence.repositories.interaction_record import (
    SqlAlchemyInteractionRecordRepository,
)
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]


# ================================================================== 测试底座


class _OkRuntime:
    """成功 Runtime：记录调用次数（e 区端到端用）。

    P1 / C1：投递是显式的第 7 段（由应用层在结果落库后发起）。
    """

    def __init__(self, result: RuntimeExecutionResult) -> None:
        self.result = result
        self.calls = 0
        self.deliveries: list = []

    def execute(self, inbound: InboundInteraction) -> RuntimeExecutionResult:
        self.calls += 1
        return self.result

    def deliver(self, outbound) -> None:
        self.deliveries.append(outbound)


def _uow_factory(db_engine) -> Callable[[], SqlAlchemyUnitOfWork]:
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine, with_session: bool = False) -> dict:
    """最小种子：user / channel / identity（+ 可选 session，满足 FK）。"""
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


def _create_pending(db_engine, channel_id: int, msg_id: str = "msg-1") -> int:
    """走 intake phase 落一条 PENDING（Worker / reclaim 的输入）。"""
    class _Never:
        def execute(self, inbound):
            raise AssertionError("create_pending 不应触发执行")

    inbound = InboundInteraction(
        channel_id=channel_id, external_interaction_id=msg_id,
        external_conversation_id="conv-1", external_identity_id="wx_a",
        content="你好")
    phases = PersistingCoordinator(inner=_Never(),  # type: ignore[arg-type]
                                   uow_factory=_uow_factory(db_engine))
    return phases.create_pending(inbound)


def _claim(db_engine, interaction_id: int) -> None:
    """以独立调用方身份 claim（模拟某个 Worker 的 claim 动作）。"""
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.claim_processing(interaction_id) is True
        uow.commit()


def _row(db_engine, interaction_id: int):
    with _uow_factory(db_engine)() as uow:
        return uow.interactions.get_by_id(interaction_id)


def _backdate(db_engine, interaction_id: int, seconds: int) -> None:
    """把 started_at 回拨（模拟「该 claim 已过去 seconds 秒」）——时间机器。

    确定性要求：reclaim 的 cutoff 由 DB 时钟计算，测试内无法让真实时间流逝；
    回拨等价于「claim 发生在 seconds 秒之前」，与 12.9.4.11 §4.4 的
    threshold ≫ 1s 论证一致（回拨量 120s ≫ 秒级精度）。
    """
    with Session(db_engine) as s:
        s.execute(text(
            "UPDATE interactions SET processing_started_at = "
            "datetime('now', :minus) WHERE id = :i"),
            {"minus": f"-{seconds} seconds", "i": interaction_id})
        s.commit()


def _activities(db_engine, interaction_id: int):
    with Session(db_engine) as s:
        rows = s.execute(select(ActivityModel).where(
            ActivityModel.interaction_id == interaction_id)).scalars().all()
        return [(r.type, r.stage, r.status) for r in rows]


def _coord(db_engine) -> PersistingCoordinator:
    class _Never:
        def execute(self, inbound):
            raise AssertionError("本测试不触发真实执行")

    return PersistingCoordinator(inner=_Never(),  # type: ignore[arg-type]
                                 uow_factory=_uow_factory(db_engine))


# ================================================================== A. Schema / Claim


def test_a1_claim_writes_db_clock_started_at(db_engine) -> None:
    """claim 前为 NULL；claim 成功 → 同一条 UPDATE 写入 DB 时钟的 started_at。"""
    g = _seed(db_engine)
    iid = _create_pending(db_engine, g["channel"])

    assert _row(db_engine, iid).processing_started_at is None   # PENDING：未 claim
    _claim(db_engine, iid)
    assert _row(db_engine, iid).processing_started_at is not None


def test_a2_failed_claim_does_not_touch_started_at(db_engine) -> None:
    """claim 失败（CAS 不命中）→ 不改写 started_at（UPDATE 未发生）。"""
    g = _seed(db_engine)
    iid = _create_pending(db_engine, g["channel"])
    _claim(db_engine, iid)
    t1 = _row(db_engine, iid).processing_started_at

    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.claim_processing(iid) is False   # 他人已 claim
        uow.commit()

    assert _row(db_engine, iid).processing_started_at == t1


def test_a3_worker_never_generates_timestamps() -> None:
    """Worker 源码不含任何时间生成原语——fence 值只能来自 DB 读回（12.9.4.11 §1.2）。"""
    src = (BACKEND / "app" / "application" / "worker.py").read_text(
        encoding="utf-8")
    for token in ("datetime.now", "utcnow", "func.now", "time.time"):
        assert token not in src, f"Worker 自己生成了时间戳：{token}"


def test_a4_claim_and_reclaim_keep_the_cas_shape() -> None:
    """实现形状守卫：claim / reclaim 都是条件化 UPDATE（rowcount 判定）。"""
    claim_src = inspect.getsource(
        SqlAlchemyInteractionRecordRepository.claim_processing)
    assert "PROCESSING_PENDING" in claim_src
    assert "processing_started_at=func.now()" in claim_src
    assert "rowcount" in claim_src

    reclaim_src = inspect.getsource(
        SqlAlchemyInteractionRecordRepository.reclaim_stale_processing)
    assert "PROCESSING_PROCESSING" in reclaim_src
    assert "is_not(None)" in reclaim_src            # started_at IS NOT NULL
    assert ".limit(" in reclaim_src                 # 有界
    assert "rowcount" in reclaim_src


# ================================================================== B. Outcome fence


def test_b1_matching_expected_commits(db_engine) -> None:
    """等式命中（列 == claim 后读回值）→ COMMITTED，终态落定，started_at 不变。"""
    g = _seed(db_engine)
    iid = _create_pending(db_engine, g["channel"])
    _claim(db_engine, iid)
    t = _row(db_engine, iid).processing_started_at

    with _uow_factory(db_engine)() as uow:
        commit = uow.interactions.record_outcome(
            iid, processing_status=PROCESSING_SUCCEEDED,
            delivery_status=DELIVERY_DELIVERED, expected_started_at=t)
        uow.commit()

    assert commit is OutcomeCommit.COMMITTED
    row = _row(db_engine, iid)
    assert (row.processing_status, row.delivery_status) == ("succeeded",
                                                            "delivered")
    assert row.processing_started_at == t            # outcome 不碰 fence 列


def test_b2_mismatched_expected_rejects_without_raising(db_engine) -> None:
    """等式失配 → REJECTED：正常竞争结果，**不抛异常**，行保持原 ownership。"""
    g = _seed(db_engine)
    iid = _create_pending(db_engine, g["channel"])
    _claim(db_engine, iid)
    t = _row(db_engine, iid).processing_started_at

    with _uow_factory(db_engine)() as uow:
        commit = uow.interactions.record_outcome(
            iid, processing_status=PROCESSING_SUCCEEDED,
            delivery_status=DELIVERY_DELIVERED,
            expected_started_at=datetime(2000, 1, 1))     # 别的 attempt 的值
        uow.commit()

    assert commit is OutcomeCommit.REJECTED
    row = _row(db_engine, iid)
    assert row.processing_status == "processing"     # ownership 未被动
    assert row.processing_started_at == t
    assert row.response_payload is None


def test_b3_legacy_call_without_fence_still_commits(db_engine) -> None:
    """无 fence 的既有形状（同步/种子路径直接落终态）行为不变。"""
    g = _seed(db_engine)
    iid = _create_pending(db_engine, g["channel"])

    with _uow_factory(db_engine)() as uow:
        commit = uow.interactions.record_outcome(
            iid, processing_status=PROCESSING_SUCCEEDED,
            delivery_status=DELIVERY_DELIVERED)          # 未传 expected
        uow.commit()

    assert commit is OutcomeCommit.COMMITTED
    assert _row(db_engine, iid).processing_status == "succeeded"


# ================================================================== C. reclaim


def test_c1_stale_processing_row_is_reclaimed_to_pending(db_engine) -> None:
    """超时 PROCESSING 孤儿 → PENDING + started_at=NULL（reclaim 必置 NULL）。"""
    g = _seed(db_engine)
    iid = _create_pending(db_engine, g["channel"])
    _claim(db_engine, iid)
    _backdate(db_engine, iid, seconds=120)               # 已停滞 120s

    with _uow_factory(db_engine)() as uow:
        reclaimed = uow.interactions.reclaim_stale_processing(30)
        uow.commit()

    assert reclaimed == 1
    row = _row(db_engine, iid)
    assert row.processing_status == "pending"
    assert row.processing_started_at is None             # D.2：必置 NULL


def test_c2_fresh_processing_row_is_not_reclaimed(db_engine) -> None:
    """未超阈值（threshold ≫ 实际年龄）→ 不回收。"""
    g = _seed(db_engine)
    iid = _create_pending(db_engine, g["channel"])
    _claim(db_engine, iid)                                # 刚刚 claim

    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.reclaim_stale_processing(30) == 0
        uow.commit()

    assert _row(db_engine, iid).processing_status == "processing"


def test_c3_pending_and_terminal_rows_are_untouched(db_engine) -> None:
    """只回收 processing 行：PENDING 与终态行绝不被动。"""
    g = _seed(db_engine)
    pending_id = _create_pending(db_engine, g["channel"], msg_id="msg-p")
    terminal_id = _create_pending(db_engine, g["channel"], msg_id="msg-t")
    with _uow_factory(db_engine)() as uow:
        uow.interactions.record_outcome(
            terminal_id, processing_status=PROCESSING_SUCCEEDED,
            delivery_status=DELIVERY_DELIVERED)
        uow.commit()

    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.reclaim_stale_processing(0) == 0
        uow.commit()

    assert _row(db_engine, pending_id).processing_status == "pending"
    assert _row(db_engine, terminal_id).processing_status == "succeeded"


def test_c4_reclaim_is_bounded(db_engine) -> None:
    """有界：limit=1 时单批最多回收一行，剩余的留给下一批。"""
    g = _seed(db_engine)
    first = _create_pending(db_engine, g["channel"], msg_id="msg-1")
    second = _create_pending(db_engine, g["channel"], msg_id="msg-2")
    _claim(db_engine, first)
    _claim(db_engine, second)
    _backdate(db_engine, first, seconds=120)
    _backdate(db_engine, second, seconds=120)

    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.reclaim_stale_processing(30, limit=1) == 1
        uow.commit()

    states = {_row(db_engine, first).processing_status,
              _row(db_engine, second).processing_status}
    assert states == {"pending", "processing"}            # 恰好回收一行

    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.reclaim_stale_processing(30, limit=1) == 1
        uow.commit()

    assert _row(db_engine, first).processing_status == "pending"
    assert _row(db_engine, second).processing_status == "pending"


# ================================================================== D. 核心回归
#     A claim T1 → reclaim → B claim T2 → A outcome(T1) → REJECTED
#     → B ownership untouched → B outcome(T2) → COMMITTED


def test_d1_old_attempt_cannot_write_through_new_attempt(db_engine) -> None:
    """整个 orphan recovery 设计最核心的 regression test（12.9.4.12 指定）。"""
    g = _seed(db_engine, with_session=True)
    phases = _coord(db_engine)
    iid = phases.create_pending(_inbound(g["channel"]))

    # 1) Worker A claim（T1），随后进程崩溃 / 停滞（时间机器回拨 = T1 已很老）
    _claim(db_engine, iid)
    _backdate(db_engine, iid, seconds=120)
    t1 = _row(db_engine, iid).processing_started_at       # A 持有的 claim 值

    # 2) reclaim：A 的 ownership 被剥夺，行退回 PENDING
    assert phases.reclaim_stale_processing(30) == 1

    # 3) Worker B claim（T2）
    _claim(db_engine, iid)
    t2 = _row(db_engine, iid).processing_started_at
    assert t2 is not None and t2 != t1                    # 阈值 ≫ 1s ⇒ 不同值

    # 4) A「执行完」了并晚到：outcome(T1) → REJECTED，绝不写穿 B
    late_commit = phases.record_failure(
        iid, _inbound(g["channel"]), RuntimeError("slow attempt failed"),
        expected_started_at=t1)
    assert late_commit is OutcomeCommit.REJECTED

    row = _row(db_engine, iid)                            # B ownership untouched
    assert row.processing_status == "processing"
    assert row.processing_started_at == t2
    assert row.delivery_status == "pending"
    assert row.response_payload is None
    assert all(t != ACTIVITY_STAGE_FAILURE                # A 的失败 Evidence 不落
               for t, _, _ in _activities(db_engine, iid))

    # 5) B outcome(T2) → COMMITTED，终态由 B 落定
    #    P1 / C3：成功被拆为「Agent 成功」（落结果）与「投递成功」两个 phase。
    result = RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=g["session"],
        agent_response=AgentResponse(content="ok"))
    b_commit = phases.record_agent_success(iid, result, expected_started_at=t2)
    assert b_commit is OutcomeCommit.COMMITTED

    row = _row(db_engine, iid)
    assert (row.processing_status, row.delivery_status) == ("succeeded",
                                                            "pending")
    assert row.processing_started_at == t2

    # 6) 投递终态单独落定（processing 不被改写）
    #    P2：投递终态的围栏是 **attempt 身份**（delivering ∧ attempts=我的号）
    #    ⇒ 先取得投递权（统一 claim 闸门），再落终态
    with _uow_factory(db_engine)() as uow:
        attempt = uow.interactions.claim_delivery(
            iid, max_attempts=3, lease_seconds=300)
        uow.commit()
    assert attempt == 1
    assert phases.record_outbound_success(
        iid, session_id=g["session"], expected_delivery_attempts=1) \
        is OutcomeCommit.COMMITTED
    row = _row(db_engine, iid)
    assert (row.processing_status, row.delivery_status) == ("succeeded",
                                                            "delivered")
    assert row.processing_started_at == t2


def test_d2_old_attempt_success_is_equally_rejected(db_engine) -> None:
    """对称性：旧 attempt 的**成功**结果同样写不穿新 attempt。"""
    g = _seed(db_engine, with_session=True)
    phases = _coord(db_engine)
    iid = phases.create_pending(_inbound(g["channel"]))

    _claim(db_engine, iid)
    _backdate(db_engine, iid, seconds=120)
    t1 = _row(db_engine, iid).processing_started_at
    assert phases.reclaim_stale_processing(30) == 1
    _claim(db_engine, iid)
    t2 = _row(db_engine, iid).processing_started_at

    result = RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=g["session"],
        agent_response=AgentResponse(content="stale"))
    assert phases.record_agent_success(
        iid, result, expected_started_at=t1) is OutcomeCommit.REJECTED

    row = _row(db_engine, iid)
    assert row.processing_status == "processing"          # B ownership untouched
    assert row.processing_started_at == t2
    assert row.response_payload is None                   # 旧响应不可伪造落地


# ================================================================== E. Worker 端到端


def test_e1_worker_completes_a_reclaimed_orphan(db_engine) -> None:
    """回收闭环：崩溃 Worker 留下的孤儿 → reclaim → 新 Worker 正常执行完成。"""
    g = _seed(db_engine, with_session=True)
    iid = _create_pending(db_engine, g["channel"])
    _claim(db_engine, iid)                                # 崩溃的 Worker A
    _backdate(db_engine, iid, seconds=120)

    phases = _coord(db_engine)
    assert phases.reclaim_stale_processing(30) == 1

    runtime = _OkRuntime(RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=g["session"],
        agent_response=AgentResponse(content="ok")))
    worker = Worker(runtime=runtime,
                    phases=PersistingCoordinator(
                        inner=runtime, uow_factory=_uow_factory(db_engine)),
                    uow_factory=_uow_factory(db_engine))
    assert worker.process(iid) is True                    # B 重新 pickup 并执行
    assert runtime.calls == 1

    row = _row(db_engine, iid)
    assert (row.processing_status, row.delivery_status) == ("succeeded",
                                                            "delivered")
    assert row.response_payload["content"] == "ok"


def test_e2_normal_worker_path_passes_the_fence(db_engine) -> None:
    """正常（未超阈值）路径 fence 恒过：执行 → COMMITTED，不误伤。"""
    g = _seed(db_engine, with_session=True)
    iid = _create_pending(db_engine, g["channel"])

    runtime = _OkRuntime(RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=g["session"],
        agent_response=AgentResponse(content="ok")))
    worker = Worker(runtime=runtime,
                    phases=PersistingCoordinator(
                        inner=runtime, uow_factory=_uow_factory(db_engine)),
                    uow_factory=_uow_factory(db_engine))

    assert worker.process(iid) is True
    row = _row(db_engine, iid)
    assert row.processing_status == "succeeded"
    assert row.processing_started_at is not None          # fence 列仍在（未被清）


# ================================================================== Schema 面


def test_f_eight_core_tables_plus_tenants() -> None:
    """12.9.4.12 只加列不加业务表：仍是 4.2 冻结的 8 张核心表；
    Cloud-0.1.1-B 追加 tenants root；Cloud-0.1.1-C 追加 2 张 Browser Auth 表。"""
    from app.persistence.database import Base
    assert set(Base.metadata.tables) == {
        "users", "identities", "channels", "agents", "connections",
        "sessions", "interactions", "activities", "tenants",
        "user_credentials", "browser_sessions",
        # Implementation Package 2 / Step 1：授权状态存储（非 4.2 业务表）。
        "connection_conversations",
        # M6.6-C3 / ADR-12：Bootstrap 窗口存储（不是授权状态）。
        "bootstrap_arms"}
    cols = {c.name for c in InteractionModel.__table__.columns}
    assert "processing_started_at" in cols                # 本步唯一 schema 变更
    assert InteractionModel.__table__.c.processing_started_at.nullable
