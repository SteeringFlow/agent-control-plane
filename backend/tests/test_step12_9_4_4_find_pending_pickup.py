"""Step 12.9.4.4 —— find_pending() Persistence API（只解决 Pickup）。

语义严格限定：

    find_pending(limit) → 当前 PENDING 的 Interaction，**纯读**
                          不产生任何 claim，不改变任何状态

验收：
A  PENDING 能找到
B  PROCESSING / SUCCEEDED / FAILED 都找不到
C  多条按 created_at 升序返回（同秒并列时以 id 保序）
D  limit 生效（含默认上限）
E  空队列 → []
F  find_pending 本身绝不改变状态（调用后仍可被 claim 成功）
G  普通 SQL、无 FOR UPDATE、无 schema 变更、且尚未接入生产 Runtime

边界（本步不做）：Worker、同步执行路径改造、任何生产代码调用 pickup。
"""

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.domain.channel import Channel
from app.domain.interaction_record import (
    DELIVERY_DELIVERED,
    DELIVERY_PENDING,
    PROCESSING_FAILED,
    PROCESSING_SUCCEEDED,
    InteractionRecord,
)
from app.domain.user import User
from app.persistence.models.interaction import InteractionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
_EARLY = datetime(2020, 1, 1, 0, 0, 0)


# ================================================================== 测试底座


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine) -> int:
    """最小种子（满足 interactions.channel_id FK），返回 channel_id。"""
    with _uow_factory(db_engine)() as uow:
        uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        uow.commit()
        return channel.id


def _make(db_engine, channel_id: int, msg_id: str, status: str) -> int:
    """造一条指定状态的 interaction，返回 id。"""
    with _uow_factory(db_engine)() as uow:
        rec = uow.interactions.create(InteractionRecord(
            direction="inbound", channel_id=channel_id,
            external_interaction_id=msg_id,
            request_payload={"content": msg_id}))
        uow.commit()
        interaction_id = rec.id
    if status != "pending":
        with _uow_factory(db_engine)() as uow:
            if status == "processing":
                uow.interactions.claim_processing(interaction_id)
            elif status == "succeeded":
                uow.interactions.record_outcome(
                    interaction_id, processing_status=PROCESSING_SUCCEEDED,
                    delivery_status=DELIVERY_DELIVERED)
            elif status == "failed":
                uow.interactions.record_outcome(
                    interaction_id, processing_status=PROCESSING_FAILED,
                    delivery_status=DELIVERY_PENDING, failure_code="X")
            uow.commit()
    return interaction_id


def _pending(db_engine, limit: int = 10):
    with _uow_factory(db_engine)() as uow:
        return uow.interactions.find_pending(limit)


# ================================================================== A. PENDING 能找到


def test_a1_pending_row_is_found(db_engine) -> None:
    channel_id = _seed(db_engine)
    interaction_id = _make(db_engine, channel_id, "msg-1", "pending")

    rows = _pending(db_engine)
    assert [r.id for r in rows] == [interaction_id]
    assert rows[0].processing_status == "pending"


# ================================================================== B. 非 PENDING 找不到


@pytest.mark.parametrize("status", ["processing", "succeeded", "failed"])
def test_b1_non_pending_rows_are_not_found(db_engine, status) -> None:
    channel_id = _seed(db_engine)
    _make(db_engine, channel_id, "msg-1", status)
    assert _pending(db_engine) == []


def test_b2_only_pending_rows_are_returned(db_engine) -> None:
    """四类状态混存，只挑出 PENDING 那条。"""
    channel_id = _seed(db_engine)
    _make(db_engine, channel_id, "m-processing", "processing")
    _make(db_engine, channel_id, "m-pending", "pending")
    _make(db_engine, channel_id, "m-succeeded", "succeeded")
    _make(db_engine, channel_id, "m-failed", "failed")

    rows = _pending(db_engine)
    assert [r.external_interaction_id for r in rows] == ["m-pending"]


# ================================================================== C. created_at 顺序


def test_c1_order_is_created_at_ascending(db_engine) -> None:
    """按 created_at 升序：把最后插入的一行改成更早时间 → 它必须排到最前。"""
    channel_id = _seed(db_engine)
    first = _make(db_engine, channel_id, "m-1", "pending")
    second = _make(db_engine, channel_id, "m-2", "pending")
    third = _make(db_engine, channel_id, "m-3", "pending")

    with Session(db_engine) as s:            # 第三行 → 最早创建时间
        s.execute(update(InteractionModel).where(
            InteractionModel.id == third).values(created_at=_EARLY))
        s.commit()

    assert [r.id for r in _pending(db_engine)] == [third, first, second]


def test_c2_same_timestamp_rows_keep_insertion_order(db_engine) -> None:
    """同秒并列时以 id 保序（SQLite CURRENT_TIMESTAMP 为秒级精度）。

    若不定序，两次轮询之间的 pickup 顺序会漂移——对重试与未来孤儿回收都是
    不可预期的行为，因此并列必须稳定。
    """
    channel_id = _seed(db_engine)
    ids = [_make(db_engine, channel_id, f"m-{i}", "pending") for i in range(5)]

    with Session(db_engine) as s:            # 强制全部并列
        s.execute(update(InteractionModel).where(
            InteractionModel.id.in_(ids)).values(created_at=_EARLY))
        s.commit()

    assert [r.id for r in _pending(db_engine)] == ids


# ================================================================== D. limit


def test_d1_limit_caps_the_batch(db_engine) -> None:
    channel_id = _seed(db_engine)
    ids = [_make(db_engine, channel_id, f"m-{i}", "pending") for i in range(5)]

    assert [r.id for r in _pending(db_engine, limit=2)] == ids[:2]
    assert len(_pending(db_engine, limit=3)) == 3


def test_d2_default_limit_is_bounded(db_engine) -> None:
    """默认上限是有界的——pickup 不是「全表扫描」读面。"""
    channel_id = _seed(db_engine)
    for i in range(15):
        _make(db_engine, channel_id, f"m-{i}", "pending")
    assert len(_pending(db_engine)) == 10


# ================================================================== E. 空队列


def test_e1_empty_queue_returns_empty_list(db_engine) -> None:
    _seed(db_engine)
    assert _pending(db_engine) == []


# ================================================================== F. 绝不改变状态


def test_f1_find_pending_never_claims(db_engine) -> None:
    """纯读证明：pickup 之后再 claim 仍应成功——若 pickup 偷偷 claim 过会返回 False。"""
    channel_id = _seed(db_engine)
    ids = [_make(db_engine, channel_id, f"m-{i}", "pending") for i in range(3)]

    first_read = _pending(db_engine)
    second_read = _pending(db_engine)
    assert [r.id for r in first_read] == [r.id for r in second_read] == ids
    assert {r.processing_status for r in second_read} == {"pending"}

    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.claim_processing(ids[0]) is True
        uow.commit()
    assert [r.id for r in _pending(db_engine)] == ids[1:]   # 已 claim 的不再被 pickup


# ================================================================== G. 形状与接入守卫


def test_g1_pickup_uses_plain_sql_without_row_locks() -> None:
    """普通 SQL：不用行锁 API（with_for_update），也不拼裸 SQL（text()）。

    只查**代码**形状，不做朴素子串匹配——文档字符串里出现 "FOR UPDATE" 字样
    （用于说明「为什么不用」）是允许的。
    """
    src = (BACKEND / "app" / "persistence" / "repositories"
           / "interaction_record.py").read_text(encoding="utf-8")
    assert "with_for_update" not in src       # SQLAlchemy 行锁 API
    assert "text(" not in src                 # 不拼裸 SQL


def test_g2_pickup_adds_no_schema() -> None:
    """pickup 不引入任何新列（既有的 processing_status / created_at 已够用）。

    12.9.4.12 修订：`processing_started_at` 成为合法列（attempt fence 的
    claim 时间戳）——它由 claim/reclaim 显式写入，不是 pickup/lease 设施；
    claimed_by / lease_token / locked_at 仍禁止。
    """
    cols = {c.name for c in InteractionModel.__table__.columns}
    assert {"processing_status", "created_at"} <= cols
    assert not cols & {"claimed_by", "lease_token", "locked_at"}


def test_g3_pickup_wiring_is_narrowed_to_the_driver() -> None:
    """12.9.4.16：pickup 的生产引用面 = Port + 实现 + Core phase API + Driver。

    12.9.4.4 时它是「尚未接线」的 primitive；12.9.4.16 起**只**被 Driver 消费。
    引用面仍被严格锁窄：不允许散落到 api / channel_inbound / 其他 application
    模块——pickup 的触发者必须唯一，否则就会出现第二个推动 Core 的地方。
    """
    allowed = {
        BACKEND / "app" / "repositories" / "interaction_record.py",
        BACKEND / "app" / "persistence" / "repositories" / "interaction_record.py",
        BACKEND / "app" / "application" / "interaction_recording.py",
        BACKEND / "app" / "application" / "driver.py",
    }
    offenders = [
        str(p) for p in (BACKEND / "app").rglob("*.py")
        if p not in allowed
        and "find_pending" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"pickup 被越权引用：{offenders}"
