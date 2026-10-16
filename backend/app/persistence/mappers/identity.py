"""IdentityMapper：IdentityModel <-> Identity。

唯一允许"翻译 ORM 行与领域对象"的地方。

注意：ORM 侧因 SQLAlchemy 保留 metadata 而使用 extra_metadata（列名仍是冻结的
metadata），领域侧统一叫 metadata——这个差异只在本 Mapper 内消化。
"""

from app.domain.identity import Identity
from app.persistence.models.identity import IdentityModel


def to_domain(row: IdentityModel) -> Identity:
    return Identity(
        id=row.id,
        user_id=row.user_id,
        channel_id=row.channel_id,
        external_id=row.external_id,
        display_name=row.display_name,
        status=row.status,
        metadata=row.extra_metadata,
    )


def to_orm(entity: Identity) -> IdentityModel:
    # id 由数据库自增生成；写路径不携带领域 id
    return IdentityModel(
        user_id=entity.user_id,
        channel_id=entity.channel_id,
        external_id=entity.external_id,
        display_name=entity.display_name,
        status=entity.status,
        extra_metadata=entity.metadata,
    )


def apply_to_orm(entity: Identity, row: IdentityModel) -> None:
    """把 entity 的业务字段覆写到已加载的 ORM 行（update 路径）。

    id / user_id / channel_id / external_id 与 created_at 保持不变——
    绑定关系不属于 update 可改的字段（改绑定 = Identity Linking，后续 Slice）。
    """
    row.display_name = entity.display_name
    row.status = entity.status
    row.extra_metadata = entity.metadata
