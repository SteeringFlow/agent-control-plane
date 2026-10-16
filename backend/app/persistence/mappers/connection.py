"""ConnectionMapper：ConnectionModel <-> Connection。

唯一允许"翻译 ORM 行与领域对象"的地方。
"""

from app.domain.connection import Connection
from app.persistence.models.connection import ConnectionModel


def to_domain(row: ConnectionModel) -> Connection:
    return Connection(
        id=row.id,
        channel_id=row.channel_id,
        agent_id=row.agent_id,
        name=row.name,
        status=row.status,
        config=row.config,
        credential_ref=row.credential_ref,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def to_orm(entity: Connection) -> ConnectionModel:
    # id / created_at / updated_at 由数据库生成；写路径不携带
    return ConnectionModel(
        channel_id=entity.channel_id,
        agent_id=entity.agent_id,
        name=entity.name,
        status=entity.status,
        config=entity.config,
        credential_ref=entity.credential_ref,
    )


def apply_to_orm(entity: Connection, row: ConnectionModel) -> None:
    """把**可变字段**覆写到已加载的 ORM 行（update 路径）。

    绑定（channel_id / agent_id）是 immutable binding——即使 entity 携带了
    不同的绑定值也绝不覆写（改绑定 = 删除旧 Path + 建新 Path，属删除再创建，
    不是 update）。id / created_at / updated_at 保持不变。
    """
    row.name = entity.name
    row.status = entity.status
    row.config = entity.config
    row.credential_ref = entity.credential_ref
