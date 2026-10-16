"""ChannelMapper：ChannelModel <-> Channel。"""

from app.domain.channel import Channel
from app.persistence.models.channel import ChannelModel


def to_domain(row: ChannelModel) -> Channel:
    return Channel(
        id=row.id,
        name=row.name,
        type=row.type,
        status=row.status,
        config=row.config,
    )


def to_orm(entity: Channel) -> ChannelModel:
    return ChannelModel(
        name=entity.name,
        type=entity.type,
        status=entity.status,
        config=entity.config,
    )


def apply_to_orm(entity: Channel, row: ChannelModel) -> None:
    """把 entity 的业务字段覆写到已加载的 ORM 行（update 路径）。

    id 与 created_at 保持不变，只覆写可变字段。
    """
    row.name = entity.name
    row.type = entity.type
    row.status = entity.status
    row.config = entity.config
