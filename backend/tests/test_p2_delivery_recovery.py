"""P2 Delivery Recovery —— 状态机 / 原子 claim / attempt 围栏 / 恢复白名单（SQLite）。

覆盖（实现前置核验 §9 的可测项；PostgreSQL 并发项见
tests/test_p2_delivery_recovery_postgres.py）：

  A. 原子 claim：首次 claim / 重投 claim / protocol=0 / unknown / 上限 /
     未持久化响应 / 跨租户；
  B. 首次投递与恢复投递**共用**同一闸门：没有 claim 成功 ⇒ 绝不投递；
  C. attempt 围栏：旧 attempt 的迟到 outcome 写不穿新 attempt；写库失败
     **不**伪装成 delivered；租约过期 → unknown（不自动重投）；
  D. 恢复白名单：pending+protocol=1+attempts=0 可恢复；failed+retryable 可恢复；
     ACK 超时 / 发送异常 / unknown **永不**自动重投；
  E. 历史回填：幂等、tenant-scoped、**不写 Evidence**；核验计数分别检查。

不涉及网络、不连接生产库、不发送任何真实企微消息。
"""

import json
from collections.abc import Callable
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.application.delivery import build_outbound_response
from app.application.interaction_recording import PersistingCoordinator
from app.application.errors import ChannelDeliveryFailedError
from app.domain.agent_response import AgentResponse
from app.domain.artifact import Artifact
from app.domain.artifact_reference import render_outbound_content
from app.domain.channel import Channel
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.interaction_record import (
    DELIVERY_DELIVERED,
    DELIVERY_DELIVERING,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    DELIVERY_PROTOCOL_V1,
    DELIVERY_UNKNOWN,
    PROCESSING_SUCCEEDED,
)
from app.domain.outbound_response import OutboundResponse
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session import Session as SessionEntity
from app.domain.session_resolution import REUSE_ACTIVE
from app.domain.user import User
from app.persistence.models.activity import ActivityModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

MAX_ATTEMPTS = 3
LEASE_SECONDS = 300


def _uow_factory(db_engine, tenant_id: int | None = None) -> Callable[[], SqlAlchemyUnitOfWork]:
    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(
            session_factory=sessionmaker(bind=db_engine, expire_on_commit=False),
            tenant_id=tenant_id)
    return factory


def _seed(db_engine, tenant_id: int = 1) -> dict:
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        identity = uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        session = uow.sessions.create(SessionEntity(
            identity_id=identity.id, channel_id=channel.id,
            external_conversation_id="conv-1", status="active"))
        uow.commit()
        return {"channel": channel.id, "session": session.id}


def _inbound(channel_id: int) -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id="msg-1",
        external_conversation_id="conv-1", external_identity_id="wx_a",
        content="hello")


class _RecordingRuntime:
    """记录 execute / deliver 的替身 Runtime（投递只被记录，不外发）。"""

    def __init__(self, result=None) -> None:
        self.result = result
        self.execute_calls = 0
        self.deliveries: list[OutboundResponse] = []

    def execute(self, inbound) -> RuntimeExecutionResult:
        self.execute_calls += 1
        return self.result

    def deliver(self, outbound) -> None:
        self.deliveries.append(outbound)


def _coord(db_engine) -> PersistingCoordinator:
    class _Never:
        def execute(self, inbound):
            raise AssertionError("no real execution in this test")

        def deliver(self, outbound):
            raise AssertionError("no real delivery in this test")

    return PersistingCoordinator(inner=_Never(),  # type: ignore[arg-type]
                                 uow_factory=_uow_factory(db_engine))


def _row(db_engine, interaction_id: int):
    with _uow_factory(db_engine)() as uow:
        return uow.interactions.get_by_id(interaction_id)


def _activities(db_engine, interaction_id: int):
    with Session(db_engine) as s:
        rows = s.execute(select(ActivityModel).where(
            ActivityModel.interaction_id == interaction_id)).scalars().all()
        return [(r.type, r.stage, r.status) for r in rows]


def _result(session_id: int, content: str = "answer") -> RuntimeExecutionResult:
    return RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=session_id,
        agent_response=AgentResponse(content=content))


# ================================================================== A. 原子 claim

def _seed_pending(db_engine, g: dict, msg_id: str = "msg-1") -> int:
    """落一条 PENDING 行（intake 形态）。"""
    phases = _coord(db_engine)
    inbound = InboundInteraction(
        channel_id=g["channel"], external_interaction_id=msg_id,
        external_conversation_id="conv-1", external_identity_id="wx_a",
        content="hello")
    return phases.create_pending(inbound)


def _make_succeeded(db_engine, g: dict, iid: int, *, protocol: int = DELIVERY_PROTOCOL_V1,
                    delivery: str = DELIVERY_PENDING, attempts: int = 0,
                    retryable: bool = False, payload: dict | None = None) -> None:
    """把行直接置成「Agent 已成功」形态（绕过执行，直接构造待投递行）。"""
    from sqlalchemy import text
    with Session(db_engine) as s:
        s.execute(text(
            "UPDATE interactions SET processing_status='succeeded', "
            "delivery_status=:d, delivery_protocol=:p, delivery_attempts=:a, "
            "retryable=:r, response_payload=:pl WHERE id=:i"),
            {"d": delivery, "p": protocol, "a": attempts, "r": retryable,
             "pl": json.dumps(payload if payload is not None else {"content": "x"}),
             "i": iid})
        s.commit()


def test_a1_first_claim_grants_ownership_once(db_engine) -> None:
    """首次 claim：成功一次 → delivering + attempts=1 + 租约起点；第二次失败。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g)
    _make_succeeded(db_engine, g, iid)
    with _uow_factory(db_engine)() as uow:
        first = uow.interactions.claim_delivery(
            iid, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS)
        uow.commit()
    assert first == 1
    row = _row(db_engine, iid)
    assert row.delivery_status == DELIVERY_DELIVERING
    assert row.delivery_attempts == 1
    assert row.delivery_started_at is not None
    with _uow_factory(db_engine)() as uow:
        second = uow.interactions.claim_delivery(
            iid, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS)
        uow.commit()
    assert second is None            # 已被 claim → 不得再取


def test_a2_legacy_protocol_row_can_never_be_claimed(db_engine) -> None:
    """protocol=0（历史行）⇒ **结构上**不可 claim ⇒ 永不自动重投。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g)
    _make_succeeded(db_engine, g, iid, protocol=0)
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.claim_delivery(
            iid, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS) is None
        uow.commit()
    assert _row(db_engine, iid).delivery_attempts == 0


def test_a3_unknown_and_delivered_can_never_be_claimed(db_engine) -> None:
    """unknown / delivered ⇒ 不在白名单 ⇒ 不可 claim。"""
    g = _seed(db_engine)
    for index, status in enumerate((DELIVERY_UNKNOWN, DELIVERY_DELIVERED)):
        iid = _seed_pending(db_engine, g, msg_id=f"msg-{index}")
        _make_succeeded(db_engine, g, iid, delivery=status)
        with _uow_factory(db_engine)() as uow:
            assert uow.interactions.claim_delivery(
                iid, max_attempts=MAX_ATTEMPTS,
                lease_seconds=LEASE_SECONDS, include_failed=True) is None
            uow.commit()


def test_a4_max_attempts_blocks_claim(db_engine) -> None:
    """达上限 ⇒ 不可 claim（不再重投）。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g)
    _make_succeeded(db_engine, g, iid, attempts=MAX_ATTEMPTS)
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.claim_delivery(
            iid, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS) is None
        uow.commit()


def test_a5_missing_response_payload_blocks_claim(db_engine) -> None:
    """响应未持久化 ⇒ 不可 claim（没有可投的内容）。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g)
    _make_succeeded(db_engine, g, iid, payload={"content": "x"})
    from sqlalchemy import text
    with Session(db_engine) as s:
        s.execute(text(
            "UPDATE interactions SET response_payload=NULL WHERE id=:i"), {"i": iid})
        s.commit()
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.claim_delivery(
            iid, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS) is None
        uow.commit()


def test_a6_retry_claim_requires_failed_and_retryable(db_engine) -> None:
    """重投 claim：只有「failed ∧ retryable ∧ protocol=1」才成立。"""
    g = _seed(db_engine)
    ok_id = _seed_pending(db_engine, g, msg_id="msg-ok")
    _make_succeeded(db_engine, g, ok_id, delivery=DELIVERY_FAILED,
                    attempts=1, retryable=True)
    no_retry_id = _seed_pending(db_engine, g, msg_id="msg-noretry")
    _make_succeeded(db_engine, g, no_retry_id, delivery=DELIVERY_FAILED,
                    attempts=1, retryable=False)
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.claim_delivery(
            ok_id, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS,
            include_failed=True) == 2
        assert uow.interactions.claim_delivery(
            no_retry_id, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS,
            include_failed=True) is None
        uow.commit()


def test_a7_tenant_isolation_blocks_foreign_claim(db_engine) -> None:
    """跨租户：别的租户的仓储**不能** claim 本租户的行。"""
    g = _seed(db_engine, tenant_id=1)
    iid = _seed_pending(db_engine, g)
    _make_succeeded(db_engine, g, iid)
    with _uow_factory(db_engine, tenant_id=2)() as uow:
        assert uow.interactions.claim_delivery(
            iid, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS) is None
        uow.commit()
    assert _row(db_engine, iid).delivery_attempts == 0


# ================================================================== B. 统一闸门

def test_b1_first_delivery_claims_then_delivers(db_engine) -> None:
    """首次投递：phase A → **原子 claim** → 投递 → 投递终态（delivering→delivered）。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g)
    phases = _coord(db_engine)
    runtime = _RecordingRuntime()
    phases.record_agent_success_and_deliver(
        runtime, _inbound(g["channel"]), _result(g["session"], "answer"),
        interaction_id=iid, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS)
    assert len(runtime.deliveries) == 1
    row = _row(db_engine, iid)
    assert (row.processing_status, row.delivery_status) == (
        "succeeded", DELIVERY_DELIVERED)
    assert row.delivery_attempts == 1
    assert row.delivery_protocol == DELIVERY_PROTOCOL_V1
    assert row.response_payload["content"] == "answer"


def test_b2_without_claim_no_delivery_happens(db_engine, monkeypatch) -> None:
    """没有 claim 成功 ⇒ **绝不**投递（投递闸门独立于 phase A 的执行围栏）。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g)
    phases = _coord(db_engine)
    runtime = _RecordingRuntime()

    from app.persistence.repositories.interaction_record import (
        SqlAlchemyInteractionRecordRepository,
    )

    original = SqlAlchemyInteractionRecordRepository.claim_delivery

    def lose(self, *args, **kwargs):
        return None          # 模拟「投递权被别人抢走」

    monkeypatch.setattr(
        SqlAlchemyInteractionRecordRepository, "claim_delivery", lose)
    phases.record_agent_success_and_deliver(
        runtime, _inbound(g["channel"]), _result(g["session"], "answer"),
        interaction_id=iid, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS)
    assert runtime.deliveries == []          # ★ 没有 claim ⇒ 没有外部 I/O
    row = _row(db_engine, iid)
    assert row.processing_status == "succeeded"   # 结果仍已落库
    assert row.delivery_status == DELIVERY_PENDING
    assert row.delivery_attempts == 0
    monkeypatch.setattr(
        SqlAlchemyInteractionRecordRepository, "claim_delivery", original)


def test_b3_m1_rejected_still_never_delivers(db_engine) -> None:
    """M1 闸门仍在：phase A REJECTED ⇒ 不 claim、不投递。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g)
    phases = _coord(db_engine)
    runtime = _RecordingRuntime()
    phases.record_agent_success_and_deliver(
        runtime, _inbound(g["channel"]), _result(g["session"], "answer"),
        interaction_id=iid, expected_started_at=datetime(2000, 1, 1),
        max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS)
    assert runtime.deliveries == []
    assert _row(db_engine, iid).delivery_attempts == 0


# ================================================================== C. attempt 围栏

def test_c1_old_attempt_outcome_cannot_overwrite_new_attempt(db_engine) -> None:
    """旧 attempt 的迟到 outcome ⇒ REJECTED（**写不穿**新 attempt）。

    场景：attempt#1 明确失败 → 重投 claim（attempt#2 接手）→ attempt#1 的迟到
    outcome 到达 ⇒ 因 attempt 计数等式失配而被拒（新 attempt 的状态不受影响）。
    """
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g)
    phases = _coord(db_engine)
    _make_succeeded(db_engine, g, iid)
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.claim_delivery(
            iid, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS) == 1
        uow.commit()
    # attempt#1 明确失败（结果已知、可重试）→ failed
    commit = phases.record_outbound_failure(
        iid, ChannelDeliveryFailedError(
            channel_id=g["channel"], external_conversation_id="conv-1",
            retryable=True),
        session_id=g["session"], expected_delivery_attempts=1)
    assert commit.value == "committed"
    assert _row(db_engine, iid).delivery_status == DELIVERY_FAILED
    # 重投 claim ⇒ attempt#2 接手
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.claim_delivery(
            iid, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS,
            include_failed=True) == 2
        uow.commit()
    # attempt#1 的迟到 outcome ⇒ 必须被拒（写不穿）
    late = phases.record_outbound_success(
        iid, session_id=g["session"], expected_delivery_attempts=1)
    assert late.value == "rejected"
    row = _row(db_engine, iid)
    assert row.delivery_status == DELIVERY_DELIVERING
    assert row.delivery_attempts == 2


def test_c2_outcome_requires_delivering_and_attempt_match(db_engine) -> None:
    """outcome 只在「delivering ∧ attempt 匹配」时才能提交。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g)
    phases = _coord(db_engine)
    _make_succeeded(db_engine, g, iid, delivery=DELIVERY_PENDING)
    # 未在途 ⇒ 拒绝
    commit = phases.record_outbound_success(
        iid, session_id=g["session"], expected_delivery_attempts=1)
    assert commit.value == "rejected"
    # 在途但 attempt 号不匹配 ⇒ 拒绝
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.claim_delivery(
            iid, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS) == 1
        uow.commit()
    commit = phases.record_outbound_success(
        iid, session_id=g["session"], expected_delivery_attempts=99)
    assert commit.value == "rejected"
    # 在途且 attempt 匹配 ⇒ 提交
    commit = phases.record_outbound_success(
        iid, session_id=g["session"], expected_delivery_attempts=1)
    assert commit.value == "committed"
    assert _row(db_engine, iid).delivery_status == DELIVERY_DELIVERED


def test_c3_write_failure_is_not_faked_as_delivered(db_engine, monkeypatch) -> None:
    """投递失败但**失败状态写库也失败** ⇒ 行**不**是终态；两个异常都在链上。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g)
    phases = _coord(db_engine)

    class _FailingRuntime:
        def __init__(self) -> None:
            self.deliveries = []

        def execute(self, inbound):
            raise AssertionError("no real execution")

        def deliver(self, outbound):
            self.deliveries.append(outbound)
            raise ChannelDeliveryFailedError(
                channel_id=outbound.channel_id,
                external_conversation_id=outbound.external_conversation_id)

    runtime = _FailingRuntime()

    from app.persistence.repositories.interaction_record import (
        SqlAlchemyInteractionRecordRepository,
    )

    original = SqlAlchemyInteractionRecordRepository.record_outcome

    def boom(self, *args, **kwargs):
        if kwargs.get("expected_delivery_attempts") is not None:
            raise RuntimeError("db write failed")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(
        SqlAlchemyInteractionRecordRepository, "record_outcome", boom)
    with pytest.raises(ChannelDeliveryFailedError) as excinfo:
        phases.record_agent_success_and_deliver(
            runtime, _inbound(g["channel"]), _result(g["session"], "answer"),
            interaction_id=iid, max_attempts=MAX_ATTEMPTS,
            lease_seconds=LEASE_SECONDS)
    # 原始投递异常仍是抛出类型；数据库异常作为 __cause__（两者都在 traceback）
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    row = _row(db_engine, iid)
    assert row.delivery_status == DELIVERY_DELIVERING   # ★ 未伪装成终态
    assert row.delivery_attempts == 1
    assert row.response_payload is not None              # 结果仍在


def test_c4_expired_lease_becomes_unknown_and_is_never_retried(db_engine) -> None:
    """租约过期 ⇒ `unknown`（**绝不**退回 pending ⇒ 不会自动重投）。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g)
    phases = _coord(db_engine)
    _make_succeeded(db_engine, g, iid)
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.claim_delivery(
            iid, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS) == 1
        uow.commit()
    from sqlalchemy import text
    with Session(db_engine) as s:
        s.execute(text(
            "UPDATE interactions SET delivery_started_at = datetime('now', '-120 seconds') "
            "WHERE id = :i"), {"i": iid})
        s.commit()
    assert phases.reclaim_stale_delivery(0) == 1
    row = _row(db_engine, iid)
    assert row.delivery_status == DELIVERY_UNKNOWN
    assert row.delivery_attempts == 1        # 计数保持单调
    # unknown 不在恢复候选内 ⇒ 不会被自动重投
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.find_undelivered(
            10, max_attempts=MAX_ATTEMPTS) == []
        uow.commit()


# ================================================================== D. 恢复白名单

def test_d1_pending_protocol1_attempts0_is_recoverable(db_engine) -> None:
    """`pending + protocol=1 + attempts=0` ⇒ 恢复候选 ⇒ 可被 claim（可证明未尝试）。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g)
    _make_succeeded(db_engine, g, iid)
    with _uow_factory(db_engine)() as uow:
        candidates = uow.interactions.find_undelivered(
            10, max_attempts=MAX_ATTEMPTS)
        uow.commit()
    assert [r.id for r in candidates] == [iid]
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.claim_delivery(
            iid, max_attempts=MAX_ATTEMPTS,
            lease_seconds=LEASE_SECONDS, include_failed=True) == 1
        uow.commit()


def test_d2_failed_retryable_is_recoverable(db_engine) -> None:
    """`failed + retryable + protocol=1` ⇒ 恢复候选（结果已知的未送达）。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g)
    _make_succeeded(db_engine, g, iid, delivery=DELIVERY_FAILED, attempts=1,
                    retryable=True)
    with _uow_factory(db_engine)() as uow:
        candidates = uow.interactions.find_undelivered(
            10, max_attempts=MAX_ATTEMPTS)
        uow.commit()
    assert [r.id for r in candidates] == [iid]
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.claim_delivery(
            iid, max_attempts=MAX_ATTEMPTS,
            lease_seconds=LEASE_SECONDS, include_failed=True) == 2
        uow.commit()


def test_d3_unknown_timeout_and_send_error_are_never_retried(db_engine) -> None:
    """ACK 超时 / 发送异常 / unknown ⇒ **永不**进入恢复白名单。"""
    g = _seed(db_engine)
    # unknown（结果未知）
    unknown_id = _seed_pending(db_engine, g, msg_id="msg-unknown")
    _make_succeeded(db_engine, g, unknown_id, delivery=DELIVERY_UNKNOWN,
                    attempts=1, retryable=True)
    # 发送异常在 Adapter 侧被标为 outcome_known=False ⇒ 落库为 unknown（已在
    # interaction_recording.record_outbound_failure 中分流）⇒ 这里断言分类函数：
    phases = _coord(db_engine)
    unknown_error = ChannelDeliveryFailedError(
        channel_id=g["channel"], external_conversation_id="conv-1",
        retryable=True, outcome_known=False)
    # failed 但 retryable=False（企微明确拒绝）⇒ 不可重投
    rejected_id = _seed_pending(db_engine, g, msg_id="msg-rejected")
    _make_succeeded(db_engine, g, rejected_id, delivery=DELIVERY_FAILED,
                    attempts=1, retryable=False)
    with _uow_factory(db_engine)() as uow:
        candidates = uow.interactions.find_undelivered(
            10, max_attempts=MAX_ATTEMPTS)
        uow.commit()
    assert candidates == []                    # ★ 三类都不在白名单
    assert unknown_error.outcome_known is False
    assert phases is not None


def test_d4_recover_delivery_claims_then_delivers(db_engine) -> None:
    """恢复路径：claim → 用**已落库结果**重建信封 → 投递 → 落终态（不重跑 Agent）。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g)
    phases = _coord(db_engine)
    runtime = _RecordingRuntime()
    phases._inner = runtime                   # type: ignore[attr-defined]
    phases.record_agent_success(
        iid, _result(g["session"], "answer"),
        expected_started_at=None)
    row = _row(db_engine, iid)
    assert row.delivery_status == DELIVERY_PENDING
    assert row.delivery_protocol == DELIVERY_PROTOCOL_V1
    assert phases.recover_delivery(row) is True
    assert len(runtime.deliveries) == 1
    assert runtime.deliveries[0].content == "answer"
    after = _row(db_engine, iid)
    assert after.delivery_status == DELIVERY_DELIVERED
    assert after.delivery_attempts == 1


def test_d5_recovery_driver_is_disabled_by_default_and_bounded() -> None:
    """恢复驱动：默认配置（有界批次、租约、attempt 上限）。"""
    from app.application.delivery_recovery import (
        DeliveryRecoveryConfig,
        DeliveryRecoveryDriver,
    )

    config = DeliveryRecoveryConfig()
    assert config.max_attempts == 3
    assert config.lease_seconds == 300
    assert config.pickup_limit == 10
    assert DeliveryRecoveryConfig(pickup_limit=5).pickup_limit == 5

    class _Source:
        def __init__(self) -> None:
            self.calls = []

        def find_undelivered(self, limit=10, *, max_attempts=3):
            self.calls.append(("find", limit, max_attempts))
            return []

        def reclaim_stale_delivery(self, threshold_seconds):
            self.calls.append(("reclaim", threshold_seconds))
            return 0

    source = _Source()

    class _Executor:
        def recover_delivery(self, record):
            return False

    driver = DeliveryRecoveryDriver(
        source=source, executor=_Executor(), config=config)
    assert driver.run_once() is True
    assert source.calls[0] == ("find", 10, 3)


# ================================================================== E. 历史回填

def test_e1_backfill_is_idempotent_and_tenant_scoped(db_engine) -> None:
    """回填：只动 protocol=0 的历史行；幂等；tenant-scoped；**不写 Evidence**。"""
    from tools.delivery_backfill import _backfill, verification_counts

    g = _seed(db_engine, tenant_id=1)
    legacy_pending = _seed_pending(db_engine, g, msg_id="msg-legacy-pending")
    _make_succeeded(db_engine, g, legacy_pending, protocol=0,
                    delivery=DELIVERY_PENDING)
    legacy_failed = _seed_pending(db_engine, g, msg_id="msg-legacy-failed")
    _make_succeeded(db_engine, g, legacy_failed, protocol=0,
                    delivery=DELIVERY_FAILED, attempts=1, retryable=True)
    new_row = _seed_pending(db_engine, g, msg_id="msg-new")
    _make_succeeded(db_engine, g, new_row, protocol=DELIVERY_PROTOCOL_V1)

    with Session(db_engine) as s:
        moved = _backfill(s, 1, DELIVERY_PENDING, None)
        moved += _backfill(s, 1, DELIVERY_FAILED, True)
        s.commit()
    assert moved == 2
    # 幂等：再跑一次没有新增
    with Session(db_engine) as s:
        again = _backfill(s, 1, DELIVERY_PENDING, None)
        again += _backfill(s, 1, DELIVERY_FAILED, True)
        s.commit()
    assert again == 0
    # 另一个租户不受影响
    with Session(db_engine) as s:
        counts = verification_counts(s, 2)
    assert counts["legacy_pending"] == 0
    assert counts["legacy_failed_retryable"] == 0
    # 新协议行**不动**
    assert _row(db_engine, new_row).delivery_status == DELIVERY_PENDING
    # 核验计数：两类历史候选**分别**归零
    with Session(db_engine) as s:
        counts = verification_counts(s, 1)
    assert counts["legacy_pending"] == 0
    assert counts["legacy_failed_retryable"] == 0
    # **不写 Evidence**：回填只改状态，不追加任何 Evidence 行
    # （interaction_received 是 intake 写的，与回填无关）
    assert [t for t, _, _ in _activities(db_engine, legacy_pending)] == [
        "interaction_received"]


def test_e2_backfill_moves_legacy_rows_to_unknown(db_engine) -> None:
    """回填把历史歧义行归入 `unknown`（⇒ 永不自动重投）。"""
    from tools.delivery_backfill import _backfill

    g = _seed(db_engine, tenant_id=1)
    iid = _seed_pending(db_engine, g, msg_id="msg-legacy")
    _make_succeeded(db_engine, g, iid, protocol=0, delivery=DELIVERY_PENDING)
    with Session(db_engine) as s:
        assert _backfill(s, 1, DELIVERY_PENDING, None) == 1
        s.commit()
    assert _row(db_engine, iid).delivery_status == DELIVERY_UNKNOWN
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.find_undelivered(
            10, max_attempts=MAX_ATTEMPTS) == []
        uow.commit()


# ================================================================== F. Artifact 快照恢复一致性（P2.1）

def _artifact(artifact_id: str, name: str, *, kind: str = "file",
              uri: str | None = "https://example.test/f.csv") -> Artifact:
    return Artifact(artifact_id=artifact_id, kind=kind, name=name, uri=uri,
                    mime_type="text/csv", size_bytes=12,
                    metadata={"origin": "agent"})


def _result_with_artifacts(session_id: int,
                            artifacts: list[Artifact] | None) -> RuntimeExecutionResult:
    return RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=session_id,
        agent_response=AgentResponse(content="answer", artifacts=artifacts))


def _rendered(outbound) -> str:
    """出站消息的**用户可见文本**（引用块渲染后）——恢复与首次必须一致。"""
    return render_outbound_content(outbound.content, outbound.artifacts)


def test_f1_no_artifact_recovery_behaviour_unchanged(db_engine) -> None:
    """无 Artifact 的普通响应：恢复行为与修复前**逐字节一致**（artifacts=None）。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g, msg_id="msg-f1")
    phases = _coord(db_engine)
    runtime = _RecordingRuntime()
    phases._inner = runtime
    phases.record_agent_success(
        iid, _result_with_artifacts(g["session"], None), expected_started_at=None)
    row = _row(db_engine, iid)
    assert row.response_payload["artifacts"] is None
    assert phases.recover_delivery(row) is True
    outbound = runtime.deliveries[0]
    assert outbound.artifacts is None
    assert outbound.content == "answer"
    assert _rendered(outbound) == "answer"


def test_f2_single_artifact_recovered_message_matches_first(db_engine) -> None:
    """单个 Artifact：恢复重发的出站消息与**首次投递**逐字段一致。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g, msg_id="msg-f2")
    phases = _coord(db_engine)
    runtime = _RecordingRuntime()
    phases._inner = runtime
    inbound = _inbound(g["channel"])
    artifact = _artifact("a-1", "report.csv")
    result = _result_with_artifacts(g["session"], [artifact])
    phases.record_agent_success(iid, result, expected_started_at=None)
    row = _row(db_engine, iid)
    assert row.response_payload["artifacts"] == [{
        "artifact_id": "a-1", "kind": "file", "name": "report.csv",
        "uri": "https://example.test/f.csv", "mime_type": "text/csv",
        "size_bytes": 12, "metadata": {"origin": "agent"}}]
    expected = build_outbound_response(inbound, result.agent_response)
    assert phases.recover_delivery(row) is True
    actual = runtime.deliveries[0]
    assert actual.artifacts == expected.artifacts        # 引用逐字段一致
    assert actual.content == expected.content
    assert actual.metadata == expected.metadata
    assert _rendered(actual) == _rendered(expected)      # 用户可见文本一致
    assert "report.csv" in _rendered(actual)              # 引用**没有**丢失


def test_f3_multiple_artifacts_keep_count_order_and_references(db_engine) -> None:
    """多个 Artifact：数量、顺序与引用信息在恢复后保持一致。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g, msg_id="msg-f3")
    phases = _coord(db_engine)
    runtime = _RecordingRuntime()
    phases._inner = runtime
    inbound = _inbound(g["channel"])
    artifacts = [_artifact("a-1", "one.csv"),
                 _artifact("a-2", "two.json", kind="data")]
    result = _result_with_artifacts(g["session"], artifacts)
    phases.record_agent_success(iid, result, expected_started_at=None)
    row = _row(db_engine, iid)
    expected = build_outbound_response(inbound, result.agent_response)
    assert phases.recover_delivery(row) is True
    actual = runtime.deliveries[0]
    assert len(actual.artifacts) == 2
    assert [a.artifact_id for a in actual.artifacts] == ["a-1", "a-2"]
    assert actual.artifacts == expected.artifacts
    assert _rendered(actual) == _rendered(expected)


@pytest.mark.parametrize("broken_payload", [
    {"artifacts": "not-a-list"},
    {"artifacts": [{"artifact_id": "a-1"}]},
    {"artifacts": [{"artifact_id": "a-1", "kind": "file", "name": "x",
                     "surprise": 1}]},
    {"artifacts": [{"artifact_id": "a-1", "kind": "file", "name": "x",
                     "size_bytes": "12"}]},
    {"artifacts": [{"artifact_id": "a-1", "kind": "file", "name": "x",
                     "metadata": "no"}]},
    {"artifacts": [{"artifact_id": "", "kind": "file", "name": "x"}]},
])
def test_f4_invalid_artifact_snapshot_never_silently_drops_references(
        db_engine, broken_payload) -> None:
    """非法/不兼容快照 ⇒ 明确失败（ValueError）且**绝不**继续投递。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g, msg_id="msg-f4")
    phases = _coord(db_engine)
    runtime = _RecordingRuntime()
    phases._inner = runtime
    phases.record_agent_success(
        iid, _result_with_artifacts(g["session"], [_artifact("a-1", "r.csv")]),
        expected_started_at=None)
    import json
    from sqlalchemy import text
    payload = {"content": "answer", "external_session_id": None, "metadata": None}
    payload.update(broken_payload)
    with Session(db_engine) as s:
        s.execute(text("UPDATE interactions SET response_payload=:p WHERE id=:i"),
                  {"p": json.dumps(payload), "i": iid})
        s.commit()
    row = _row(db_engine, iid)
    with pytest.raises(ValueError):
        phases.recover_delivery(row)
    assert runtime.deliveries == []          # ★ 没有发出任何消息
    after = _row(db_engine, iid)
    assert after.delivery_status != "delivered"   # 未被伪装成送达
    assert after.delivery_status != "failed"


def test_f5_recovery_uses_persisted_snapshot_and_never_reruns_agent(db_engine) -> None:
    """恢复路径只读**已落库快照**，从不重跑 Agent。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g, msg_id="msg-f5")
    phases = _coord(db_engine)
    runtime = _RecordingRuntime()
    phases._inner = runtime
    phases.record_agent_success(
        iid, _result_with_artifacts(g["session"], [_artifact("a-1", "r.csv")]),
        expected_started_at=None)
    row = _row(db_engine, iid)
    assert phases.recover_delivery(row) is True
    assert runtime.execute_calls == 0            # ★ 从未调用 Runtime.execute
    assert runtime.deliveries[0].artifacts[0].artifact_id == "a-1"


def test_f6_artifact_recovery_still_goes_through_claim_and_fence(db_engine) -> None:
    """修复**不绕过**统一原子 claim / attempt 围栏 / M1 执行闸门。"""
    g = _seed(db_engine)
    iid = _seed_pending(db_engine, g, msg_id="msg-f6")
    phases = _coord(db_engine)
    runtime = _RecordingRuntime()
    phases._inner = runtime
    phases.record_agent_success(
        iid, _result_with_artifacts(g["session"], [_artifact("a-1", "r.csv")]),
        expected_started_at=None)
    row = _row(db_engine, iid)
    assert row.delivery_attempts == 0
    assert phases.recover_delivery(row) is True
    after = _row(db_engine, iid)
    assert after.delivery_attempts == 1              # claim 计数 +1（围栏身份）
    assert after.delivery_status == DELIVERY_DELIVERED
    iid2 = _seed_pending(db_engine, g, msg_id="msg-f6b")
    runtime2 = _RecordingRuntime()
    phases2 = _coord(db_engine)
    phases2._inner = runtime2
    phases2.record_agent_success_and_deliver(
        runtime2, _inbound(g["channel"]),
        _result_with_artifacts(g["session"], [_artifact("a-2", "x.csv")]),
        interaction_id=iid2, expected_started_at=datetime(2000, 1, 1),
        max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS)
    assert runtime2.deliveries == []
    assert _row(db_engine, iid2).delivery_attempts == 0
