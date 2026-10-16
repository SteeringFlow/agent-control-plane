"""Cloud-0.1.1-B Tenant Isolation —— Implementation verification（增量 1+2）。

覆盖实现边界 §16 的第 3/4/5/6 项（schema 层已由 test_models / gate 覆盖）：

- Repository 显式租户过滤 + 写入 stamping（第一层防线）；
- 跨租户不可见（Tenant A 的 repo/UoW 读不到 Tenant B）；
- UoW 把 tenant_id 注入每个 repo，并含 PG 事务级 SET LOCAL 钩子（第二层防线）；
- bootstrap Tenant #1 在 create_all 时自动种子（本地单租户兼容，§2.1）。

参考：docs/step-cloud-0.1.1-B-tenant-isolation-implementation-boundary.md
"""

import inspect

from sqlalchemy.orm import Session, sessionmaker

from app.domain.agent import Agent
from app.domain.user import User
from app.persistence.models.agent import AgentModel
from app.persistence.models.tenant import TenantModel
from app.persistence.models.user import UserModel
from app.persistence.repositories.agent import SqlAlchemyAgentRepository
from app.persistence.repositories.user import SqlAlchemyUserRepository
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork


# ---------------------------------------------------------------- 第一层：Repository scope


def test_repository_scopes_reads_and_writes_by_tenant(db) -> None:
    """写入显式 stamp；读取按 tenant_id 过滤——跨租户不可见。"""
    r1 = SqlAlchemyUserRepository(db, tenant_id=1)
    r2 = SqlAlchemyUserRepository(db, tenant_id=2)

    created = r1.create(User(name="alice"))
    db.flush()
    assert created.id is not None

    # tenant_id 由 Repo 在持久层 stamp（Domain/Mapper 不感知 tenant）
    assert db.get(UserModel, created.id).tenant_id == 1

    assert r1.get_by_id(created.id) is not None
    assert r2.get_by_id(created.id) is None            # 跨租户不可见
    assert [u.id for u in r1.list_users()] == [created.id]
    assert r2.list_users() == []


def test_repository_legacy_none_tenant_keeps_single_tenant_behavior(db) -> None:
    """tenant_id=None → 本地/测试单租户 legacy 路径（行为 == 隔离前）。"""
    legacy = SqlAlchemyUserRepository(db)
    created = legacy.create(User(name="bob"))
    db.flush()
    assert legacy.get_by_id(created.id) is not None
    assert [u.id for u in legacy.list_users()] == [created.id]


def test_update_does_not_cross_tenant(db) -> None:
    """租户 2 不能用租户 1 的行 id 触发 update（跨租户不可改）。"""
    r1 = SqlAlchemyUserRepository(db, tenant_id=1)
    r2 = SqlAlchemyUserRepository(db, tenant_id=2)
    created = r1.create(User(name="carol"))
    db.flush()

    import pytest

    with pytest.raises(ValueError):
        r2.update(User(id=created.id, name="hacked"))


# ---------------------------------------------------------------- 第二层：UoW 注入 + SET LOCAL


def test_uow_binds_tenant_to_every_repository(db_engine) -> None:
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=7) as uow:
        for repo in (uow.users, uow.channels, uow.agents, uow.identities,
                     uow.connections, uow.sessions, uow.interactions, uow.activities):
            assert repo._tenant_id == 7


def test_uow_source_contains_transaction_scoped_set_local_hook() -> None:
    """UoW 的 RLS 钩子必须是**事务级**租户上下文（连接池安全），且仅 PG 生效。

    用 `set_config('app.tenant_id', :tid, true)`（is_local=true ≡ SET LOCAL），
    因为 PG 的 `SET LOCAL ... = :param` 不接受绑定参数。
    """
    src = inspect.getsource(SqlAlchemyUnitOfWork)
    assert "set_config('app.tenant_id', :tid, true)" in src
    assert 'dialect.name == "postgresql"' in src


def test_uow_tenant_scope_round_trip_isolates_rows(db_engine) -> None:
    """两个租户各自 UoW：tenant 1 写入的行对 tenant 2 不可见。"""
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=1) as uow:
        uow.agents.create(Agent(name="helper", type="acp"))
        uow.commit()

    with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=2) as uow:
        assert uow.agents.list_agents() == []
    with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=1) as uow:
        assert [a.name for a in uow.agents.list_agents()] == ["helper"]


def test_agent_repository_scopes_by_tenant(db) -> None:
    r1 = SqlAlchemyAgentRepository(db, tenant_id=1)
    r2 = SqlAlchemyAgentRepository(db, tenant_id=2)
    created = r1.create(Agent(name="a1", type="acp"))
    db.flush()
    assert db.get(AgentModel, created.id).tenant_id == 1
    assert r2.get_by_id(created.id) is None
    assert r1.find_by_name("a1") is not None
    assert r2.find_by_name("a1") is None


# ---------------------------------------------------------------- §2.1 bootstrap 种子


def test_bootstrap_tenant_seeded_on_create_all(db_engine) -> None:
    """create_all 建库即种 Tenant #1（8 表 tenant_id 的 FK 目标恒存在）。"""
    with Session(db_engine) as session:
        tenant = session.get(TenantModel, 1)
    assert tenant is not None
    assert tenant.status == "active"
