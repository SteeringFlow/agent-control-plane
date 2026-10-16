"""SQLAlchemy ConnectionConversationRepository 实现（Adapter，实现 app/repositories 端口）。

Session 由调用方注入；本层不 commit（事务边界属 Unit of Work）。
Cloud：可选 `tenant_id` → 显式租户过滤（第一层防线；第二层是 PostgreSQL RLS）。

tenant 归属由**本表自有列** stamp（`row.tenant_id = self._tenant_id`），
绝不通过 `Connection → Tenant` 推导——与 Identity 同一处理。
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.connection_conversation import ConnectionConversation
from app.persistence.mappers.connection_conversation import to_domain, to_orm
from app.persistence.models.connection_conversation import ConnectionConversationModel
from app.repositories.connection_conversation import ConnectionConversationRepository


class SqlAlchemyConnectionConversationRepository:
    """实现 ConnectionConversationRepository 协议。"""

    def __init__(self, session: Session, tenant_id: int | None = None) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def create(self, record: ConnectionConversation) -> ConnectionConversation:
        row = to_orm(record)
        if self._tenant_id is not None:
            row.tenant_id = self._tenant_id
        self._session.add(row)
        self._session.flush()
        return to_domain(row)

    def get_by_id(
        self, connection_conversation_id: int
    ) -> ConnectionConversation | None:
        if self._tenant_id is None:
            row = self._session.get(ConnectionConversationModel, connection_conversation_id)
        else:
            row = self._session.execute(
                select(ConnectionConversationModel).where(
                    ConnectionConversationModel.id == connection_conversation_id,
                    ConnectionConversationModel.tenant_id == self._tenant_id,
                )
            ).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    def find_by_binding(
        self,
        channel_id: int,
        external_conversation_id: str,
        connection_id: int,
    ) -> ConnectionConversation | None:
        stmt = select(ConnectionConversationModel).where(
            ConnectionConversationModel.channel_id == channel_id,
            ConnectionConversationModel.external_conversation_id == external_conversation_id,
            ConnectionConversationModel.connection_id == connection_id,
        )
        if self._tenant_id is not None:
            stmt = stmt.where(ConnectionConversationModel.tenant_id == self._tenant_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    def find_granted_connection_ids(
        self,
        channel_id: int,
        external_conversation_id: str,
    ) -> frozenset[int]:
        stmt = select(ConnectionConversationModel.connection_id).where(
            ConnectionConversationModel.channel_id == channel_id,
            ConnectionConversationModel.external_conversation_id == external_conversation_id,
        )
        if self._tenant_id is not None:
            stmt = stmt.where(ConnectionConversationModel.tenant_id == self._tenant_id)
        # distinct 是防御性的：四元组 UNIQUE 已保证不重复，但它让「集合语义」
        # 在业务面上不依赖约束细节。
        return frozenset(self._session.execute(stmt.distinct()).scalars().all())

    def list_by_connection(
        self,
        connection_id: int,
    ) -> list[ConnectionConversation]:
        stmt = (
            select(ConnectionConversationModel)
            .where(ConnectionConversationModel.connection_id == connection_id)
            .order_by(ConnectionConversationModel.id)
        )
        if self._tenant_id is not None:
            stmt = stmt.where(ConnectionConversationModel.tenant_id == self._tenant_id)
        rows = self._session.execute(stmt).scalars().all()
        return [to_domain(row) for row in rows]

    def find_or_create(
        self, record: ConnectionConversation
    ) -> ConnectionConversation:
        """幂等确保存在：命中复用 / 未命中创建；并发冲突由 SAVEPOINT 消化后回读。

        SAVEPOINT（而非整体 rollback）是硬要求：Bootstrap 事务内还持有 Arm 的 CAS
        消费，整体回滚会连带退回「已消费」⇒ 破坏 one-shot。
        """
        existing = self.find_by_binding(
            record.channel_id, record.external_conversation_id,
            record.connection_id)
        if existing is not None:
            return existing
        try:
            with self._session.begin_nested():
                return self.create(record)
        except IntegrityError:
            found = self.find_by_binding(
                record.channel_id, record.external_conversation_id,
                record.connection_id)
            if found is not None:
                return found
            raise

    def delete(self, connection_conversation_id: int) -> bool:
        if self._tenant_id is None:
            row = self._session.get(
                ConnectionConversationModel, connection_conversation_id)
        else:
            row = self._session.execute(
                select(ConnectionConversationModel).where(
                    ConnectionConversationModel.id == connection_conversation_id,
                    ConnectionConversationModel.tenant_id == self._tenant_id,
                )
            ).scalar_one_or_none()
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True
