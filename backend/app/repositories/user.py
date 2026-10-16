"""UserRepository 端口（Port）。

只描述业务需要的查询/写入能力，不含实现，不 import SQLAlchemy / persistence。
实现见 app/persistence/repositories/user.py（SqlAlchemyUserRepository）。

方法按 User 管理语义命名（6.4 起加入 update），不做 GenericRepository[T] 万能 CRUD。

注意：这里**没有** find_by_name / find_by_email 之类的查找方法——User 的唯一性
由未来的 Identity（channel + external_id）承载，name 不是唯一键；
不为与 Agent/Channel 对称就扩张无用例支撑的端口方法。
"""

from typing import Protocol, runtime_checkable

from app.domain.user import User


@runtime_checkable
class UserRepository(Protocol):
    """用户仓储：按业务语义命名的方法，不是 Generic CRUD。"""

    def get_by_id(self, user_id: int) -> User | None:
        """按主键取用户；不存在返回 None。"""
        ...

    def create(self, user: User) -> User:
        """写入用户并返回带 id 的新实例（事务边界由 5.5 UoW 管理）。"""
        ...

    def update(self, user: User) -> User:
        """把 User 上的字段覆写到已存在的行（id 必须存在）；返回更新后的实例。"""
        ...

    def list_users(self) -> list[User]:
        """12.4：List 用例——返回全部 User（管理面陈列 / Identity 绑定下拉用）。"""
        ...
