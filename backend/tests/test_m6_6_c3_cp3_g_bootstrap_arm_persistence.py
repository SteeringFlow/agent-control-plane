"""M6.6-C3-CP3-G — Bootstrap Arm 持久化 / 生命周期 / 部分唯一索引（G-I）。

被守护的不变量（CP3-D O-1 / CP3-F §2~§10）：

    INV-1  同一 (tenant, channel, connection) **至多一条 armed** —— 权威层 = **DB**
    INV-2  consumed / revoked 是终态
    INV-3  过期 = 逻辑判定（`armed AND expires_at <= now()`），不建状态值、不删行
    INV-4  消费 = CAS（条件 UPDATE + rowcount），过期 Arm 不可消费
    INV-5  时间判据一律由 **DB 时钟**给出

G-I：部分唯一索引的**安全权威层**测试（不是普通 migration coverage）——
必须同时验证 SQLite 与 PostgreSQL（PG 侧 env 门控）。
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.tenant import TENANT_LOCAL
from app.domain.agent import Agent
from app.domain.bootstrap_arm import (
    STATUS_ARMED,
    STATUS_CONSUMED,
    STATUS_REVOKED,
    BootstrapArm,
)
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.user import User
from app.persistence.models.bootstrap_arm import BootstrapArmModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

_BACKEND = Path(__file__).resolve().parents[1]
_PG_URL_ENV = "CLOUD_TEST_DATABASE_URL"

#: 负 TTL = 「一出生就已过期」（DB 侧 `now() - N` 计算，方言无关）。
_EXPIRED_TTL_SECONDS = -10


def _pg_url() -> str | None:
    return os.environ.get(_PG_URL_ENV)


def _factory(engine, tenant_id: int | None = TENANT_LOCAL):
    """默认绑定本土租户：PG 上 RLS 已启用，未 SET LOCAL ⇒ fail-closed（读写皆空）。"""
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        tenant_id=tenant_id)


def _seed(engine) -> tuple[int, int, int]:
    """user / channel / agent / connection → (user_id, channel_id, connection_id)。"""
    with _factory(engine)() as uow:
        user = uow.users.create(User(name="arm-admin"))
        channel = uow.channels.create(Channel(name="arm-ch", type="local"))
        agent = uow.agents.create(Agent(name="arm-agent", type="local"))
        assert user.id and channel.id and agent.id
        connection = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="arm-path"))
        assert connection.id
        uow.commit()
        return user.id, channel.id, connection.id


def _arm(engine, channel_id: int, connection_id: int, initiator: int,
         ttl_seconds: int = 600) -> BootstrapArm:
    with _factory(engine)() as uow:
        arm = uow.bootstrap_arms.arm(
            BootstrapArm(channel_id=channel_id, connection_id=connection_id,
                         initiator_user_id=initiator),
            ttl_seconds=ttl_seconds)
        uow.commit()
    return arm


def _count_armed(engine, channel_id: int, connection_id: int) -> int:
    with _factory(engine)() as uow:
        rows = uow.session.execute(
            text("SELECT count(*) FROM bootstrap_arms "
                 "WHERE channel_id = :c AND connection_id = :k AND status = 'armed'"),
            {"c": channel_id, "k": connection_id},
        ).scalar_one()
    return int(rows)


# ==================================================================== A. 生命周期


def test_arm_creates_active_arm_with_db_generated_expiry(db_engine) -> None:
    user_id, channel_id, connection_id = _seed(db_engine)
    arm = _arm(db_engine, channel_id, connection_id, user_id)

    assert arm.id is not None
    assert arm.status == STATUS_ARMED
    # expires_at 由 DB 生成并回显（app 不计算）
    assert arm.expires_at is not None

    with _factory(db_engine)() as uow:
        active = uow.bootstrap_arms.find_active_by_channel(channel_id)
    assert active is not None and active.id == arm.id
    assert active.connection_id == connection_id


def test_arm_is_scoped_to_tenant_channel_connection(db_engine) -> None:
    """scope 错配不得命中：另一条 Connection 的 Arm 不属于本查询。"""
    user_id, channel_id, connection_id = _seed(db_engine)
    _arm(db_engine, channel_id, connection_id, user_id)

    with _factory(db_engine)() as uow:
        agent = uow.agents.create(Agent(name="arm-agent-2", type="local"))
        assert agent.id
        other = uow.connections.create(Connection(
            channel_id=channel_id, agent_id=agent.id, name="arm-path-2"))
        assert other.id
        uow.commit()
        other_id = other.id

    assert _count_armed(db_engine, channel_id, other_id) == 0
    assert _count_armed(db_engine, channel_id, connection_id) == 1


# ==================================================================== B. supersede


def test_supersede_revokes_old_and_creates_new(db_engine) -> None:
    """重复 Verify = supersede：旧 armed → revoked，新 armed；至多一条 armed。"""
    user_id, channel_id, connection_id = _seed(db_engine)
    first = _arm(db_engine, channel_id, connection_id, user_id)
    second = _arm(db_engine, channel_id, connection_id, user_id)

    assert second.id != first.id
    assert _count_armed(db_engine, channel_id, connection_id) == 1

    with _factory(db_engine)() as uow:
        old = uow.bootstrap_arms.get_by_id(first.id)
        active = uow.bootstrap_arms.find_active_by_channel(channel_id)
    assert old is not None and old.status == STATUS_REVOKED     # 终态
    assert active is not None and active.id == second.id


def test_supersede_after_expiry_still_works(db_engine) -> None:
    """已过期但 `status='armed'` 的旧行**必须**能被 supersede。

    ⚠️ 这是 O-1 的易错点：部分唯一索引覆盖 `status='armed'`（与过期无关），
    若 revoke 只过滤未过期行，过期旧行会让新 Arm 永久撞约束。
    """
    user_id, channel_id, connection_id = _seed(db_engine)
    expired = _arm(db_engine, channel_id, connection_id, user_id,
                   ttl_seconds=_EXPIRED_TTL_SECONDS)
    fresh = _arm(db_engine, channel_id, connection_id, user_id)

    assert fresh.id != expired.id
    assert _count_armed(db_engine, channel_id, connection_id) == 1


# ==================================================================== C. 过期（逻辑）


def test_expired_arm_is_not_active_and_not_consumable(db_engine) -> None:
    user_id, channel_id, connection_id = _seed(db_engine)
    arm = _arm(db_engine, channel_id, connection_id, user_id,
               ttl_seconds=_EXPIRED_TTL_SECONDS)
    assert arm.id is not None

    with _factory(db_engine)() as uow:
        assert uow.bootstrap_arms.find_active_by_channel(channel_id) is None
        consumed = uow.bootstrap_arms.consume(
            arm.id, interaction_id=1, external_identity_id="zhangsan",
            external_conversation_id="zhangsan")

    assert consumed is False
    # 过期**不写状态**：仍是 armed（EXPIRED 不是状态值），且无任何消费痕迹
    with _factory(db_engine)() as uow:
        row = uow.bootstrap_arms.get_by_id(arm.id)
    assert row is not None and row.status == STATUS_ARMED
    assert row.consumed_at is None and row.consumed_by_interaction_id is None


# ==================================================================== D. CAS 消费


def test_consume_is_cas_and_writes_trace(db_engine) -> None:
    user_id, channel_id, connection_id = _seed(db_engine)
    arm = _arm(db_engine, channel_id, connection_id, user_id)
    assert arm.id is not None

    with _factory(db_engine)() as uow:
        winner = uow.bootstrap_arms.consume(
            arm.id, interaction_id=42, external_identity_id="zhangsan",
            external_conversation_id="conv-1")
        uow.commit()
    assert winner is True

    # 第二次消费（CAS 输）→ False，且**不改写**赢家的追溯字段
    with _factory(db_engine)() as uow:
        loser = uow.bootstrap_arms.consume(
            arm.id, interaction_id=43, external_identity_id="lisi",
            external_conversation_id="conv-2")
    assert loser is False

    with _factory(db_engine)() as uow:
        row = uow.bootstrap_arms.get_by_id(arm.id)
    assert row is not None and row.status == STATUS_CONSUMED
    assert row.consumed_by_interaction_id == 42
    assert row.consumed_external_identity_id == "zhangsan"
    assert row.consumed_external_conversation_id == "conv-1"


def test_consumed_arm_is_no_longer_active(db_engine) -> None:
    user_id, channel_id, connection_id = _seed(db_engine)
    arm = _arm(db_engine, channel_id, connection_id, user_id)
    assert arm.id is not None
    with _factory(db_engine)() as uow:
        uow.bootstrap_arms.consume(
            arm.id, interaction_id=7, external_identity_id="zhangsan",
            external_conversation_id="zhangsan")
        uow.commit()
    with _factory(db_engine)() as uow:
        assert uow.bootstrap_arms.find_active_by_channel(channel_id) is None


# ==================================================================== E. G-I（部分唯一索引）


def test_g_i_partial_unique_index_shape_is_tenant_leading() -> None:
    """G-I 静态面：唯一索引存在、tenant 前导、两种方言同语义。"""
    indexes = {ix.name: ix for ix in BootstrapArmModel.__table__.indexes}
    index = indexes.get("uq_bootstrap_arms_scope_armed")

    assert index is not None, "缺少 O-1 的部分唯一索引"
    assert index.unique is True
    assert [c.name for c in index.columns] == [
        "tenant_id", "channel_id", "connection_id"]
    # 两种方言必须表达同一语义（CP3-G §5）
    assert index.dialect_kwargs.get("postgresql_where") is not None
    assert index.dialect_kwargs.get("sqlite_where") is not None


def test_g_i_second_armed_same_scope_fails_sqlite(db_engine) -> None:
    """G-I · SQLite：直接插入第二条同 scope 的 ARMED 必须失败（DB 权威）。"""
    user_id, channel_id, connection_id = _seed(db_engine)
    _arm(db_engine, channel_id, connection_id, user_id)

    with _factory(db_engine)() as uow:
        uow.session.add(BootstrapArmModel(
            tenant_id=1, channel_id=channel_id, connection_id=connection_id,
            status=STATUS_ARMED, initiator_user_id=user_id,
            expires_at=datetime.now(timezone.utc),
        ))
        with pytest.raises(IntegrityError):
            uow.session.flush()


def test_g_i_revoked_then_new_armed_succeeds(db_engine) -> None:
    """G-I：REVOKED 旧行**不阻止**新 ARMED 插入（supersede 可用）。"""
    user_id, channel_id, connection_id = _seed(db_engine)
    _arm(db_engine, channel_id, connection_id, user_id)
    _arm(db_engine, channel_id, connection_id, user_id)   # supersede
    assert _count_armed(db_engine, channel_id, connection_id) == 1


pytestmark_pg = pytest.mark.skipif(
    not _pg_url(),
    reason=f"需要 {_PG_URL_ENV} 指向可重置的 PostgreSQL（Cloud profile）",
)


def _pg_reset_schema(url: str) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def _pg_alembic_upgrade(url: str) -> None:
    """真实跑 `alembic upgrade head`（G1：0008 必须真的能执行）。"""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(cfg, "head")
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev


@pytest.fixture()
def pg_engine():
    """PG：public schema 归零 → `alembic upgrade head` → 绑定 engine。"""
    url = _pg_url()
    _pg_reset_schema(url)
    _pg_alembic_upgrade(url)
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytestmark_pg
def test_g1_migration_0008_creates_table_and_partial_index_postgres(
    pg_engine,
) -> None:
    """G1：0008 在 PostgreSQL 上真实落地 —— 表 + **部分唯一索引**（含 WHERE 子句）。"""
    assert "bootstrap_arms" in inspect(pg_engine).get_table_names()

    with pg_engine.connect() as conn:
        definition = conn.execute(text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'bootstrap_arms' AND indexname = :name"
        ), {"name": "uq_bootstrap_arms_scope_armed"}).scalar_one_or_none()

    assert definition is not None, "0008 未创建 O-1 的部分唯一索引"
    assert "UNIQUE INDEX" in definition.upper()
    assert definition.index("tenant_id") < definition.index("channel_id") \
        < definition.index("connection_id")            # tenant 前导
    # 部分性必须由 DB 表达（不是应用层约定）
    assert "WHERE" in definition.upper() and "armed" in definition

    # RLS 已启用（G3 的前置；策略细节由下一个用例验证）
    with pg_engine.connect() as conn:
        relrowsecurity, relforcerowsecurity = conn.execute(text(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'bootstrap_arms'"
        )).one()
    assert relrowsecurity is True and relforcerowsecurity is True


@pytestmark_pg
def test_g_i_second_armed_same_scope_fails_postgres(pg_engine) -> None:
    """G-I · PostgreSQL：同一 scope 第二条 ARMED 由 DB 拒绝（方言 parity）。"""
    user_id, channel_id, connection_id = _seed(pg_engine)
    _arm(pg_engine, channel_id, connection_id, user_id)

    with _factory(pg_engine)() as uow:
        uow.session.add(BootstrapArmModel(
            tenant_id=TENANT_LOCAL, channel_id=channel_id,
            connection_id=connection_id, status=STATUS_ARMED,
            initiator_user_id=user_id,
            expires_at=datetime.now(timezone.utc),
        ))
        with pytest.raises(IntegrityError):
            uow.session.flush()


@pytestmark_pg
def test_g_i_revoked_then_new_armed_succeeds_postgres(pg_engine) -> None:
    """G-I · PostgreSQL：REVOKED 旧行不阻止新 ARMED（supersede 可用）。"""
    user_id, channel_id, connection_id = _seed(pg_engine)
    first = _arm(pg_engine, channel_id, connection_id, user_id)
    second = _arm(pg_engine, channel_id, connection_id, user_id)

    assert first.id != second.id
    assert _count_armed(pg_engine, channel_id, connection_id) == 1
    with _factory(pg_engine)() as uow:
        old = uow.bootstrap_arms.get_by_id(first.id)
    assert old is not None and old.status == STATUS_REVOKED


def _dsn_with_role(url: str, role: str, password: str) -> str:
    head, _, tail = url.partition("://")
    _, at, host = tail.partition("@")
    return f"{head}://{role}:{password}{at}{host}"


_RLS_ROLE = "cp3g_rls_app"
_RLS_PW = "cp3g-rls-pw"


def _make_app_role(admin_engine) -> None:
    """创建/复用**非 superuser** 应用角色（生产对应 `acp_app`）。

    证据纪律（与 `tools/closure_evidence.py` Evidence C 同法）：
    `POSTGRES_USER` 是 **superuser**，会**绕过 RLS**；因此 RLS 断言必须在
    `rolsuper=false` + `rolbypassrls=false` 的角色下执行，否则测的是空气。
    """
    with admin_engine.begin() as conn:
        conn.execute(text(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles "
            f"WHERE rolname='{_RLS_ROLE}') THEN "
            f"CREATE ROLE {_RLS_ROLE} LOGIN PASSWORD '{_RLS_PW}' "
            f"NOSUPERUSER NOBYPASSRLS; END IF; END $$;"))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {_RLS_ROLE}"))
        conn.execute(text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
            f"IN SCHEMA public TO {_RLS_ROLE}"))
        conn.execute(text(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {_RLS_ROLE}"))


@pytestmark_pg
def test_g3_rls_isolation_and_fail_closed_postgres(pg_engine) -> None:
    """G3：RLS 第二层 —— 只放行本租户；未设 `app.tenant_id` 一律 fail-closed。

    必须以**非 superuser** 角色断言（superuser 绕过 RLS）。
    """
    user_id, channel_id, connection_id = _seed(pg_engine)      # 迁移/seed 用 admin 角色
    _arm(pg_engine, channel_id, connection_id, user_id)
    _make_app_role(pg_engine)

    engine = create_engine(_dsn_with_role(_pg_url(), _RLS_ROLE, _RLS_PW))
    try:
        with engine.connect() as conn:
            rolsuper, rolbypass = conn.execute(text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user")).one()
        assert rolsuper is False and rolbypass is False, \
            "RLS 证据必须在非 superuser / 非 bypassrls 角色下采集"

        def _raw_count(tenant_id: int | None) -> int:
            with _factory(engine, tenant_id)() as uow:
                return int(uow.session.execute(
                    text("SELECT count(*) FROM bootstrap_arms")).scalar_one())

        assert _raw_count(TENANT_LOCAL) == 1        # 本租户可见
        assert _raw_count(TENANT_LOCAL + 1) == 0    # 其它租户不可见
        assert _raw_count(None) == 0                # 未 SET LOCAL ⇒ fail-closed

        # Repository 层显式过滤与 RLS 同向（双层防线）
        with _factory(engine, TENANT_LOCAL + 1)() as uow:
            assert uow.bootstrap_arms.find_active_by_channel(channel_id) is None
    finally:
        engine.dispose()


@pytestmark_pg
def test_expired_and_consume_semantics_parity_postgres(pg_engine) -> None:
    """G7/G11 · PG 上过期谓词与 CAS 语义与 SQLite 一致（DB 时钟）。"""
    user_id, channel_id, connection_id = _seed(pg_engine)

    expired = _arm(pg_engine, channel_id, connection_id, user_id,
                   ttl_seconds=_EXPIRED_TTL_SECONDS)
    assert expired.id is not None
    with _factory(pg_engine)() as uow:
        assert uow.bootstrap_arms.find_active_by_channel(channel_id) is None
        assert uow.bootstrap_arms.consume(
            expired.id, interaction_id=1, external_identity_id="zhangsan",
            external_conversation_id="zhangsan") is False

    fresh = _arm(pg_engine, channel_id, connection_id, user_id)
    assert fresh.id is not None
    with _factory(pg_engine)() as uow:
        assert uow.bootstrap_arms.consume(
            fresh.id, interaction_id=2, external_identity_id="zhangsan",
            external_conversation_id="conv-1") is True
        uow.commit()
    with _factory(pg_engine)() as uow:
        assert uow.bootstrap_arms.consume(
            fresh.id, interaction_id=3, external_identity_id="lisi",
            external_conversation_id="conv-2") is False      # CAS loser
        row = uow.bootstrap_arms.get_by_id(fresh.id)
    assert row is not None and row.status == STATUS_CONSUMED
    assert row.consumed_by_interaction_id == 2


# ==================================================================== F. 审计字段


def test_initiator_is_persisted_and_never_null(db_engine) -> None:
    """O-4：`initiator_user_id` NOT NULL —— Arm 只在有真实操作者时存在。"""
    user_id, channel_id, connection_id = _seed(db_engine)
    arm = _arm(db_engine, channel_id, connection_id, user_id)
    assert arm.id is not None

    with _factory(db_engine)() as uow:
        row = uow.bootstrap_arms.get_by_id(arm.id)
        with pytest.raises(IntegrityError):
            uow.session.add(BootstrapArmModel(
                tenant_id=1, channel_id=channel_id, connection_id=connection_id,
                status=STATUS_ARMED,
                expires_at=datetime.now(timezone.utc),
            ))
            uow.session.flush()

    assert row is not None and row.initiator_user_id == user_id
