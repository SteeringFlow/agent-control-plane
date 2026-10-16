"""ActivityRecordMapper：ActivityRecord <-> ActivityModel（Step 10.4.2）。

Append-only：只有 to_orm（写入）与 to_domain（读回），无 update 语义。
"""

from app.domain.activity_record import ActivityRecord
from app.persistence.models.activity import ActivityModel


def to_domain(row: ActivityModel) -> ActivityRecord:
    return ActivityRecord(
        id=row.id,
        interaction_id=row.interaction_id,
        session_id=row.session_id,
        type=row.type,
        stage=row.stage,
        status=row.status,
        message=row.message,
        metadata=row.extra_metadata,
        # 13.1 Observability：DB 生成的 created_at 只随读路径回显（时间线排序）
        created_at=row.created_at,
    )


def to_orm(entity: ActivityRecord) -> ActivityModel:
    # id / created_at 由数据库生成；append-only 无更新路径
    return ActivityModel(
        interaction_id=entity.interaction_id,
        session_id=entity.session_id,
        type=entity.type,
        stage=entity.stage,
        status=entity.status,
        message=entity.message,
        extra_metadata=entity.metadata,
    )
