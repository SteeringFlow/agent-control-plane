"""SessionMapper：SessionModel <-> Session。

唯一允许"翻译 ORM 行与领域对象"的地方。

读路径（9.3）：`to_domain`；写路径（9.7）：`to_orm`。不做 update（会话生命周期
管理属后续 Slice）。
"""

from app.domain.session import Session
from app.persistence.models.session import SessionModel


def to_domain(row: SessionModel) -> Session:
    return Session(
        id=row.id,
        identity_id=row.identity_id,
        channel_id=row.channel_id,
        connection_id=row.connection_id,
        agent_id=row.agent_id,
        external_session_id=row.external_session_id,
        external_conversation_id=row.external_conversation_id,
        status=row.status,
        metadata=row.extra_metadata,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def to_orm(entity: Session) -> SessionModel:
    # id / created_at / updated_at 由数据库生成；写路径不携带
    return SessionModel(
        identity_id=entity.identity_id,
        channel_id=entity.channel_id,
        connection_id=entity.connection_id,
        agent_id=entity.agent_id,
        external_session_id=entity.external_session_id,
        external_conversation_id=entity.external_conversation_id,
        status=entity.status,
        extra_metadata=entity.metadata,
    )
