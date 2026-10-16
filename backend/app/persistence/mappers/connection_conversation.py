"""ConnectionConversationMapper：ORM 行 <-> 领域实体。

唯一允许「翻译 ORM 行与领域对象」的地方。

刻意**没有** `apply_to_orm`：授权行**不可变**（改授权 = 删除 + 新建），
因此不存在 update 路径——与 Connection 绑定不可变同一思想。
"""

from app.domain.connection_conversation import ConnectionConversation
from app.persistence.models.connection_conversation import ConnectionConversationModel


def to_domain(row: ConnectionConversationModel) -> ConnectionConversation:
    return ConnectionConversation(
        id=row.id,
        channel_id=row.channel_id,
        external_conversation_id=row.external_conversation_id,
        connection_id=row.connection_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def to_orm(entity: ConnectionConversation) -> ConnectionConversationModel:
    # id 由数据库自增生成；写路径不携带领域 id 与时间戳（由 DB 生成）。
    return ConnectionConversationModel(
        channel_id=entity.channel_id,
        external_conversation_id=entity.external_conversation_id,
        connection_id=entity.connection_id,
    )
