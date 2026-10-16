"""BootstrapArmMapper：BootstrapArmModel <-> BootstrapArm。

唯一允许"翻译 ORM 行与领域对象"的地方。

写路径**不携带** `expires_at` / `created_at` / `updated_at`（全部由 DB 生成）；
`consumed_at` 亦由消费 SQL 的 `func.now()` 写入。读路径把它们作为只读回显带出。
"""

from app.domain.bootstrap_arm import BootstrapArm
from app.persistence.models.bootstrap_arm import BootstrapArmModel


def to_domain(row: BootstrapArmModel) -> BootstrapArm:
    return BootstrapArm(
        id=row.id,
        channel_id=row.channel_id,
        connection_id=row.connection_id,
        initiator_user_id=row.initiator_user_id,
        status=row.status,
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        consumed_at=row.consumed_at,
        consumed_by_interaction_id=row.consumed_by_interaction_id,
        consumed_external_identity_id=row.consumed_external_identity_id,
        consumed_external_conversation_id=row.consumed_external_conversation_id,
    )


def to_orm(entity: BootstrapArm) -> BootstrapArmModel:
    """领域对象 → ORM 行（**不携带** DB 生成的时间戳；id 由数据库自增）。"""
    return BootstrapArmModel(
        channel_id=entity.channel_id,
        connection_id=entity.connection_id,
        initiator_user_id=entity.initiator_user_id,
        status=entity.status,
    )
