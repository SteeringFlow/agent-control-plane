"""Cloud-0.1.1-B Tenant Isolation —— Architecture Contract Gate（实现后 / 演进版）。

原文件是「实现前基线锁」：锁定 runtime 契约无 tenant、8 表无 tenant_id、`tenants`
不存在、仅 0001 迁移。Cloud-0.1.1-B 实现后，按实现边界 §19.2 演进为
「实现后不变量」，并**保留全部运行时红线断言**：

KEEP（运行时红线，不得削弱）：
  - 6 个运行时契约字段面无 `tenant_id`；
  - repositories 直接以 SQLAlchemy 构建查询（过滤可注入）；
  - 全工程无隐式全局 tenant 上下文（无 current_tenant / global_tenant）；
  - PG 引擎默认连接池（证明必须用事务级 SET LOCAL）；
  - SDK 完全租户无关。

EVOLVE（schema 断言翻转为实现后状态）：
  - `tenants` root 表存在，且自身不带 `tenant_id`；
  - 8 张业务表各含 `tenant_id`（FK → tenants.id）；
  - 3 个复合唯一约束前导 `tenant_id`；`sessions` 新增复合唯一约束；
  - `app/core/tenant.py` 提供 `TenantScope` + 入口端口（实现边界 §6）；
  - Alembic 基线 0001 不变（tenant 迁移 0002 由实现 Increment 4 引入）。

参考：docs/step-cloud-0.1.1-B-tenant-isolation-implementation-boundary.md
"""

import dataclasses
import inspect
from pathlib import Path

from sqlalchemy import UniqueConstraint

from app.domain.agent_request import AgentRequest
from app.domain.agent_response import AgentResponse
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session import Session
from app.domain.session_resolution import SessionResolution
from app.domain.inbound_interaction import InboundInteraction
from app.persistence import models as _models_pkg  # noqa: F401  注册 9 张表到 Base.metadata
from app.persistence.database import Base, _engine_kwargs
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.persistence.models.tenant import TenantModel  # noqa: F401  确认 root 表存在
from app.persistence.repositories.user import SqlAlchemyUserRepository
from app.persistence.repositories.identity import SqlAlchemyIdentityRepository
from app.persistence.repositories.channel import SqlAlchemyChannelRepository
from app.persistence.repositories.agent import SqlAlchemyAgentRepository
from app.persistence.repositories.connection import SqlAlchemyConnectionRepository
from app.persistence.repositories.session import SqlAlchemySessionRepository
from app.persistence.repositories.interaction_record import (
    SqlAlchemyInteractionRecordRepository,
)
from app.persistence.repositories.activity_record import SqlAlchemyActivityRecordRepository
from app.core.tenant import TenantProvisioner, TenantResolver, TenantScope

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
SDK = BACKEND / "agent_runtime_sdk"

CORE_TABLES = [
    "users", "identities", "channels", "agents",
    "connections", "sessions", "interactions", "activities",
]

REPO_ADAPTERS = [
    SqlAlchemyUserRepository, SqlAlchemyIdentityRepository,
    SqlAlchemyChannelRepository, SqlAlchemyAgentRepository,
    SqlAlchemyConnectionRepository, SqlAlchemySessionRepository,
    SqlAlchemyInteractionRecordRepository, SqlAlchemyActivityRecordRepository,
]

RUNTIME_CONTRACTS = [
    AgentRequest, AgentResponse, RuntimeExecutionResult,
    SessionResolution, Session, InboundInteraction,
]


# ---------------------------------------------------------------- helpers


def _table(name):
    return Base.metadata.tables[name]


def _columns(name):
    return {c.name for c in _table(name).columns}


def _unique_cols(name) -> set[frozenset]:
    out = set()
    for cons in _table(name).constraints:
        if isinstance(cons, UniqueConstraint):
            out.add(frozenset(c.name for c in cons.columns))
    return out


# ================================================================ Runtime boundary (KEEP)


def test_runtime_contracts_have_no_tenant_id() -> None:
    """红线：6 个运行时契约字段面均无 tenant_id（Runtime 完全租户无关）。"""
    for cls in RUNTIME_CONTRACTS:
        names = {f.name for f in dataclasses.fields(cls)}
        assert "tenant_id" not in names, f"{cls.__name__} 不应含 tenant_id：{names}"


# ================================================================ Schema after tenant (EVOLVE)


def test_eight_core_tables_have_tenant_id_fk_to_tenants() -> None:
    """8 张业务表均含 `tenant_id`，且 FK 指向 `tenants.id`（加性分区维度）。"""
    for t in CORE_TABLES:
        assert t in Base.metadata.tables, f"缺少核心表 {t}"
        cols = _columns(t)
        assert "id" in cols, f"{t} 无整数 PK"
        assert "tenant_id" in cols, f"{t} 缺少 tenant_id"
        targets = {fk.target_fullname for fk in _table(t).foreign_keys}
        assert "tenants.id" in targets, f"{t}.tenant_id 未指向 tenants.id：{targets}"


def test_tenants_root_present_without_tenant_id() -> None:
    """`tenants` 是 root catalog：存在，且自身不带 tenant_id 维度。"""
    assert "tenants" in Base.metadata.tables
    t = _table("tenants")
    cols = {c.name for c in t.columns}
    assert cols == {"id", "name", "status", "config", "created_at", "updated_at"}
    assert "tenant_id" not in cols, "tenants root 不应带 tenant_id"


def test_existing_business_fks_intact() -> None:
    """既有业务 FK 完整保留（tenant_id 是正交新增，不改既有关系图）。"""
    id_fks = {fk.target_fullname for fk in _table("identities").foreign_keys}
    assert "users.id" in id_fks and "channels.id" in id_fks
    conn_fks = {fk.target_fullname for fk in _table("connections").foreign_keys}
    assert "channels.id" in conn_fks and "agents.id" in conn_fks


# ================================================================ Repository isolation (KEEP)


def test_repositories_build_sqlalchemy_queries_with_injectable_session() -> None:
    """每个 repo adapter 直接以 SQLAlchemy Session 构建查询（过滤可注入），
    且构造签名含 session（tenant_id 由 UoW 注入）。"""
    for repo in REPO_ADAPTERS:
        sig = inspect.signature(repo.__init__)
        assert "session" in sig.parameters, f"{repo.__name__} 构造未接收 session"
        src = inspect.getsource(repo)
        assert "select(" in src, f"{repo.__name__} 未用 sqlalchemy.select"
        assert "self._session" in src, f"{repo.__name__} 未基于注入的 session 查询"


def test_no_implicit_global_tenant_context() -> None:
    """全工程无隐式全局 tenant 上下文（红线）。"""
    for root in (APP, SDK):
        for py in root.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            assert "current_tenant" not in text, f"{py} 含隐式全局 current_tenant"
            assert "global_tenant" not in text, f"{py} 含隐式全局 global_tenant"


# ================================================================ UoW / pool (KEEP)


def test_uow_transaction_lifecycle_boundary() -> None:
    """UoW 持有单一 session 工厂 + 全部 repo 构造注入点 + 事务边界。"""
    src = inspect.getsource(SqlAlchemyUnitOfWork)
    assert "self._session_factory()" in src, "UoW 应持有并创建共享 session"
    assert "SqlAlchemyUserRepository(" in src, "UoW 是 repo 的唯一构造注入点"
    assert "def commit" in src and "__exit__" in src, "UoW 需暴露事务边界"


def test_pg_engine_uses_connection_pool_justifying_set_local() -> None:
    """对 PG 引擎不设置自定义 poolclass（默认 QueuePool）→ 必须用事务级 SET LOCAL。"""
    assert _engine_kwargs("postgresql://u:p@localhost/db") == {}
    assert _engine_kwargs("postgresql+psycopg://u:p@h/db") == {}
    assert _engine_kwargs("sqlite:///x.db") == {
        "connect_args": {"check_same_thread": False}}


# ================================================================ Unique / index (EVOLVE)


def test_unique_constraints_lead_with_tenant_id() -> None:
    """3 个复合唯一约束前导 tenant_id；sessions 新增复合唯一约束（RLS 不保护 UNIQUE）。"""
    uc = {
        "identities": _unique_cols("identities"),
        "connections": _unique_cols("connections"),
        "interactions": _unique_cols("interactions"),
        "sessions": _unique_cols("sessions"),
    }
    assert frozenset({"tenant_id", "channel_id", "external_id"}) in uc["identities"]
    assert frozenset({"tenant_id", "channel_id", "agent_id"}) in uc["connections"]
    assert frozenset({"tenant_id", "channel_id", "external_interaction_id"}) in uc["interactions"]
    assert frozenset({
        "tenant_id", "identity_id", "connection_id", "external_conversation_id",
    }) in uc["sessions"]


def test_tenant_id_indexed_on_all_core_tables() -> None:
    """8 张业务表均有 ix_<t>_tenant_id（RLS policy 扫描 / 租户作用域查询必需）。"""
    for t in CORE_TABLES:
        names = {idx.name for idx in _table(t).indexes}
        assert f"ix_{t}_tenant_id" in names, f"{t} 缺少 ix_{t}_tenant_id：{names}"


# ================================================================ Migration baseline (KEEP 0001)


def test_alembic_has_tenant_migration_0002() -> None:
    """租户迁移作为 0002 追加：revises 0001，含 tenants / tenant_id / RLS。"""
    versions = sorted((BACKEND / "alembic" / "versions").glob("*.py"))
    migs = [p for p in versions if p.name != "__init__.py" and p.stem != "__pycache__"]
    names = {p.name for p in migs}
    assert any(n.startswith("0002") for n in names), f"缺少 0002 租户迁移：{names}"
    text = next(p for p in migs if p.name.startswith("0002")).read_text(encoding="utf-8")
    assert 'revision: str = "0002"' in text
    assert 'down_revision: Union[str, None] = "0001"' in text
    assert 'op.create_table(' in text and '"tenants"' in text
    assert "tenant_id" in text
    assert "ROW LEVEL SECURITY" in text
    assert "app.tenant_id" in text


def test_alembic_baseline_0001_unchanged() -> None:
    """基线 0001 仍存在且为 root revision（tenant 迁移作为 0002 追加，不回改 0001）。"""
    versions = sorted((BACKEND / "alembic" / "versions").glob("*.py"))
    migs = [p for p in versions if p.name != "__init__.py" and p.stem != "__pycache__"]
    assert migs, "应至少存在 0001 基线迁移"
    baseline = [p for p in migs if p.name.startswith("0001")]
    assert baseline, f"缺少 0001 基线迁移：{[p.name for p in migs]}"
    text = baseline[0].read_text(encoding="utf-8")
    assert 'revision: str = "0001"' in text
    assert "down_revision" in text and "= None" in text
    assert "tenant_id" not in text.lower(), "基线 0001 不应含 tenant_id（应由 0002 引入）"


# ================================================================ Core tenant contracts (EVOLVE)


def test_tenant_scope_and_ingress_ports_exist() -> None:
    """TenantScope 是显式对象（唯一载荷 tenant_id）；入口端口为 Protocol。"""
    assert [f.name for f in dataclasses.fields(TenantScope)] == ["tenant_id"]
    assert getattr(TenantResolver, "_is_protocol", False)
    assert getattr(TenantProvisioner, "_is_protocol", False)


# ================================================================ SDK tenant-agnostic (KEEP)


def test_sdk_is_tenant_agnostic() -> None:
    """Agent Runtime SDK 完全租户无关。"""
    for py in SDK.rglob("*.py"):
        low = py.read_text(encoding="utf-8").lower()
        assert "tenant" not in low, f"{py} 含 tenant 引用（SDK 应租户无关）"


# ================================================================ Ingress DTO (KEEP)


def test_ingress_dto_carrier_has_no_tenant_id() -> None:
    """InboundInteraction 不含 tenant_id：tenant 在基础设施层解析，不进 Agent 消息体。"""
    assert "tenant_id" not in {f.name for f in dataclasses.fields(InboundInteraction)}
