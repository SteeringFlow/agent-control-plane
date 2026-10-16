"""SQLAlchemy ChannelRepository 实现（Adapter，实现 app/repositories 端口）。

Session 由调用方注入；本层不 commit（事务边界属 5.5 Unit of Work）。
Cloud-0.1.1-B：可选 `tenant_id` → 显式租户过滤（第一层防线）。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.channel import Channel
from app.persistence.mappers.channel import apply_to_orm, to_domain, to_orm
from app.persistence.models.channel import ChannelModel
from app.repositories.channel import ChannelRepository


class SqlAlchemyChannelRepository:
    """实现 ChannelRepository 协议。"""

    def __init__(self, session: Session, tenant_id: int | None = None) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def get_by_id(self, channel_id: int) -> Channel | None:
        if self._tenant_id is None:
            row = self._session.get(ChannelModel, channel_id)
        else:
            row = self._session.execute(
                select(ChannelModel).where(
                    ChannelModel.id == channel_id,
                    ChannelModel.tenant_id == self._tenant_id,
                )
            ).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    def find_tenant_id_by_id(self, channel_id: int) -> int | None:
        """bootstrap 路由投影（Cloud-0.1.3.1）：按主键读渠道所属 tenant_id。

        **刻意不追加 `tenant_id == self._tenant_id` 过滤**——这是 tenant 解析之前的
        定位读（B §10/§12），用于「URL channel_id → 已认证 Channel → tenant」。
        与 `get_by_id` 的租户作用域读正交，是 Port 上唯一的 bootstrap 例外，
        故在此显式说明而非隐含。
        """
        return self._session.execute(
            select(ChannelModel.tenant_id).where(ChannelModel.id == channel_id)
        ).scalar_one_or_none()

    def create(self, channel: Channel) -> Channel:
        row = to_orm(channel)
        if self._tenant_id is not None:
            row.tenant_id = self._tenant_id
        self._session.add(row)
        self._session.flush()
        return to_domain(row)

    def update(self, channel: Channel) -> Channel:
        if self._tenant_id is None:
            row = self._session.get(ChannelModel, channel.id)
        else:
            row = self._session.execute(
                select(ChannelModel).where(
                    ChannelModel.id == channel.id,
                    ChannelModel.tenant_id == self._tenant_id,
                )
            ).scalar_one_or_none()
        # Application 用例在同一 UoW 内先 get_by_id 确认存在才走到这里；
        # 防御性兜底，避免把不存在的更新静默吞掉。
        if row is None:
            raise ValueError(f"更新目标不存在：Channel id={channel.id}")
        apply_to_orm(channel, row)
        self._session.flush()
        return to_domain(row)

    def find_by_type_and_name(self, channel_type: str, name: str) -> Channel | None:
        stmt = select(ChannelModel).where(
            ChannelModel.type == channel_type, ChannelModel.name == name
        )
        if self._tenant_id is not None:
            stmt = stmt.where(ChannelModel.tenant_id == self._tenant_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    def list_channels(self) -> list[Channel]:
        """11.8：管理面 List 用例——返回全部 Channel（按 id 升序）。

        MVP 不做分页/过滤（与 11.3 list_agents 同一裁决：无真实消费者
        证明需要前不过度建设）。Cloud-0.1.1-B：租户作用域下只返回本租户行。
        """
        stmt = select(ChannelModel)
        if self._tenant_id is not None:
            stmt = stmt.where(ChannelModel.tenant_id == self._tenant_id)
        rows = self._session.execute(stmt.order_by(ChannelModel.id)).scalars().all()
        return [to_domain(row) for row in rows]
