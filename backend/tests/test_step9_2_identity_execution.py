"""Step 9.2 —— Identity Resolution Execution（8.2 契约的第一次真实执行）。

9.2 正式允许 Runtime Application 通过 Repository Port 使用 Persistence：

    Inbound 的 (channel_id, external_identity_id)
        ↓ IdentityRepository.find_by_channel_and_external_id（6.6 已冻结）
        ↓ Identity
        ↓ UserRepository.get_by_id（6.4 已冻结；user_id 可为 None，未绑定则不查）
        ↓ User | None
        ↓ IdentityResolution

锁定项：
A  正常解析返回 IdentityResolution（Identity + User 链路正确）；
A' 找到 Identity 但 **User 未绑定**（user_id IS NULL）→ 解析**成功**且 user=None
   （Implementation Package 1 / Gate 2.6 D3 语义反转：
    User 存在与否**不再**决定 Resolution 成败）；
B  Identity 不存在 → IdentityResolutionFailedError（Fail Closed，STOP）；
C  Channel 隔离：同 external_id 跨 Channel 是独立查找空间；
D  不猜 User：external_id → Identity → User（不是 external_id → User）；
E  Repository Boundary：只依赖 Repository/UoW Port，不 import SQLAlchemy/ORM；
F  不创建 Identity/User（失败后计数不变；源码无 create/update 调用）；
G  不修改 Management Contract：IdentityApplicationService 仍只有 create/get/update；
H  不调用/不 import IdentityApplicationService；
I  不修改 IdentityRepository/UserRepository surface；
J  不创建 IdentityResolver/IdentityResolutionPolicy 等（Execution ≠ Resolver）；
K  mutation guard（越层 import / Resolver 偷渡必须报红）。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.application import IdentityResolutionExecutor, IdentityResolutionFailedError
from app.domain.channel import Channel
from app.domain.identity import Identity
from app.domain.identity_resolution import IdentityResolution
from app.domain.user import User
from app.persistence.models.identity import IdentityModel
from app.persistence.models.user import UserModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]
EXECUTOR_PY = APP_DIR / "application" / "identity_resolution.py"

FORBIDDEN_ROOTS = (
    "sqlalchemy", "fastapi", "starlette", "pydantic",
    "app.persistence", "app.adapters", "app.api", "app.schemas",
)
# 只允许依赖：stdlib + domain + repositories Port + application.errors（自身层）
def _root_allowed(root: str) -> bool:
    return (root in {"collections", "dataclasses", "typing"}
            or root.startswith("collections.")
            or root.startswith("typing.")
            or root == "app.domain" or root.startswith("app.domain.")
            or root == "app.repositories" or root.startswith("app.repositories.")
            or root == "app.application.errors")


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _executor(db_engine) -> IdentityResolutionExecutor:
    return IdentityResolutionExecutor(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


def _seed(db_engine, *, channel_name: str, user_name: str,
          external_id: str, channel_type: str = "wecom"):
    """落库一条 User + Channel + Identity（直接走 Port，绕过 Management）。"""
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        user = uow.users.create(User(name=user_name))
        channel = uow.channels.create(Channel(name=channel_name, type=channel_type))
        identity = uow.identities.create(Identity(
            user_id=user.id, channel_id=channel.id, external_id=external_id))
        uow.commit()
        return user, channel, identity


def _seed_unbound(db_engine, *, channel_name: str = "wecom-unbound",
                  external_id: str, channel_type: str = "wecom"):
    """落库一条 **未绑定 ACP User** 的 Identity（user_id=NULL）+ 其 Channel。

    Implementation Package 1（Gate 3 A-1）之后 `identities.user_id` 可空，
    这条路径**可以用真实 DB 构造**——原先只能靠 Fake UoW 模拟。
    """
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        channel = uow.channels.create(Channel(name=channel_name, type=channel_type))
        identity = uow.identities.create(Identity(
            user_id=None, channel_id=channel.id, external_id=external_id))
        uow.commit()
        return channel, identity


def _count(db_engine, model) -> int:
    with Session(db_engine) as session:
        return session.execute(select(func.count()).select_from(model)).scalar_one()


# ------------------------------------------------------------------ AST helpers


def _roots(src: str) -> set[str]:
    roots: set[str] = set()
    tree = ast.parse(src, filename=str(EXECUTOR_PY))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def _classes(src: str) -> dict[str, ast.ClassDef]:
    return {n.name: n for n in ast.walk(ast.parse(src, filename=str(EXECUTOR_PY)))
            if isinstance(n, ast.ClassDef)}


def _methods(cls: ast.ClassDef) -> set[str]:
    return {s.name for s in cls.body if isinstance(s, ast.FunctionDef)}


def _boundary_problems(src: str) -> list[str]:
    """Execution 模块边界守卫：越层依赖 / 基础设施标识符 / 写调用 / 多余类。"""
    problems: list[str] = []
    for root in sorted(_roots(src)):
        if not _root_allowed(root):
            problems.append(f"越层 import {root}")
    tree = ast.parse(src, filename=str(EXECUTOR_PY))
    forbidden_names = {"SqlAlchemyUnitOfWork", "Session", "sessionmaker",
                       "create_engine", "Engine", "IdentityModel", "UserModel",
                       "ChannelModel", "HTTPException", "APIRouter"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            problems.append(f"出现基础设施标识符 {node.id}")
    write_calls = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"create", "update", "delete"}
    }
    if write_calls:
        problems.append(f"Execution 不应写：{write_calls}")
    extra = set(_classes(src)) - {"IdentityResolutionExecutor"}
    if extra:
        problems.append(f"出现非 Execution 组件类：{sorted(extra)}")
    return problems


# ================================================================== A 正常解析


def test_a_resolve_returns_identity_resolution(db_engine) -> None:
    user, channel, identity = _seed(
        db_engine, channel_name="wecom-1", user_name="alice", external_id="wx_1")
    result = _executor(db_engine).execute(channel.id, "wx_1")

    assert isinstance(result, IdentityResolution)
    assert result.identity.id == identity.id
    assert result.user.id == user.id
    # Identity → User 链路：user 由 identity.user_id 取得
    assert result.user.id == result.identity.user_id
    assert result.identity.channel_id == channel.id
    assert result.identity.external_id == "wx_1"


# ================================================================== B Fail Closed


def test_b_unknown_identity_fails_closed(db_engine) -> None:
    _seed(db_engine, channel_name="wecom-1", user_name="alice", external_id="wx_1")
    with pytest.raises(IdentityResolutionFailedError):
        _executor(db_engine).execute(channel_id=1, external_identity_id="wx_missing")


def test_b_failure_does_not_create_identity_or_user(db_engine) -> None:
    user, channel, identity = _seed(
        db_engine, channel_name="wecom-1", user_name="alice", external_id="wx_1")
    before = (_count(db_engine, IdentityModel), _count(db_engine, UserModel))
    for attempt in ("wx_missing", "other_1"):
        with pytest.raises(IdentityResolutionFailedError):
            _executor(db_engine).execute(channel.id, attempt)
    after = (_count(db_engine, IdentityModel), _count(db_engine, UserModel))
    assert before == after == (1, 1)


def test_b_identity_without_user_resolves_with_user_none(db_engine) -> None:
    """找到 Identity 但 User **未绑定** → 解析**成功**，`user is None`。

    ★ 语义反转（Implementation Package 1 / Gate 2.6 D3）：本测试原为
    `test_b_identity_without_user_fails_closed`——断言抛错，且只能用 Fake UoW
    模拟（原文写「FK 使 DB 无法构造」，因为当时 user_id NOT NULL）。
    Gate 3 A-1 放开可空后，这条路径**可以用真实 DB 构造**：
    不是把断言改松，而是**把假测换成真测**。
    """
    channel, identity = _seed_unbound(db_engine, external_id="wx_unbound")
    result = _executor(db_engine).execute(channel.id, "wx_unbound")

    assert isinstance(result, IdentityResolution)
    assert result.identity.id == identity.id
    assert result.identity.user_id is None
    assert result.user is None                # 未绑定 = 正常结果，不是失败
    # 解析是只读的：不因「没有 User」而补建任何行
    assert _count(db_engine, IdentityModel) == 1
    assert _count(db_engine, UserModel) == 0


def test_b_identity_with_dangling_user_id_resolves_with_user_none() -> None:
    """identity.user_id 指向不存在的 User（DB 不可构造）→ 仍解析成功、user=None。

    用 Fake UoW 覆盖**悬挂引用**这一 DB 不可达情形（真实 FK + NO ACTION 会阻止
    这种行落库）。语义（2.6 D3）：User 存在与否**不再决定成败**。
    """

    class _FakeUsers:
        def get_by_id(self, user_id: int):
            return None  # user 缺失（悬挂引用）

    class _FakeUoW:
        identities = None
        users = _FakeUsers()

        def __enter__(self): return self

        def __exit__(self, *a): return False

        def commit(self): pass

        def rollback(self): pass

    class _FakeIdentities:
        def find_by_channel_and_external_id(self, channel_id, external_id):
            return Identity(user_id=99, channel_id=1, external_id="wx_1", id=5)

    uow = _FakeUoW()
    uow.identities = _FakeIdentities()
    executor = IdentityResolutionExecutor(uow_factory=lambda: uow)

    result = executor.execute(channel_id=1, external_identity_id="wx_1")
    assert result.identity.user_id == 99
    assert result.user is None                # 不再是 IDENTITY_RESOLUTION_FAILED


# ================================================================== C Channel 隔离


def test_c_channel_isolation_same_external_id(db_engine) -> None:
    """同 external_id 在不同 Channel 是两个独立查找空间。"""
    u1, c1, i1 = _seed(db_engine, channel_name="wecom-1", user_name="alice",
                       external_id="123", channel_type="wecom")
    u2, c2, i2 = _seed(db_engine, channel_name="feishu-1", user_name="bob",
                       external_id="123", channel_type="feishu")
    executor = _executor(db_engine)

    r1 = executor.execute(c1.id, "123")
    r2 = executor.execute(c2.id, "123")
    assert r1.identity.id == i1.id and r1.user.id == u1.id
    assert r2.identity.id == i2.id and r2.user.id == u2.id
    assert r1.identity.id != r2.identity.id
    # 同 external_id 只在本 channel 命中，另一 channel 无绑定 → Fail Closed
    u3, c3, _ = _seed(db_engine, channel_name="wechat-1", user_name="carol",
                      external_id="456", channel_type="wechat")
    with pytest.raises(IdentityResolutionFailedError):
        executor.execute(c3.id, "123")
    assert u3 is not None  # 无关 seed 不影响隔离结论


# ================================================================== E Repository Boundary（源码级）


def test_e_module_imports_only_ports_and_domain() -> None:
    src = EXECUTOR_PY.read_text(encoding="utf-8")
    assert not _boundary_problems(src), _boundary_problems(src)
    roots = _roots(src)
    assert any(r == "app.repositories" or r.startswith("app.repositories.")
               for r in roots), "Execution 应依赖 repositories 端口"
    assert "app.repositories.unit_of_work" in roots


def test_e_executor_only_reads_no_writes() -> None:
    """Execution 只读组合：不调用任何 repo 的 create/update（解析 ≠ 创建）。"""
    src = EXECUTOR_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    read_calls = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)  # uow.identities.find_...
    }
    assert read_calls <= {"find_by_channel_and_external_id", "get_by_id"}, read_calls


def test_e_no_sqlalchemy_or_orm_import() -> None:
    src = EXECUTOR_PY.read_text(encoding="utf-8")
    roots = _roots(src)
    for r in roots:
        assert not any(r == f or r.startswith(f + ".") for f in FORBIDDEN_ROOTS), \
            f"越层 import {r}"


# ================================================================== G/H Management 不变


def test_g_management_service_still_crud_only() -> None:
    """9.2 不修改 Management Contract：IdentityApplicationService 仍只有
    create/get/list/update（12.4：+ list_identities，List 用例）。"""
    src = (APP_DIR / "application" / "identities.py").read_text(encoding="utf-8")
    cls = next(n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.ClassDef) and n.name == "IdentityApplicationService")
    methods = {m for m in _methods(cls) if not m.startswith("_")}
    assert methods == {"create_identity", "get_identity", "list_identities",
                       "update_identity"}, methods
    assert "resolve_identity" not in methods


def test_h_executor_does_not_import_management() -> None:
    src = EXECUTOR_PY.read_text(encoding="utf-8")
    roots = _roots(src)
    assert "app.application.identities" not in roots
    # AST 级检查：任何 import 语句都不得引入管理 Service（docstring 提及不算）
    tree = ast.parse(src, filename=str(EXECUTOR_PY))
    imported = {
        alias.name for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "IdentityApplicationService" not in imported


# ================================================================== I Repository surface 不变


def test_i_repository_surface_unchanged() -> None:
    """Runtime 不反过来污染 Repository Contract：方法面保持 6.x 冻结语义。

    12.4：user/identity 各 + list_*（Management API 的 List 用例，
    与 Runtime Resolution 无关——Runtime 只消费 find_by_channel_and_external_id）。
    """
    import app.repositories.identity as ri
    import app.repositories.user as ru

    identity_methods = {n for n in dir(ri.IdentityRepository)
                        if not n.startswith("_")}
    user_methods = {n for n in dir(ru.UserRepository) if not n.startswith("_")}
    # M6.6-C3 / ADR-12 §11：+ find_or_create（Bootstrap 事务内的幂等建立；
    # Runtime Resolution 仍只消费 find_by_channel_and_external_id）
    assert identity_methods == {"get_by_id", "create", "update",
                                "find_by_channel_and_external_id",
                                "list_identities", "find_or_create"}, identity_methods
    assert user_methods == {"get_by_id", "create", "update",
                            "list_users"}, user_methods


# ================================================================== J Execution ≠ Resolver


def test_j_executor_is_not_a_resolver() -> None:
    """Execution 组件 = 单个执行类，不创建 IdentityResolver/Service/Linker/Matcher/Policy。"""
    src = EXECUTOR_PY.read_text(encoding="utf-8")
    classes = set(_classes(src))
    assert classes == {"IdentityResolutionExecutor"}, classes
    for banned in ("IdentityResolver", "IdentityResolutionService",
                   "IdentityLinker", "IdentityMatcher",
                   "IdentityResolutionPolicy", "IdentityResolutionRule"):
        assert f"class {banned}" not in src, f"不应出现 {banned}"


# ================================================================== K mutation 探测


def _mutated(before: str, after: str) -> str:
    return EXECUTOR_PY.read_text(encoding="utf-8").replace(before, after)


@pytest.mark.parametrize("inject", [
    "import sqlalchemy",
    "from app.persistence.models.user import UserModel",
    "from app.application.identities import IdentityApplicationService",
])
def test_k_forbidden_import_is_caught(inject: str) -> None:
    src = EXECUTOR_PY.read_text(encoding="utf-8") + "\n" + inject + "\n"
    assert _boundary_problems(src), f"边界守卫未抓到 {inject}"


def test_k_write_call_is_caught() -> None:
    src = EXECUTOR_PY.read_text(encoding="utf-8").replace(
        "user = uow.users.get_by_id(identity.user_id)",
        "user = uow.users.create(User(name='guess'))",
    )
    assert any("不应写" in p for p in _boundary_problems(src)), "写调用未被守卫抓住"


def test_k_resolver_class_injection_caught_by_reverse_audit() -> None:
    """真实文件注入 IdentityResolver → 7.3 反向审计必须报红（Execution ≠ Resolver）。"""
    backup = EXECUTOR_PY.read_text(encoding="utf-8")
    try:
        EXECUTOR_PY.write_text(
            backup + "\n\nclass IdentityResolver:\n    pass\n", encoding="utf-8")
        audit_py = BACKEND_DIR / "tests" / "test_step7_3_connection_management_audit.py"
        ns: dict = {"__file__": str(audit_py), "__name__": "audit_under_9_2"}
        exec(compile(audit_py.read_text(encoding="utf-8"), str(audit_py), "exec"), ns)
        problems = ns["_next_stage_problems"]()
        assert any("IdentityResolver" in p for p in problems), \
            f"反向审计未抓到 IdentityResolver 偷渡：{problems}"
        # 本文件守卫同样要报红
        assert _boundary_problems(EXECUTOR_PY.read_text(encoding="utf-8")), \
            "模块类面守卫未抓到偷渡"
    finally:
        EXECUTOR_PY.write_text(backup, encoding="utf-8")
