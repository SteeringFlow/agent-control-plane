"""SQLAlchemy ActivityRecordRepository 实现（Adapter，Step 10.4.2）。

Append-only：只有 add + flush，没有 update / delete——
历史事实不允许被改写（与 Port / schema 互为表里）。

13.1 起：增加 `list_by_interaction` 只读时间线（单一 select，零写入）——
读路径同样不改写历史。

Cloud-0.1.1-B：可选 `tenant_id` → 显式租户过滤（第一层防线）。
"""

from sqlalchemy import select

from app.domain.activity_record import ActivityRecord
from app.persistence.mappers.activity_record import to_domain, to_orm
from app.persistence.models.activity import ActivityModel
from app.repositories.activity_record import ActivityRecordRepository


class SqlAlchemyActivityRecordRepository:
    """实现 ActivityRecordRepository 协议。"""

    def __init__(self, session, tenant_id: int | None = None) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def append(self, record: ActivityRecord) -> ActivityRecord:
        row = to_orm(record)
        if self._tenant_id is not None:
            row.tenant_id = self._tenant_id
        self._session.add(row)
        self._session.flush()
        return to_domain(row)

    def list_by_interaction(self, interaction_id: int) -> list[ActivityRecord]:
        """某次 Interaction 的 Evidence 时间线（13.1，纯读）。

        发生序：`created_at` 升序、同秒以 `id` 升序（append 写入序 tiebreak）。
        原样返回 Evidence，不归并、不过滤、不改写。
        Cloud-0.1.1-B：租户作用域下只返回本租户证据。
        """
        stmt = select(ActivityModel).where(
            ActivityModel.interaction_id == interaction_id)
        if self._tenant_id is not None:
            stmt = stmt.where(ActivityModel.tenant_id == self._tenant_id)
        rows = self._session.execute(
            stmt.order_by(ActivityModel.created_at, ActivityModel.id)
        ).scalars().all()
        return [to_domain(row) for row in rows]


# 协议一致性自检（Port 形状；不做运行时开销）
_ = ActivityRecordRepository
