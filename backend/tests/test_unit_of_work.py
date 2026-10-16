"""Step 5.5 验收：Persistence Transaction Boundary & Unit of Work。

A — 三个 Repository 共享同一个 Session
B — Commit 后跨 Session 可见
C — 显式 rollback 后不落库
D — 异常自动 rollback（比显式 rollback 更重要）
E — 退出后 Session 关闭、连接归还（不泄漏）
F — Repository 不 commit：正常退出（无 commit）也不落库
边界 — UoW 不是 Service（无业务方法）、不知道 Domain、不引入任何事务框架
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.user import User
from app.persistence.models.agent import AgentModel
from app.persistence.models.channel import ChannelModel
from app.persistence.models.user import UserModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _count(db_engine, model) -> int:
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(model)
        ).scalar_one()


# ---------------------------------------------------------------- A: 共享 Session


def test_a_repositories_share_the_same_session(db_engine) -> None:
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        assert uow.session is not None
        assert uow.users._session is uow.session
        assert uow.channels._session is uow.session
        assert uow.agents._session is uow.session
        assert uow.identities._session is uow.session
        assert uow.connections._session is uow.session


# ---------------------------------------------------------------- B: Commit


def test_b_commit_persists_across_sessions(db_engine) -> None:
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        uow.users.create(User(name="zhang-san"))
        uow.channels.create(Channel(name="wecom", type="wecom"))
        uow.agents.create(Agent(name="helper", type="acp"))
        uow.commit()

    assert _count(db_engine, UserModel) == 1
    assert _count(db_engine, ChannelModel) == 1
    assert _count(db_engine, AgentModel) == 1


# ---------------------------------------------------------------- C: 显式 rollback


def test_c_explicit_rollback_leaves_db_unchanged(db_engine) -> None:
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        uow.users.create(User(name="temp-user"))
        uow.channels.create(Channel(name="temp-channel", type="wecom"))
        uow.rollback()

    assert _count(db_engine, UserModel) == 0
    assert _count(db_engine, ChannelModel) == 0


# ---------------------------------------------------------------- D: 异常 rollback


def test_d_exception_rolls_back_automatically(db_engine) -> None:
    with pytest.raises(RuntimeError):
        with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
            uow.users.create(User(name="boom-user"))
            uow.channels.create(Channel(name="boom-channel", type="wecom"))
            raise RuntimeError("boom")

    assert _count(db_engine, UserModel) == 0
    assert _count(db_engine, ChannelModel) == 0


# ---------------------------------------------------------------- E: 不泄漏 Session


def test_e_session_is_closed_and_connection_returned() -> None:
    """用临时文件引擎（QueuePool 有 checkedout）验证连接归还、Session 不泄漏。"""
    import tempfile
    from pathlib import Path

    from sqlalchemy import create_engine

    from app.persistence.database import Base as app_base

    path = Path(tempfile.mkdtemp(prefix="acp-uow-leak-")) / "probe.db"
    bind = create_engine(f"sqlite:///{path}")
    app_base.metadata.create_all(bind)
    factory = sessionmaker(bind=bind, class_=Session, expire_on_commit=False)

    before = bind.pool.checkedout()
    uow = SqlAlchemyUnitOfWork(session_factory=factory)
    with uow:
        uow.users.create(User(name="leak-check"))
        assert bind.pool.checkedout() == before + 1

    # 退出后：Session 引用被清空、Repository 被释放、连接已归还
    assert uow.session is None
    assert uow.users is None
    assert bind.pool.checkedout() == before
    bind.dispose()


# ---------------------------------------------------------------- F: Repository 不 commit


def test_f_no_implicit_commit_on_clean_exit(db_engine) -> None:
    """没有显式 commit() 也没有异常：正常退出也必须不落库。

    证明 commit 永远属于 UoW，Repository 只 flush。
    """
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        uow.users.create(User(name="no-commit-user"))

    assert _count(db_engine, UserModel) == 0


def test_f_repository_source_never_commits() -> None:
    """行为之外再加源码级守护：adapter 与 repository 实现里没有 commit。"""
    from app.persistence.repositories import user as user_adapter

    tree = ast.parse(Path(user_adapter.__file__).read_text(encoding="utf-8"))
    calls = [
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert "commit" not in calls


# ---------------------------------------------------------------- 边界


def test_uow_is_not_a_service(db_engine) -> None:
    """UoW 只暴露 Repository 属性 + commit/rollback，绝无业务方法。"""
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        # 实例面：只有基础设施属性（无业务操作方法）
        assert set(vars(uow)) == {
            "_session_factory", "_tenant_id", "session",
            "users", "channels", "agents", "identities",
            "connections", "connection_conversations",
            # M6.6-C3 / ADR-12: + bootstrap_arms（Bootstrap 窗口 Port）
            "bootstrap_arms",
            "sessions", "interactions", "activities",
            "browser_auth", "tenants",
        }  # Package 2 / Step 1: + connection_conversations（授权状态 Port）
        # 10.4.2: + interactions / activities；Cloud-0.1.1-C: + browser_auth
        # Cloud-0.1.2.1: + tenants（TenantProvisioner 端口，root catalog）
        # Cloud-0.1.1-B: + _tenant_id（租户作用域，基础设施属性，非业务方法）
        for bad in ("create_user", "create_channel", "create_agent",
                    "resolve_identity", "route_agent", "dispatch"):
            assert not hasattr(uow, bad), f"UoW 不应是 Service：出现了 {bad}"


def test_uow_source_has_no_business_methods() -> None:
    """源码级：unit_of_work.py 定义的方法面锁死为基础设施方法。"""
    from app.persistence import unit_of_work as uow_module

    path = Path(uow_module.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }
    allowed = {"__init__", "__enter__", "__exit__", "commit", "rollback", "close"}
    assert methods <= allowed, f"UoW 出现业务方法：{methods - allowed}"


def test_uow_does_not_import_domain() -> None:
    """UoW 不知道 Domain：不 import app.domain / app.repositories。"""
    from app.persistence import unit_of_work as uow_module

    tree = ast.parse(Path(uow_module.__file__).read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)

    forbidden = ("app.domain", "app.repositories")
    leaked = [r for r in roots if any(r == f or r.startswith(f + ".") for f in forbidden)]
    assert not leaked, f"UoW 不应知道 Domain / Port：{leaked}"
