"""SQLAlchemy UserRepository 实现（Adapter，实现 app/repositories 端口）。

Session 由调用方注入（来自持久化基础设施，不是 Application 自己创建）；
本层不 commit——事务边界属于 5.5 Unit of Work。

Cloud-0.1.1-B 租户隔离（第一层防线）：构造接收可选 `tenant_id`（由 UoW 注入）。
- `tenant_id` 非空 → 每个查询带 `tenant_id == self._tenant_id` 显式过滤，
  写入时显式 `row.tenant_id = self._tenant_id`（Domain/Mapper 不感知 tenant）；
- `tenant_id is None` → 本地/测试单租户 legacy 路径（行为 == 隔离前）。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.user import User
from app.persistence.mappers.user import apply_to_orm, to_domain, to_orm
from app.persistence.models.user import UserModel
from app.repositories.user import UserRepository


class SqlAlchemyUserRepository:
    """实现 UserRepository 协议。"""

    def __init__(self, session: Session, tenant_id: int | None = None) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def get_by_id(self, user_id: int) -> User | None:
        if self._tenant_id is None:
            row = self._session.get(UserModel, user_id)
        else:
            row = self._session.execute(
                select(UserModel).where(
                    UserModel.id == user_id,
                    UserModel.tenant_id == self._tenant_id,
                )
            ).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    def create(self, user: User) -> User:
        row = to_orm(user)
        if self._tenant_id is not None:
            row.tenant_id = self._tenant_id
        self._session.add(row)
        self._session.flush()  # 拿回自增 id（写成功后即可返回带 id 的领域对象）
        return to_domain(row)

    def update(self, user: User) -> User:
        if self._tenant_id is None:
            row = self._session.get(UserModel, user.id)
        else:
            row = self._session.execute(
                select(UserModel).where(
                    UserModel.id == user.id,
                    UserModel.tenant_id == self._tenant_id,
                )
            ).scalar_one_or_none()
        # Application 用例在同一 UoW 内先 get_by_id 确认存在才走到这里；
        # 防御性兜底，避免把不存在的更新静默吞掉。
        if row is None:
            raise ValueError(f"更新目标不存在：User id={user.id}")
        apply_to_orm(user, row)
        self._session.flush()
        return to_domain(row)

    def list_users(self) -> list[User]:
        """12.4：管理面 List 用例——返回全部 User（按 id 升序）。

        MVP 不做分页/过滤（与 11.3 list_agents / 11.8 list_channels 同一裁决）。
        Cloud-0.1.1-B：租户作用域下只返回本租户行。
        """
        stmt = select(UserModel)
        if self._tenant_id is not None:
            stmt = stmt.where(UserModel.tenant_id == self._tenant_id)
        rows = self._session.execute(stmt.order_by(UserModel.id)).scalars().all()
        return [to_domain(row) for row in rows]
