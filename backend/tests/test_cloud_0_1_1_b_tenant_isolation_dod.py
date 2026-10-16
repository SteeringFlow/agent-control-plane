"""Cloud-0.1.1-B —— Tenant Isolation **DoD gate**（实现边界 §16 十项 + §18.2）。

逐项对齐 owner 的 Implementation DoD：

1. fresh PG migration                 → 需 PostgreSQL（本文件 PG-gated 覆盖）
2. tenants + 8 tables schema          → 本文件 / test_models / gate
3. tenant_id / FK / indexes / UNIQUE  → 本文件 / gate
4. TenantScope lifecycle              → 本文件
5. SET LOCAL cleanup                  → 本文件（钩子存在 + 非 PG 不执行，PG 清理属 PG-gated）
6. Repository cross-tenant isolation  → 本文件 / test_..._impl
7. RLS cross-tenant isolation         → 需 PostgreSQL（PG-gated）
8. Worker/Driver tenant-scoped        → 本文件 / test_..._fanout
9. existing SQLite behavior           → 全量回归（本文件锁 legacy 恒等）
10. Runtime semantic diff = ZERO      → 本文件 / gate

参考：docs/step-cloud-0.1.1-B-tenant-isolation-implementation-boundary.md
"""

import dataclasses
import inspect
from pathlib import Path

from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import sessionmaker

from app.application.tenant_fanout import TenantFanoutDriver
from app.core.tenant import TENANT_LOCAL, TenantScope, default_tenant_scope
from app.domain.agent_request import AgentRequest
from app.domain.agent_response import AgentResponse
from app.domain.inbound_interaction import InboundInteraction
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session import Session
from app.domain.session_resolution import SessionResolution
from app.domain.user import User
from app.persistence import models as _models  # noqa: F401
from app.persistence.database import Base
from app.persistence.models.tenant import TenantModel
from app.persistence.repositories.user import SqlAlchemyUserRepository
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"

CORE_TABLES = ("users", "identities", "channels", "agents",
               "connections", "sessions", "interactions", "activities")
RUNTIME_CONTRACTS = (AgentRequest, AgentResponse, RuntimeExecutionResult,
                     SessionResolution, Session, InboundInteraction)
BUSINESS_DOMAIN = (User,)


# ---------------------------------------------------------------- DoD 2/3


def test_dod_schema_tenants_plus_eight_tables_with_tenant_fk_index_unique() -> None:
    assert set(Base.metadata.tables) == set(CORE_TABLES) | {
        "tenants", "user_credentials", "browser_sessions",
        # Implementation Package 2 / Step 1：授权状态存储（非 4.2 业务表）。
        "connection_conversations",
        # M6.6-C3 / ADR-12：Bootstrap 窗口存储（**不是**授权状态；不参与 granted 判定）。
        "bootstrap_arms"}
    # 新表同样必须满足 tenant 隔离三要件（列 / FK / 索引）——不是可选项。
    for t in ("connection_conversations", "bootstrap_arms"):
        table = Base.metadata.tables[t]
        assert "tenant_id" in {c.name for c in table.columns}
        assert "tenants.id" in {fk.target_fullname for fk in table.foreign_keys}
        assert f"ix_{t}_tenant_id" in {ix.name for ix in table.indexes}
    for t in CORE_TABLES:
        table = Base.metadata.tables[t]
        assert "tenant_id" in {c.name for c in table.columns}
        assert "tenants.id" in {fk.target_fullname for fk in table.foreign_keys}
        assert f"ix_{t}_tenant_id" in {ix.name for ix in table.indexes}
    for t, cols in (
        ("identities", {"tenant_id", "channel_id", "external_id"}),
        ("connections", {"tenant_id", "channel_id", "agent_id"}),
        ("interactions", {"tenant_id", "channel_id", "external_interaction_id"}),
        ("sessions", {"tenant_id", "identity_id", "connection_id",
                      "external_conversation_id"}),
    ):
        uniques = [c for c in Base.metadata.tables[t].constraints
                   if isinstance(c, UniqueConstraint)]
        assert any({c.name for c in u.columns} == cols for u in uniques), \
            f"{t} 缺少 {cols} 唯一约束"


# ---------------------------------------------------------------- DoD 4


def test_dod_tenant_scope_lifecycle() -> None:
    assert [f.name for f in dataclasses.fields(TenantScope)] == ["tenant_id"]
    assert default_tenant_scope() == TenantScope(tenant_id=TENANT_LOCAL)


# ---------------------------------------------------------------- DoD 5


def test_dod_uow_binds_tenant_and_has_transaction_scoped_set_local_hook() -> None:
    src = inspect.getsource(SqlAlchemyUnitOfWork)
    # set_config(..., is_local=true) ≡ SET LOCAL（事务级自动清理）；PG 的 SET LOCAL 不收绑定参数
    assert "set_config('app.tenant_id', :tid, true)" in src
    assert 'dialect.name == "postgresql"' in src   # 仅 PG；本地 SQLite 无 RLS


# ---------------------------------------------------------------- DoD 6


def test_dod_repository_cross_tenant_isolation(db) -> None:
    r1 = SqlAlchemyUserRepository(db, tenant_id=1)
    r2 = SqlAlchemyUserRepository(db, tenant_id=2)
    created = r1.create(User(name="alice"))
    db.flush()
    assert r1.get_by_id(created.id) is not None
    assert r2.get_by_id(created.id) is None
    assert r2.list_users() == []


# ---------------------------------------------------------------- DoD 8


def test_dod_worker_driver_tenant_fanout() -> None:
    built = []
    fan = TenantFanoutDriver(
        tenant_source=lambda: [TenantScope(1), TenantScope(2)],
        driver_factory=lambda s: (built.append(s.tenant_id) or _NoopDriver()),
    )
    fan.start()
    assert built == [1, 2]          # 每租户各一套（tenant-scoped run）
    fan.stop()


class _NoopDriver:
    def start(self) -> None: ...
    def stop(self) -> None: ...


# ---------------------------------------------------------------- DoD 9


def test_dod_legacy_single_tenant_behavior_is_identity(db) -> None:
    """tenant_id=None → 行为 == 隔离前（单租户恒等）。"""
    legacy = SqlAlchemyUserRepository(db)   # 无 tenant 作用域
    created = legacy.create(User(name="bob"))
    db.flush()
    assert legacy.get_by_id(created.id) is not None
    assert [u.id for u in legacy.list_users()] == [created.id]


# ---------------------------------------------------------------- DoD 10


def test_dod_runtime_semantic_diff_is_zero() -> None:
    """运行时契约 + domain 实体均无 tenant_id（Runtime 完全租户无关）。"""
    for cls in RUNTIME_CONTRACTS + BUSINESS_DOMAIN:
        assert "tenant_id" not in {f.name for f in dataclasses.fields(cls)}
    from app.application.runtime import RUNTIME_EXECUTION_ORDER

    assert RUNTIME_EXECUTION_ORDER == (
        "identity_resolution", "authorization", "routing", "session_resolution",
        "session_creation", "agent_dispatch", "delivery",
    )


# ---------------------------------------------------------------- DoD 1/7（PG-gated）

# DoD 1（fresh PG migration）/ 5（SET LOCAL cleanup）/ 7（RLS cross-tenant）的**权威
# 证据**来自 Linux + PostgreSQL 的 Closure Evidence Run：
#     tools/closure_evidence.py → A. Migration / B. SET LOCAL / C. RLS
# 以及 `tests/test_cloud_0_1_1_a_postgres_alembic_foundation.py` 的 PG 迁移/round-trip
# 测试（PG-gated）。此处不重复直接连库断言（会与 pg_engine 的 DROP SCHEMA 夹具
# 产生顺序耦合）。
