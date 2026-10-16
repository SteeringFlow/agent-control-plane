"""UserMapper：UserModel <-> User。

唯一允许"翻译 ORM 行与领域对象"的地方。
本步只需两方向：读路径 to_domain、写路径 to_orm。
"""

from app.domain.user import User
from app.persistence.models.user import UserModel


def to_domain(row: UserModel) -> User:
    return User(id=row.id, name=row.name, status=row.status)


def to_orm(entity: User) -> UserModel:
    # id 由数据库自增生成；写路径不携带领域 id
    return UserModel(name=entity.name, status=entity.status)


def apply_to_orm(entity: User, row: UserModel) -> None:
    """把 entity 的业务字段覆写到已加载的 ORM 行（update 路径）。

    id 与 created_at 保持不变，只覆写可变字段。
    """
    row.name = entity.name
    row.status = entity.status
