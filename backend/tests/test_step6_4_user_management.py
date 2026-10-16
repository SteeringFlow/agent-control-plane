"""Step 6.4 —— User Management Application Slice 验收。

本步专门验证一个判断：**User 的唯一性到底由什么定义？**

结论（不要机械复制 Agent/Channel）：
- User 是 Control Plane 内部主体；「他是谁」由未来的 Identity（channel +
  external_id，DB 层 UNIQUE(channel_id, external_id)）承载；
- users.name 只是显示名，两个真人可以同名 → 不是唯一键；
- 因此本 Slice 无 DuplicateUserError、无 name 去重守卫、无 name 查找 Port 方法，
  也不为对称性强行给 users.name 加唯一约束。

覆盖：Create / Get / Update + 输入契约校验 + 错误映射 + UoW 事务边界。
仍然不做：Identity / Connection / Session / Interaction / Routing / Runtime /
Channel Adapter / Agent Adapter / API / Frontend / Authentication / RBAC。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.application import (
    CreateUser,
    InvalidUserInputError,
    UpdateUser,
    UserApplicationService,
    UserNotFoundError,
)
from app.domain.user import User
from app.persistence.models.user import UserModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _service(db_engine) -> UserApplicationService:
    return UserApplicationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


def _count(db_engine) -> int:
    with Session(db_engine) as session:
        return session.execute(select(func.count()).select_from(UserModel)).scalar_one()


# ------------------------------------------------ 唯一性判断（本步核心）


def test_same_name_users_coexist_no_duplicate_guard(db_engine) -> None:
    """name 不是唯一键：同名 User 允许共存（真人可以同名）。"""
    service = _service(db_engine)
    first = service.create_user(CreateUser(name="张伟"))
    second = service.create_user(CreateUser(name="张伟"))

    assert _count(db_engine) == 2
    assert first.id != second.id
    assert service.get_user(first.id).name == "张伟"
    assert service.get_user(second.id).name == "张伟"


def test_no_duplicate_user_error_type_exists() -> None:
    """不为与 Agent/Channel 对称而预设 DuplicateUserError（源码级锁定）。"""
    py = BACKEND_DIR / "app" / "application" / "errors.py"
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert "DuplicateAgentError" in classes
    assert "DuplicateChannelError" in classes
    assert "DuplicateUserError" not in classes, \
        "User 唯一性归 Identity Slice；不要为对称性预设 DuplicateUserError"


def test_user_port_has_no_name_lookup_method() -> None:
    """UserRepository 不扩张无用例支撑的按 name 查找方法。

    12.4：+ list_users（User Management API 的 List 用例——Identity 绑定
    UI 的 User 下拉数据源，与 11.3 list_agents 同一演进先例）。
    """
    py = BACKEND_DIR / "app" / "repositories" / "user.py"
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    methods = {
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name != "__init__"
    }
    assert methods == {"get_by_id", "create", "update", "list_users"}, \
        f"User Port 方法面漂移：{methods}"
    assert not any("name" in m for m in methods), "User 唯一性不按 name 判定"


# ------------------------------------------------ AST：users.py 依赖边界


def test_users_service_imports_only_ports_and_domain() -> None:
    """users.py 只 import domain + repositories 端口 + 自身，绝不 import persistence/sqlalchemy。"""
    py = BACKEND_DIR / "app" / "application" / "users.py"
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))

    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)

    allowed_prefixes = (
        "app.domain", "app.repositories", "app.application",
        "collections", "dataclasses", "typing",
    )
    leaked = [
        r for r in roots
        if not any(r == p or r.startswith(p + ".") for p in allowed_prefixes)
    ]
    assert not leaked, f"users.py 越层导入：{leaked}"
    assert any(r == "app.repositories" or r.startswith("app.repositories.")
               for r in roots), "Service 应依赖 repositories（UoW/仓储）端口"

    forbidden_names = {"SqlAlchemyUnitOfWork", "Session", "sessionmaker",
                       "create_engine", "Engine", "SessionLocal"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            pytest.fail(f"users.py 出现 SQLAlchemy 概念：{node.id}")


# ------------------------------------------------ 输入契约 / 基础业务约束


@pytest.mark.parametrize(
    "bad_name", ["", "   "], ids=["empty", "blank"]
)
def test_create_rejects_invalid_name(db_engine, bad_name) -> None:
    service = _service(db_engine)
    with pytest.raises(InvalidUserInputError):
        service.create_user(CreateUser(name=bad_name))
    assert _count(db_engine) == 0


def test_update_rejects_invalid_name(db_engine) -> None:
    service = _service(db_engine)
    created = service.create_user(CreateUser(name="zhang-san"))
    with pytest.raises(InvalidUserInputError):
        service.update_user(created.id, UpdateUser(name=""))
    assert service.get_user(created.id).name == "zhang-san"


# ------------------------------------------------ Create / Get


def test_create_user_round_trip(db_engine) -> None:
    service = _service(db_engine)
    created = service.create_user(CreateUser(name="zhang-san"))
    assert created.id is not None
    assert created.status == "active"

    fetched = service.get_user(created.id)
    assert fetched == created
    assert fetched == User(id=created.id, name="zhang-san", status="active")
    assert _count(db_engine) == 1


def test_get_user_existing_and_missing(db_engine) -> None:
    service = _service(db_engine)
    created = service.create_user(CreateUser(name="exists"))
    assert service.get_user(created.id) == created
    assert service.get_user(99999) is None


# ------------------------------------------------ Update


def test_update_user_partial_fields_persist(db_engine) -> None:
    service = _service(db_engine)
    created = service.create_user(CreateUser(name="zhang-san"))

    updated = service.update_user(created.id, UpdateUser(name="zhang-san-v2"))
    assert updated.id == created.id
    assert updated.name == "zhang-san-v2"
    assert updated.status == "active"

    # 跨 UoW 重读：改动真实落库
    assert service.get_user(created.id) == updated


def test_update_user_status(db_engine) -> None:
    """基础状态字段可管理（status 为自由字符串，允许 disabled 等值）。"""
    service = _service(db_engine)
    created = service.create_user(CreateUser(name="zhang-san"))
    assert created.status == "active"

    disabled = service.update_user(created.id, UpdateUser(status="disabled"))
    assert disabled.status == "disabled"
    assert service.get_user(created.id).status == "disabled"


def test_update_missing_user_raises_not_found(db_engine) -> None:
    service = _service(db_engine)
    with pytest.raises(UserNotFoundError):
        service.update_user(99999, UpdateUser(name="ghost"))
    assert _count(db_engine) == 0, "update 不存在目标时不应建新行"


# ------------------------------------------------ 事务边界


def test_transaction_boundary_commit_is_effective(db_engine) -> None:
    """每个用例自持一个 UoW：create/update 提交后立即可见。"""
    service = _service(db_engine)
    created = service.create_user(CreateUser(name="tx-ok"))
    assert _count(db_engine) == 1
    service.update_user(created.id, UpdateUser(status="disabled"))
    assert _count(db_engine) == 1
    assert service.get_user(created.id).status == "disabled"


def test_transaction_boundary_rollback_on_exception(db_engine) -> None:
    """用例中途异常 → UoW 自动回滚 → 无残留。"""
    from app.persistence.repositories.user import SqlAlchemyUserRepository

    class _FlushThenRaise(SqlAlchemyUserRepository):
        def create(self, user: User) -> User:
            super().create(user)  # 真实 flush
            raise RuntimeError("boom")

    class _FailingUoW(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            self.users = _FlushThenRaise(self.session)
            return self

    service = UserApplicationService(
        uow_factory=lambda: _FailingUoW(session_factory=_factory(db_engine))
    )
    with pytest.raises(RuntimeError):
        service.create_user(CreateUser(name="boom"))
    assert _count(db_engine) == 0
