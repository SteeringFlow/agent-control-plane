"""SQLAlchemy IdentityRepository 实现（Adapter，实现 app/repositories 端口）。

Session 由调用方注入；本层不 commit（事务边界属 5.5 Unit of Work）。
Cloud-0.1.1-B：可选 `tenant_id` → 显式租户过滤（第一层防线）。
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.identity import Identity
from app.persistence.mappers.identity import apply_to_orm, to_domain, to_orm
from app.persistence.models.identity import IdentityModel
from app.repositories.identity import IdentityRepository


class SqlAlchemyIdentityRepository:
    """实现 IdentityRepository 协议。"""

    def __init__(self, session: Session, tenant_id: int | None = None) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def get_by_id(self, identity_id: int) -> Identity | None:
        if self._tenant_id is None:
            row = self._session.get(IdentityModel, identity_id)
        else:
            row = self._session.execute(
                select(IdentityModel).where(
                    IdentityModel.id == identity_id,
                    IdentityModel.tenant_id == self._tenant_id,
                )
            ).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    def create(self, identity: Identity) -> Identity:
        row = to_orm(identity)
        if self._tenant_id is not None:
            row.tenant_id = self._tenant_id
        self._session.add(row)
        self._session.flush()
        return to_domain(row)

    def update(self, identity: Identity) -> Identity:
        if self._tenant_id is None:
            row = self._session.get(IdentityModel, identity.id)
        else:
            row = self._session.execute(
                select(IdentityModel).where(
                    IdentityModel.id == identity.id,
                    IdentityModel.tenant_id == self._tenant_id,
                )
            ).scalar_one_or_none()
        # Application 用例在同一 UoW 内先 get_by_id 确认存在才走到这里；
        # 防御性兜底，避免把不存在的更新静默吞掉。
        if row is None:
            raise ValueError(f"更新目标不存在：Identity id={identity.id}")
        apply_to_orm(identity, row)
        self._session.flush()
        return to_domain(row)

    def find_or_create(self, identity: Identity) -> Identity:
        """幂等确保存在：命中复用 / 未命中创建；并发冲突由 SAVEPOINT 消化后回读。

        SAVEPOINT（而非整体 rollback）是硬要求：调用方（Bootstrap 事务）在同一事务里
        还持有 Arm 的 CAS 消费，整体回滚会连带把「已消费」退回去 ⇒ 破坏 one-shot。
        """
        existing = self.find_by_channel_and_external_id(
            identity.channel_id, identity.external_id)
        if existing is not None:
            return existing
        try:
            with self._session.begin_nested():
                return self.create(identity)
        except IntegrityError:
            # SAVEPOINT 已回滚：外层事务（含 Arm 消费）完好。
            found = self.find_by_channel_and_external_id(
                identity.channel_id, identity.external_id)
            if found is not None:
                return found
            raise

    def find_by_channel_and_external_id(
        self, channel_id: int, external_id: str
    ) -> Identity | None:
        stmt = select(IdentityModel).where(
            IdentityModel.channel_id == channel_id,
            IdentityModel.external_id == external_id,
        )
        if self._tenant_id is not None:
            stmt = stmt.where(IdentityModel.tenant_id == self._tenant_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    def list_identities(self) -> list[Identity]:
        """12.4：管理面 List 用例——返回全部 Identity（按 id 升序）。

        MVP 不做分页/过滤（与 11.3 list_agents / 11.8 list_channels 同一裁决）。
        Cloud-0.1.1-B：租户作用域下只返回本租户行。
        """
        stmt = select(IdentityModel)
        if self._tenant_id is not None:
            stmt = stmt.where(IdentityModel.tenant_id == self._tenant_id)
        rows = self._session.execute(stmt.order_by(IdentityModel.id)).scalars().all()
        return [to_domain(row) for row in rows]
