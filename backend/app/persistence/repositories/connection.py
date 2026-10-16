"""SQLAlchemy ConnectionRepository 实现（Adapter，实现 app/repositories 端口）。

Session 由调用方注入；本层不 commit（事务边界属 UoW）。
Cloud-0.1.1-B：可选 `tenant_id` → 显式租户过滤（第一层防线）。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.connection import Connection
from app.persistence.mappers.connection import apply_to_orm, to_domain, to_orm
from app.persistence.models.connection import ConnectionModel
from app.repositories.connection import ConnectionRepository


class SqlAlchemyConnectionRepository:
    """实现 ConnectionRepository 协议。"""

    def __init__(self, session: Session, tenant_id: int | None = None) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def get_by_id(self, connection_id: int) -> Connection | None:
        if self._tenant_id is None:
            row = self._session.get(ConnectionModel, connection_id)
        else:
            row = self._session.execute(
                select(ConnectionModel).where(
                    ConnectionModel.id == connection_id,
                    ConnectionModel.tenant_id == self._tenant_id,
                )
            ).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    def create(self, connection: Connection) -> Connection:
        row = to_orm(connection)
        if self._tenant_id is not None:
            row.tenant_id = self._tenant_id
        self._session.add(row)
        self._session.flush()
        return to_domain(row)

    def update(self, connection: Connection) -> Connection:
        if self._tenant_id is None:
            row = self._session.get(ConnectionModel, connection.id)
        else:
            row = self._session.execute(
                select(ConnectionModel).where(
                    ConnectionModel.id == connection.id,
                    ConnectionModel.tenant_id == self._tenant_id,
                )
            ).scalar_one_or_none()
        # Application 用例在同一 UoW 内先 get_by_id 确认存在才走到这里；
        # 防御性兜底，避免把不存在的更新静默吞掉。
        if row is None:
            raise ValueError(f"更新目标不存在：Connection id={connection.id}")
        apply_to_orm(connection, row)
        self._session.flush()
        return to_domain(row)

    def find_by_channel_and_agent(
        self, channel_id: int, agent_id: int
    ) -> Connection | None:
        stmt = select(ConnectionModel).where(
            ConnectionModel.channel_id == channel_id,
            ConnectionModel.agent_id == agent_id,
        )
        if self._tenant_id is not None:
            stmt = stmt.where(ConnectionModel.tenant_id == self._tenant_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    def find_active_by_channel(self, channel_id: int) -> list[Connection]:
        stmt = select(ConnectionModel).where(
            ConnectionModel.channel_id == channel_id,
            ConnectionModel.status == "active",
        )
        if self._tenant_id is not None:
            stmt = stmt.where(ConnectionModel.tenant_id == self._tenant_id)
        rows = self._session.execute(
            stmt.order_by(ConnectionModel.id)).scalars().all()
        return [to_domain(row) for row in rows]

    def list_connections(self) -> list[Connection]:
        """11.11：management 陈列读——全部 Connection（含 inactive），按 id 排序。

        无过滤：管理面需要看见软下线（inactive）的 Access Path；
        runtime 读路径 find_active_by_channel 保持原样，互不替代。
        Cloud-0.1.1-B：租户作用域下只返回本租户行。
        """
        stmt = select(ConnectionModel)
        if self._tenant_id is not None:
            stmt = stmt.where(ConnectionModel.tenant_id == self._tenant_id)
        rows = self._session.execute(
            stmt.order_by(ConnectionModel.id)).scalars().all()
        return [to_domain(row) for row in rows]
