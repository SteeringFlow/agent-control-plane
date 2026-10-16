"""Cloud-0.1.1-A —— PostgreSQL + Alembic Persistence Foundation 验收测试。

两层：

A. **静态 / 契约守卫**（无外部依赖，默认全量回归即运行）：
   - baseline migration 恰为 8 张核心表、DDL 不含 tenant/RLS 概念；
   - `alembic.ini` 纯 ASCII（Alembic 用 locale 读 config，防解码回归）；
   - `init_database` 的 `create_all` 仅对 SQLite 生效（A8）；
   - `reclaim_stale_processing` cutoff 为方言感知（SQLite 分支保留 + PG 分支存在）；
   - OSS `docker-compose.yml` 仍是 SQLite profile；Cloud compose 用 Alembic 而非 create_all；
   - Runtime 契约冻结：执行顺序 / RuntimeExecutionResult / AgentRequest / AgentResponse /
     AgentAdapter（本步必须零语义变化）。

B. **PostgreSQL 回归**（env 门控：``CLOUD_TEST_DATABASE_URL``；未设则 skip）：
   - fresh schema → ``alembic upgrade head`` → 恰 8 业务表 + ``alembic_version``；
   - UoW 8 实体 round-trip；
   - CAS claim 恰好一次；
   - reclaim 孤儿（方言感知 cutoff）；
   - Attempt Fence REJECTED；
   - ``init_database`` 在 PostgreSQL 上不建表（A8）。

本机（Docker PostgreSQL）运行示例：

    CLOUD_TEST_DATABASE_URL=postgresql+psycopg://acp:acp@localhost:5432/acp \
        python -m pytest tests/test_cloud_0_1_1_a_postgres_alembic_foundation.py -q
"""

import dataclasses
import os
import re
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from tests.conftest import CORE_TABLES

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent

_PG_URL_ENV = "CLOUD_TEST_DATABASE_URL"


def _pg_url() -> str:
    return os.environ.get(_PG_URL_ENV, "").strip()


# ===================================================================== A. 静态 / 契约守卫


def test_alembic_ini_is_ascii_only() -> None:
    """`alembic.ini` 必须纯 ASCII——Alembic 以 locale 编码读取 config，
    非 ASCII 注释会在非 UTF-8 locale（Windows）直接解析失败。"""
    (BACKEND / "alembic.ini").read_bytes().decode("ascii")


def test_baseline_migration_creates_exactly_eight_core_tables() -> None:
    """baseline 恰为 8 张核心表，且 DDL 不含 tenant / RLS 概念。"""
    src = (
        BACKEND / "alembic" / "versions" / "0001_baseline_core_tables.py"
    ).read_text(encoding="utf-8")
    created = set(re.findall(r'op\.create_table\(\s*"([a-z_]+)"', src))
    assert created == set(CORE_TABLES), (
        f"baseline 表集合必须恰为 8 张核心表；多出={created - set(CORE_TABLES)}，"
        f"缺少={set(CORE_TABLES) - created}"
    )

    ddl_lines = [
        line for line in src.splitlines()
        if line.strip().startswith(("op.", "sa.", "sqlite."))
    ]
    ddl = "\n".join(ddl_lines).lower()
    for forbidden in ("tenant", "policy", "rls", "row level security"):
        assert forbidden not in ddl, f"baseline DDL 不应出现越界概念：{forbidden}"


def test_baseline_migration_single_head_and_root() -> None:
    """baseline 是单一 root revision（revision=0001，down_revision=None）。"""
    src = (
        BACKEND / "alembic" / "versions" / "0001_baseline_core_tables.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "0001"' in src
    assert "down_revision: Union[str, None] = None" in src


def test_init_database_create_all_is_sqlite_only() -> None:
    """A8：`init_database` 的 `create_all` 仅对 SQLite 生效（Cloud PG 归 Alembic）。"""
    src = (BACKEND / "app" / "persistence" / "database.py").read_text(encoding="utf-8")
    assert 'bind.dialect.name != "sqlite"' in src
    # 真实调用 `Base.metadata.create_all(bind=bind)` 必须位于 sqlite 守卫之后
    # （用 rindex：docstring 里也提到了 create_all，取最后一次出现＝代码调用）
    guard = src.index('if bind.dialect.name != "sqlite":')
    create_all = src.rindex("Base.metadata.create_all(bind=bind)")
    assert guard < create_all


def test_reclaim_cutoff_is_dialect_aware() -> None:
    """reclaim cutoff 方言感知：SQLite 分支原样保留 + PG 分支存在。"""
    src = (
        BACKEND / "app" / "persistence" / "repositories" / "interaction_record.py"
    ).read_text(encoding="utf-8")
    assert "def _stale_cutoff" in src
    assert 'dialect == "sqlite"' in src
    assert 'func.datetime("now"' in src           # SQLite 原表达式保留
    assert "timedelta" in src and "Interval" in src  # PostgreSQL 相对间隔绑定
    assert "text(" not in src                     # 不拼裸 SQL（既有守卫）


def test_oss_compose_is_still_sqlite_profile() -> None:
    """A7：OSS `docker-compose.yml` 仍为 SQLite profile（未被 Cloud 改动）。"""
    src = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "sqlite:///" in src
    assert "postgres" not in src.lower()


def test_cloud_compose_uses_alembic_not_create_all() -> None:
    """Cloud profile 用 `alembic upgrade head` 建 schema（A8：不用 create_all）。"""
    src = (REPO_ROOT / "docker-compose.cloud.yml").read_text(encoding="utf-8")
    assert "alembic upgrade head" in src
    assert "postgresql+psycopg://" in src
    # create_all 只允许出现在注释里（说明「不用 create_all」），不得是实际配置
    executable = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert "create_all" not in executable


def test_runtime_execution_order_frozen() -> None:
    """Runtime execution order 冻结不变（本步零语义变化）。"""
    from app.application.runtime import RUNTIME_EXECUTION_ORDER

    assert RUNTIME_EXECUTION_ORDER == (
        "identity_resolution",
        "authorization",  # Package 2 / Step 2（ADR-7 / ADR-8）
        "routing",
        "session_resolution",
        "session_creation",
        "agent_dispatch",
        "delivery",
    )


def test_runtime_execution_result_field_surface_frozen() -> None:
    """RuntimeExecutionResult 字段面恰好三个（未经审计不得扩展）。"""
    from app.domain.runtime_execution_result import RuntimeExecutionResult

    assert [f.name for f in dataclasses.fields(RuntimeExecutionResult)] == [
        "session_mode", "session_id", "agent_response",
    ]


def test_agent_contract_field_surfaces_frozen() -> None:
    """AgentRequest / AgentResponse 字段面冻结不变。"""
    from app.domain.agent_request import AgentRequest
    from app.domain.agent_response import AgentResponse

    assert [f.name for f in dataclasses.fields(AgentRequest)] == [
        "agent_id", "control_plane_session_id", "content",
        "external_session_id", "metadata",
    ]
    assert [f.name for f in dataclasses.fields(AgentResponse)] == [
        "content", "external_session_id", "metadata", "artifacts",
    ]


def test_agent_adapter_contract_signature_frozen() -> None:
    """AgentAdapter Port 仍为 `send(request, connection) -> AgentResponse`。"""
    import inspect as _inspect

    from app.repositories.agent_adapter import AgentAdapter

    sig = _inspect.signature(AgentAdapter.send)
    assert list(sig.parameters) == ["self", "request", "connection"]


# ===================================================================== B. PostgreSQL 回归


pytestmark_pg = pytest.mark.skipif(
    not _pg_url(),
    reason=f"需要 {_PG_URL_ENV} 指向可重置的 PostgreSQL（Cloud profile）",
)


def _alembic_upgrade(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(cfg, "head")
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev


def _reset_schema(url: str) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


@pytest.fixture()
def pg_engine():
    """每个测试：public schema 归零 → alembic upgrade head → 返回绑定 engine。"""
    url = _pg_url()
    _reset_schema(url)
    _alembic_upgrade(url)
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


def _uow_factory(engine):
    from app.core.tenant import TENANT_LOCAL
    from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

    # Cloud-0.1.1-B：PG 上 RLS 已启用，UoW 必须绑定租户（未 SET LOCAL → fail-closed）。
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        tenant_id=TENANT_LOCAL)


@pytestmark_pg
def test_fresh_pg_after_upgrade_has_eight_core_tables_plus_tenants(pg_engine) -> None:
    """A5：fresh PostgreSQL + `alembic upgrade head` → 8 张业务表 + tenants
    + Cloud-0.1.1-C 的 2 张 Browser Auth 表 + alembic_version。"""
    tables = set(inspect(pg_engine).get_table_names())
    assert tables == set(CORE_TABLES) | {
        "tenants", "user_credentials", "browser_sessions", "alembic_version",
        # Implementation Package 2 / Step 1：授权状态存储（非 4.2 业务表）。
        "connection_conversations",
        # M6.6-C3 / ADR-12 迁移 0008：Bootstrap 窗口存储（不是授权状态）。
        "bootstrap_arms"}


@pytestmark_pg
def test_alembic_revision_is_head(pg_engine) -> None:
    """`alembic upgrade head` 后 alembic_version 必须等于**当前 head**。

    ★ baseline 漂移修复（Implementation Package 1 / Gate 3 §9-1）：
    本断言原名 `test_alembic_revision_is_0002` 并硬编码 `"0003"`，而迁移链 head
    早已推进到 0005（0004 channels_bootstrap_rls / 0005 RLS robustness 追加时未
    同步）。该测试是 PG-gated（本地默认 skip），漂移因此长期未暴露——它吞掉了
    真实的「head 与断言不一致」信号。

    修法不是把字面量改成 "0006"（那只会把同一个坑推迟），而是**从迁移脚本目录
    动态取 head**——同类漂移从结构上不可能再复发。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    expected = ScriptDirectory.from_config(cfg).get_current_head()

    with pg_engine.connect() as conn:
        actual = conn.execute(text("select version_num from alembic_version")).scalar()
    assert actual == expected, f"alembic_version={actual} 与 head={expected} 不一致"


@pytestmark_pg
def test_uow_eight_entity_roundtrip_on_pg(pg_engine) -> None:
    """A6：8 实体经既有 Repository/UoW 在 PostgreSQL 上完整 round-trip（零语义变化）。"""
    from app.domain.activity_record import ActivityRecord
    from app.domain.agent import Agent
    from app.domain.channel import Channel
    from app.domain.connection import Connection
    from app.domain.identity import Identity
    from app.domain.interaction_record import InteractionRecord
    from app.domain.session import Session as SessionEntity
    from app.domain.user import User

    gf = _uow_factory(pg_engine)

    with gf() as uow:
        user = uow.users.create(User(name="pg-user"))
        channel = uow.channels.create(Channel(name="pg-channel", type="local", config={}))
        agent = uow.agents.create(Agent(name="pg-agent", type="acp"))
        identity = uow.identities.create(Identity(
            user_id=user.id, channel_id=channel.id, external_id="ext-1"))
        connection = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="pg-path", status="active"))
        session = uow.sessions.create(SessionEntity(
            identity_id=identity.id, channel_id=channel.id,
            connection_id=connection.id,
            external_conversation_id="conv-1", status="active"))
        interaction = uow.interactions.create(InteractionRecord(
            channel_id=channel.id, external_interaction_id="msg-1",
            session_id=session.id))
        uow.activities.append(ActivityRecord(
            type="interaction_received", status="ok",
            interaction_id=interaction.id, session_id=session.id))
        uow.commit()
        ids = {
            "user": user.id, "channel": channel.id, "agent": agent.id,
            "identity": identity.id, "connection": connection.id,
            "session": session.id, "interaction": interaction.id,
        }

    with gf() as uow:
        assert uow.users.get_by_id(ids["user"]).name == "pg-user"
        assert uow.channels.get_by_id(ids["channel"]).type == "local"
        assert uow.agents.get_by_id(ids["agent"]).name == "pg-agent"
        assert uow.identities.get_by_id(ids["identity"]).external_id == "ext-1"
        assert uow.connections.get_by_id(ids["connection"]).agent_id == ids["agent"]
        assert uow.sessions.find_active(
            ids["identity"], ids["connection"], "conv-1") is not None
        record = uow.interactions.get_by_id(ids["interaction"])
        assert record.channel_id == ids["channel"]
        assert record.processing_status == "pending"
        evidence = uow.activities.list_by_interaction(ids["interaction"])
        assert len(evidence) == 1 and evidence[0].type == "interaction_received"


@pytestmark_pg
def test_cas_claim_reclaim_and_attempt_fence_on_pg(pg_engine) -> None:
    """A6：CAS claim 恰好一次 / 孤儿 reclaim / Attempt Fence REJECTED 在 PG 上成立。"""
    from app.domain.channel import Channel
    from app.domain.interaction_record import (
        InteractionRecord,
        OutcomeCommit,
    )

    gf = _uow_factory(pg_engine)

    with gf() as uow:
        channel = uow.channels.create(Channel(name="pg-c", type="local", config={}))
        record = uow.interactions.create(InteractionRecord(
            channel_id=channel.id, external_interaction_id="m-1"))
        uow.commit()
        channel_id, interaction_id = channel.id, record.id

    # CAS claim：恰好一次成功，第二次（已 processing）失败
    with gf() as uow:
        assert uow.interactions.claim_processing(interaction_id) is True
        uow.commit()
    with gf() as uow:
        assert uow.interactions.claim_processing(interaction_id) is False
        uow.commit()

    with gf() as uow:
        t1 = uow.interactions.get_by_id(interaction_id).processing_started_at
    assert t1 is not None  # DB 时钟写入的 fence 值

    # reclaim：方言感知 cutoff（threshold=0）；跨过 claim 时刻后必回收
    time.sleep(1.1)
    with gf() as uow:
        reclaimed = uow.interactions.reclaim_stale_processing(0)
        uow.commit()
    assert reclaimed >= 1
    with gf() as uow:
        row = uow.interactions.get_by_id(interaction_id)
        assert row.processing_status == "pending"
        assert row.processing_started_at is None  # reclaim 必置 NULL

    # 新 attempt 重新 claim（新的 fence 值）
    with gf() as uow:
        assert uow.interactions.claim_processing(interaction_id) is True
        uow.commit()

    # 旧 attempt 带 T1 迟到落 outcome → REJECTED（不写穿）
    with gf() as uow:
        outcome = uow.interactions.record_outcome(
            interaction_id,
            processing_status="succeeded",
            delivery_status="delivered",
            expected_started_at=t1,
        )
        uow.commit()
    assert outcome is OutcomeCommit.REJECTED


@pytestmark_pg
def test_init_database_does_not_create_tables_on_postgres() -> None:
    """A8：`init_database()` 在 PostgreSQL 上必须不建任何表（schema 归 Alembic）。"""
    from app.persistence.database import init_database

    url = _pg_url()
    _reset_schema(url)  # 归零：无 alembic_version、无业务表
    engine = create_engine(url)
    try:
        init_database(engine)
        assert set(inspect(engine).get_table_names()) == set()
    finally:
        engine.dispose()
