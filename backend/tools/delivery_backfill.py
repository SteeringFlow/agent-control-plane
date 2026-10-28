"""P2 Delivery Recovery —— 历史数据**一次性回填**（运维工具，tenant-scoped）。

用途：把 P2 之前遗留的「投递事实无法确定」的行**保守地**归入 `unknown`
⇒ 它们**永不**被自动重投（避免把可能已送达的消息再发一次）。

回填规则（两条，均只动 `delivery_protocol=0` 的历史行）：

    processing='succeeded' AND delivery_status='pending'
        → delivery_status='unknown'        # 投递事实未知（phase A 之后崩溃）
    processing='succeeded' AND delivery_status='failed' AND retryable=TRUE
        → delivery_status='unknown'        # ACK 超时与「明确未送达」同形 ⇒ 不可区分

**不动**：`delivery_status='failed' AND retryable=FALSE`（两种来源都可证明未送达）、
`delivery_status='delivered'`、`processing_status='processing'`（由既有 stale
processing reclaim 接管）。

纪律：
- **tenant-scoped**：必须显式给 `--tenant-id`；所有 WHERE 带 tenant_id；
- **条件化 + 幂等**：条件含 `delivery_protocol=0` ⇒ 重跑无副作用；
- **可审计**：打印回填前后的行数与样例；**绝不**写 Evidence/activities
  （Evidence 是应用层事实面，运维脚本直写属于伪造事实）；
- **默认 dry-run**：不加 `--apply` 只统计不修改；
- 本工具**不**执行任何外部投递。

用法（先 dry-run，再 apply）：

    python -m tools.delivery_backfill --tenant-id 1 --database-url sqlite:///./data/app.db
    python -m tools.delivery_backfill --tenant-id 1 --database-url ... --apply
"""

import argparse
import logging
import sys

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.persistence.models.interaction import InteractionModel

LOGGER = logging.getLogger("tools.delivery_backfill")

LEGACY = 0
UNKNOWN = "unknown"


def _count(session: Session, tenant_id: int, status: str, retryable: bool | None) -> int:
    stmt = select(func.count()).select_from(InteractionModel).where(
        InteractionModel.tenant_id == tenant_id,
        InteractionModel.delivery_protocol == LEGACY,
        InteractionModel.processing_status == "succeeded",
        InteractionModel.delivery_status == status,
    )
    if retryable is not None:
        stmt = stmt.where(InteractionModel.retryable.is_(retryable))
    return int(session.execute(stmt).scalar_one())


def _backfill(session: Session, tenant_id: int, status: str,
              retryable: bool | None) -> int:
    stmt = (
        select(InteractionModel.id)
        .where(
            InteractionModel.tenant_id == tenant_id,
            InteractionModel.delivery_protocol == LEGACY,
            InteractionModel.processing_status == "succeeded",
            InteractionModel.delivery_status == status,
        )
    )
    if retryable is not None:
        stmt = stmt.where(InteractionModel.retryable.is_(retryable))
    ids = list(session.execute(stmt).scalars().all())
    if not ids:
        return 0
    for row_id in ids:
        session.execute(
            InteractionModel.__table__.update()
            .where(InteractionModel.id == row_id)
            .values(delivery_status=UNKNOWN)
        )
    session.commit()
    return len(ids)


def verification_counts(session: Session, tenant_id: int) -> dict:
    """回填后的**核验计数**（runbook 的核验查询；两个候选**分别**统计）。

    ⚠️ 不可用「retryable=TRUE 的单个过滤条件」代表所有候选已清零——历史
    `pending`（投递事实未知）与历史 `failed & retryable`（ACK 超时同形）必须
    **分别**检查。
    """
    return {
        "legacy_pending": _count(session, tenant_id, "pending", None),
        "legacy_failed_retryable": _count(session, tenant_id, "failed", True),
        "legacy_failed_not_retryable": _count(
            session, tenant_id, "failed", False),
        "legacy_delivered": _count(session, tenant_id, "delivered", None),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P2 Delivery 历史回填（tenant-scoped）")
    parser.add_argument("--tenant-id", type=int, required=True,
                        help="目标租户（必填；本工具绝不跨租户操作）")
    parser.add_argument("--database-url", required=True, help="数据库 URL")
    parser.add_argument("--apply", action="store_true",
                        help="真正执行回填（缺省 = dry-run，只统计）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    engine = create_engine(args.database_url)
    with Session(engine) as session:
        before_pending = _count(session, args.tenant_id, "pending", None)
        before_failed = _count(session, args.tenant_id, "failed", True)
        LOGGER.info(
            "tenant_id=%s 回填前：历史 pending=%s、历史 failed&retryable=%s",
            args.tenant_id, before_pending, before_failed)
        if not args.apply:
            LOGGER.info("dry-run（未修改任何行）；确认后加 --apply 执行")
            return 0
        moved_pending = _backfill(session, args.tenant_id, "pending", None)
        moved_failed = _backfill(session, args.tenant_id, "failed", True)
        after_pending = _count(session, args.tenant_id, "pending", None)
        after_failed = _count(session, args.tenant_id, "failed", True)
        LOGGER.info("核验计数：%s", verification_counts(session, args.tenant_id))
        LOGGER.info(
            "tenant_id=%s 回填后：历史 pending=%s、历史 failed&retryable=%s"
            "（本次迁移 pending=%s、failed=%s）",
            args.tenant_id, after_pending, after_failed,
            moved_pending, moved_failed)
        if after_pending or after_failed:
            LOGGER.warning(
                "tenant_id=%s 仍存在历史候选行 ⇒ 请复核（不得启用恢复驱动）",
                args.tenant_id)
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
