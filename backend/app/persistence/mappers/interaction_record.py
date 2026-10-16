"""InteractionRecordMapper：InteractionRecord <-> InteractionModel（Step 10.4.2）。

唯一允许"翻译 ORM 行与领域对象"的地方（interaction 记录维度）。
"""

from app.domain.interaction_record import InteractionRecord
from app.persistence.models.interaction import InteractionModel


def to_domain(row: InteractionModel) -> InteractionRecord:
    return InteractionRecord(
        id=row.id,
        channel_id=row.channel_id,
        session_id=row.session_id,
        direction=row.direction,
        external_interaction_id=row.external_interaction_id,
        request_payload=row.request_payload,
        response_payload=row.response_payload,
        processing_status=row.processing_status,
        delivery_status=row.delivery_status,
        failure_stage=row.failure_stage,
        failure_code=row.failure_code,
        retryable=row.retryable,
        processing_started_at=row.processing_started_at,
        # 13.1 Observability：DB 生成的审计时间戳只随读路径回显
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def to_orm(entity: InteractionRecord) -> InteractionModel:
    # id / created_at / updated_at / processing_started_at 由数据库生成
    # （CAS 抢占的 func.now() / 回收的 NULL）；写路径不携带
    return InteractionModel(
        channel_id=entity.channel_id,
        session_id=entity.session_id,
        direction=entity.direction,
        external_interaction_id=entity.external_interaction_id,
        request_payload=entity.request_payload,
        response_payload=entity.response_payload,
        processing_status=entity.processing_status,
        delivery_status=entity.delivery_status,
        failure_stage=entity.failure_stage,
        failure_code=entity.failure_code,
        retryable=entity.retryable,
    )
