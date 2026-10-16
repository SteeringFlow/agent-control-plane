"""User Management Application Service（Step 6.4）。

把已冻结的 User Domain / Repository / Mapper / ORM 接到 Application 层。

User 唯一性结论（本 Slice 的重要判断，不是机械复制 Agent/Channel）：
- Control Plane 的 User 是内部主体（principal）；「他是谁」由未来的
  Identity（channel + external_id，DB 层 UNIQUE(channel_id, external_id)）承载；
- users.name 只是显示名，两个真人可以同名（如多个"张伟"）——它不是唯一键；
- 因此本 Slice **不设 DuplicateUserError、不做 name 去重守卫、不加唯一约束**；
  UserRepository 也不增加任何按 name 查找的方法（不做无用例支撑的 Port 扩张）。
  唯一性规则随 Identity Slice 一起定义。

用例：Create / Get / Update + 输入契约校验 + Application-level error mapping。
事务边界归 UoW（每个用例自持一个 UoW，显式 commit / 异常自动 rollback）。

边界（AST 守护）：不 import SQLAlchemy / FastAPI / api / schemas / adapters /
app.persistence；只依赖 app.domain、app.repositories（端口）、app.application。
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from app.application.errors import InvalidUserInputError, UserNotFoundError
from app.domain.user import User
from app.repositories.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class CreateUser:
    """创建 User 的输入契约（应用层校验点，非 Pydantic/API schema）。

    name 是显示名，不是唯一键——同名 User 允许共存。
    状态由领域对象默认 active。
    """

    name: str


@dataclass(frozen=True)
class UpdateUser:
    """更新 User 的输入契约（12.3 冻结）。

    None / omitted = 不修改：PATCH {} → noop，与 11.2 Agent / 11.8 Channel /
    11.11 Connection 的管理面 PATCH 语义一致；不支持把字段显式清空为 None
    （name / status 为非空列）。id 不可通过本契约修改（UpdateUser 无 id 字段）。
    """

    name: str | None = None
    status: str | None = None


def _require_non_blank(value: str | None, field: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise InvalidUserInputError(f"{field} 不能为空")


class UserApplicationService:
    """User 管理用例的编排者（依赖 UoW Port，而非具体 SQLAlchemy 实现）。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    # ------------------------------------------------------------ Create

    def create_user(self, input: CreateUser) -> User:
        """创建 User：校验（输入契约）→ 写入 → commit。

        本 Slice 无去重守卫（同名允许）；非法输入抛 InvalidUserInputError。
        """
        _require_non_blank(input.name, "name")

        user = User(name=input.name)
        with self._uow_factory() as uow:
            created = uow.users.create(user)
            uow.commit()
            return created

    # ------------------------------------------------------------ Get

    def get_user(self, user_id: int) -> User | None:
        """按 id 读取 User；不存在返回 None。"""
        with self._uow_factory() as uow:
            return uow.users.get_by_id(user_id)

    # ------------------------------------------------------------ List

    def list_users(self) -> list[User]:
        """12.4：List 用例——返回全部 User（管理面陈列 / Identity 绑定下拉用）。

        MVP 无分页/过滤（与 11.3 list_agents / 11.8 list_channels 同一裁决）；
        name 不是唯一键，同名 User 全部如实陈列。
        """
        with self._uow_factory() as uow:
            return uow.users.list_users()

    # ------------------------------------------------------------ Update

    def update_user(self, user_id: int, change: UpdateUser) -> User:
        """部分更新 User（12.3 冻结：None / omitted = 不修改）。

        PATCH {}（全 None）→ noop：原值写回，行为与 Agent/Channel/Connection
        管理面 PATCH 同型。目标不存在抛 UserNotFoundError；非 None 空串校验
        失败抛 InvalidUserInputError。id 不可改。
        """
        _require_non_blank(change.name, "name")
        _require_non_blank(change.status, "status")

        with self._uow_factory() as uow:
            current = uow.users.get_by_id(user_id)
            if current is None:
                raise UserNotFoundError(user_id)
            updated = self._apply_change(current, change)
            result = uow.users.update(updated)
            uow.commit()
            return result

    @staticmethod
    def _apply_change(current: User, change: UpdateUser) -> User:
        return replace(
            current,
            name=change.name if change.name is not None else current.name,
            status=change.status if change.status is not None else current.status,
        )
