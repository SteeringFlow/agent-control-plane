"""Package 2 / Step 1 — Authorization Persistence（授权状态持久化边界）。

范围**严格限定**为持久化层（**不含** Authorization 运行时执行器 / Coordinator /
Router / API / 前端）。
上游冻结：Package 2-C §7.2/§7.3/§7.4（DDL / RLS / migration 构成）、
          Package 2-C §5.2（回填 SQL）、Step 0 §E（变更边界）。

本文件分两段：

  A. **默认运行（SQLite）**：schema 结构 / domain / mapper / repository 边界 /
     四元组 UNIQUE / 复合 FK 的两种错配 / Connection 生命周期（阻断式 + 不碰授权）/
     租户作用域第一层防线（Repository 过滤）。
  B. **PG-gated**：migration 0007 的 DDL 事实 / RLS（ENABLE+FORCE+NULLIF 策略）/
     历史 Session 回填（DISTINCT、无 status 过滤、多 Connection、inactive 保留、
     重复收敛、不合格行跳过）/ 空库 → 0 授权。

判据（Package 2-C 冻结）：
- 授权作用域 = Conversation（channel_id + external_conversation_id），
  **不是** Identity / User；
- deny **不落库**（缺行 = deny）；
- Connection 删除**阻断式**（NO ACTION，不 cascade）；
- 同租户跨 channel 错配由**复合 FK**结构性防止（RLS 盖不住）。
"""

import os
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.persistence.mappers.connection_conversation import to_domain, to_orm
from app.persistence.models import ConnectionConversationModel, ConnectionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.connection_conversation import (
    ConnectionConversationRepository,
)

BACKEND = Path(__file__).resolve().parents[1]

TABLE = "connection_conversations"


# ------------------------------------------------------------------ helpers


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _uow(db_engine, tenant_id: int | None = None) -> SqlAlchemyUnitOfWork:
    return SqlAlchemyUnitOfWork(
        session_factory=_factory(db_engine), tenant_id=tenant_id)


def _seed_path(db_engine, *, suffix: str = "a", tenant_id: int | None = None):
    """建 channel + agent + active connection（可选租户作用域）。"""
    with _uow(db_engine, tenant_id) as uow:
        channel = uow.channels.create(Channel(name=f"ch-{suffix}", type="wecom"))
        agent = uow.agents.create(Agent(name=f"ag-{suffix}", type="acp"))
        connection = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path", status="active"))
        uow.commit()
        return channel, agent, connection


# ================================================================== A. SQLite


# ---------------------------------------------------------------- A1 Schema


def test_a1_table_exists_with_frozen_columns() -> None:
    """列清单必须**逐字**等于 Package 2-C §7.2 冻结的 DDL 顺序。"""
    t = ConnectionConversationModel.__table__
    assert t.name == TABLE
    assert [c.name for c in t.columns] == [
        "id", "tenant_id", "channel_id", "external_conversation_id",
        "connection_id", "created_at", "updated_at",
    ]


def test_a1_nullability_matches_contract() -> None:
    """四列语义列 + 时间戳全部 NOT NULL（空串由应用层校验，DB 不含 CHECK）。"""
    cols = ConnectionConversationModel.__table__.columns
    for name in ("tenant_id", "channel_id", "external_conversation_id",
                 "connection_id", "created_at", "updated_at"):
        assert cols[name].nullable is False, f"{name} 必须 NOT NULL"


def test_a1_primary_key_and_unique() -> None:
    t = ConnectionConversationModel.__table__
    assert [c.name for c in t.primary_key.columns] == ["id"]

    uqs = [c for c in t.constraints
           if c.name == "uq_connection_conversations_tenant_channel_conv_conn"]
    assert uqs, "缺少四元组 UNIQUE"
    assert {c.name for c in uqs[0].columns} == {
        "tenant_id", "channel_id", "external_conversation_id", "connection_id"}


def test_a1_composite_fk_targets_connections_three_columns() -> None:
    """复合 FK： (tenant_id, channel_id, connection_id) → connections(t,c,id)。"""
    t = ConnectionConversationModel.__table__
    composite = [
        fkc for fkc in t.foreign_key_constraints
        if fkc.name == "fk_connection_conversations_tenant_channel_connection"
    ]
    assert len(composite) == 1, "缺少复合 FK"
    fkc = composite[0]
    assert [e.parent.name for e in fkc.elements] == [
        "tenant_id", "channel_id", "connection_id"]
    assert [e.column.table.name for e in fkc.elements] == [
        "connections", "connections", "connections"]
    assert [e.column.name for e in fkc.elements] == ["tenant_id", "channel_id", "id"]


def test_a1_fk_delete_rule_is_no_action_not_cascade() -> None:
    """Connection 删除 = **阻断式**（无 ondelete 子句）；不得 cascade / SET NULL。"""
    t = ConnectionConversationModel.__table__
    for fkc in t.foreign_key_constraints:
        assert fkc.ondelete is None, f"{fkc.name} 引入了 ondelete（隐式解绑被禁止）"


def test_a1_indexes_are_minimal() -> None:
    """恰好两个索引：tenant_id（RLS 纪律）+ connection_id（List 读路径）。

    (tenant_id, channel_id, external_conversation_id) 由四元组 UNIQUE 的**前导列**
    覆盖，因此**不得**重复建该组合索引。
    """
    names = {ix.name for ix in ConnectionConversationModel.__table__.indexes}
    assert names == {
        f"ix_{TABLE}_tenant_id", f"ix_{TABLE}_connection_id"}, names


def test_a1_no_acl_columns() -> None:
    """ADR-9 / ADR-1 / ADR-3 红线：无 effect/priority/status，无 identity_id/user_id。"""
    names = {c.name for c in ConnectionConversationModel.__table__.columns}
    for banned in ("status", "effect", "priority", "expires_at", "conditions",
                   "identity_id", "user_id", "role", "permission", "policy"):
        assert banned not in names, f"出现禁列：{banned}"


def test_a1_connections_supporting_unique_exists() -> None:
    """supporting UNIQUE(tenant_id, channel_id, id)：纯结构性，**不是**业务唯一性。"""
    uqs = [c for c in ConnectionModel.__table__.constraints
           if c.name == "uq_connections_tenant_channel_id"]
    assert uqs, "connections 缺少 supporting UNIQUE"
    assert {c.name for c in uqs[0].columns} == {"tenant_id", "channel_id", "id"}

    # 业务唯一性仍在（未被取代）
    business = [c for c in ConnectionModel.__table__.constraints
                if c.name == "uq_connections_tenant_channel_agent_id"]
    assert business, "connections 业务唯一约束不得被移除"


# ---------------------------------------------------------------- A2 Domain / Mapper


def test_a2_domain_entity_shape() -> None:
    assert [f.name for f in fields(ConnectionConversation)] == [
        "channel_id", "external_conversation_id", "connection_id", "id",
        "created_at", "updated_at",
    ]
    entity = ConnectionConversation(
        channel_id=1, external_conversation_id="G1", connection_id=7)
    assert entity.id is None
    with pytest.raises(FrozenInstanceError):
        entity.connection_id = 9  # type: ignore[misc]


def test_a2_domain_carries_no_tenant_id() -> None:
    """与其余领域实体一致：租户归属由 Persistence 层自有列承载，不经 Connection 推导。"""
    assert "tenant_id" not in {f.name for f in fields(ConnectionConversation)}


def test_a2_mapper_roundtrip() -> None:
    entity = ConnectionConversation(
        channel_id=3, external_conversation_id="G1", connection_id=9)
    row = to_orm(entity)
    assert row.channel_id == 3
    assert row.external_conversation_id == "G1"
    assert row.connection_id == 9
    # 写路径不携带 id / 时间戳（由 DB 生成）
    assert row.id is None


# ---------------------------------------------------------------- A3 Repository boundary


def test_a3_port_surface_is_minimal() -> None:
    """Step 1 只落 3 个方法；update 仍然延后（无消费者不扩张）。

    **Step 2 修订**：`find_granted_connection_ids`（消费者 = 执行器）。
    **Step 3 修订**：`delete` / `list_by_connection`（消费者 = Grant Management
    API，B4.2 的 Delete / List）。放宽的是「方法数量」，不是「最小化纪律」：
    **update 依旧不存在**（行不可变，改授权 = 删 + 建）。
    **M6.6-C3 / ADR-12 修订**：`find_or_create`（消费者 = Bootstrap 事务，§11 的
    幂等建立；命中复用，冲突以 SAVEPOINT 消化后回读）。同样是「方法数量」，
    不是纪律放宽。
    """
    methods = {n for n in dir(ConnectionConversationRepository) if not n.startswith("_")}
    assert methods == {"create", "get_by_id", "find_by_binding",
                       "find_granted_connection_ids", "delete",
                       "list_by_connection", "find_or_create"}, methods
    assert "update" not in methods


def test_a3_uow_exposes_authorization_repository(db_engine) -> None:
    with _uow(db_engine) as uow:
        assert uow.connection_conversations is not None
    # 退出后引用被清空（与既有纪律一致）
    assert uow.connection_conversations is None


def test_a3_create_and_read_back(db_engine) -> None:
    channel, _agent, connection = _seed_path(db_engine)

    with _uow(db_engine) as uow:
        created = uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="G1",
            connection_id=connection.id))
        uow.commit()

    assert created.id is not None
    assert created.created_at is not None      # DB 生成的时间戳回显

    with _uow(db_engine) as uow:
        assert uow.connection_conversations.get_by_id(created.id) == created
        assert uow.connection_conversations.find_by_binding(
            channel.id, "G1", connection.id) == created
        assert uow.connection_conversations.find_by_binding(
            channel.id, "G2", connection.id) is None


def test_a3_duplicate_binding_rejected_by_unique(db_engine) -> None:
    """四元组 UNIQUE：同一 conversation 对同一 connection 只有一行。"""
    channel, _agent, connection = _seed_path(db_engine)

    with _uow(db_engine) as uow:
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="G1",
            connection_id=connection.id))
        uow.commit()

    with pytest.raises(IntegrityError):
        with _uow(db_engine) as uow:
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=channel.id, external_conversation_id="G1",
                connection_id=connection.id))
            uow.commit()


def test_a3_tenant_scope_is_first_line_of_defence(db_engine) -> None:
    """tenant A 写入的授权，tenant B 作用域**读不到**（Repository 显式过滤）。"""
    channel, _agent, connection = _seed_path(db_engine, tenant_id=1)

    with _uow(db_engine, tenant_id=1) as uow:
        created = uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="G1",
            connection_id=connection.id))
        uow.commit()

    with _uow(db_engine, tenant_id=2) as uow:
        assert uow.connection_conversations.get_by_id(created.id) is None
        assert uow.connection_conversations.find_by_binding(
            channel.id, "G1", connection.id) is None


# ---------------------------------------------------------------- A4 复合 FK：两种错配


def test_a4_same_tenant_wrong_channel_is_rejected(db_engine) -> None:
    """**同租户跨 channel 错配** —— RLS 盖不住，必须由复合 FK 拒绝。"""
    channel_a, _agent_a, connection_a = _seed_path(db_engine, suffix="a")
    channel_b, _agent_b, _connection_b = _seed_path(db_engine, suffix="b")

    with pytest.raises(IntegrityError):
        with _uow(db_engine) as uow:
            # 会话键取 channel_b，却指向 channel_a 上的 Connection
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=channel_b.id, external_conversation_id="G1",
                connection_id=connection_a.id))
            uow.commit()


def test_a4_cross_tenant_mismatch_is_rejected(db_engine) -> None:
    """**跨租户错配**：另一个租户的授权不得指向 tenant 1 的 Connection。"""
    with _uow(db_engine) as uow:
        tenant_b = uow.tenants.create(name="t2")
        uow.commit()
    assert tenant_b != 1

    channel, _agent, connection = _seed_path(db_engine, suffix="t1", tenant_id=1)

    with pytest.raises(IntegrityError):
        with _uow(db_engine, tenant_id=tenant_b) as uow:
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=channel.id, external_conversation_id="G1",
                connection_id=connection.id))
            uow.commit()


# ---------------------------------------------------------------- A5 Connection 生命周期


def test_a5_grant_is_not_touched_by_connection_status_change(db_engine) -> None:
    """Connection 软下线 → 授权行**一行都不动**（不引入隐式授权变化）。

    语义结果（Step 2 才实现）：granted 非空但 active ∩ granted = ∅ → ROUTING_FAILED，
    **不是** UNAUTHORIZED。本测试只证明授权事实未被改写。
    """
    channel, _agent, connection = _seed_path(db_engine)

    with _uow(db_engine) as uow:
        created = uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="G1",
            connection_id=connection.id))
        uow.commit()

    with _uow(db_engine) as uow:
        uow.connections.update(replace(connection, status="inactive"))
        uow.commit()

    with _uow(db_engine) as uow:
        still = uow.connection_conversations.find_by_binding(
            channel.id, "G1", connection.id)
        assert still is not None and still.id == created.id


def test_a5_deleting_referenced_connection_is_blocked(db_engine) -> None:
    """阻断式：删除被授权引用的 Connection → IntegrityError（NO ACTION，不 cascade）。"""
    channel, _agent, connection = _seed_path(db_engine)

    with _uow(db_engine) as uow:
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="G1",
            connection_id=connection.id))
        uow.commit()

    with Session(db_engine) as session:
        with pytest.raises(IntegrityError):
            session.execute(
                delete(ConnectionModel).where(ConnectionModel.id == connection.id))
            session.flush()
        session.rollback()

    with _uow(db_engine) as uow:
        assert uow.connections.get_by_id(connection.id) is not None
        assert uow.connection_conversations.find_by_binding(
            channel.id, "G1", connection.id) is not None


def test_a5_empty_database_has_no_grants(db_engine) -> None:
    """空库 = 无授权 = 全 DENY（ADR-6：缺授权记录 = 不允许）。"""
    with Session(db_engine) as session:
        assert session.query(ConnectionConversationModel).count() == 0


def test_a5_group_grant_is_conversation_scoped_not_identity_scoped(db_engine) -> None:
    """ADR-3 语义锚点：一条授权覆盖「群」——群内不同成员**共用**它。

    授权行里没有 identity 维度，因此「A 已授权、B 未授权」这种状态在本表中
    **不可表达**（这正是 ADR-3 要的：`G1 → C1`，不是逐人授权）。
    """
    channel, _agent, connection = _seed_path(db_engine)

    with _uow(db_engine) as uow:
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="CHAT-G1",
            connection_id=connection.id))
        uow.commit()

    # 两个不同成员（不同 Identity）在实现层会解析出不同 identity_id，
    # 但授权查询只按 (channel, conversation) → 命中的是同一行。
    with _uow(db_engine) as uow:
        found = uow.connection_conversations.find_by_binding(
            channel.id, "CHAT-G1", connection.id)
        assert found is not None
        assert not hasattr(found, "identity_id")
        assert not hasattr(found, "user_id")


# ================================================================== B. PG-gated


_PG = pytest.mark.skipif(
    not os.environ.get("CLOUD_TEST_DATABASE_URL"),
    reason="需要 CLOUD_TEST_DATABASE_URL 指定可用的 PostgreSQL（Cloud profile）",
)


def _pg_url() -> str:
    return os.environ["CLOUD_TEST_DATABASE_URL"]


def _reset_schema(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def _upgrade(url: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(cfg, revision)
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev


def _seed_historical_sessions(url: str) -> None:
    """在 0006 上播历史 Session（superuser 连接 → RLS 不适用）。

    覆盖 6 种情形（Package 2-C §B5.3 的边界）：
      1) 同一 (channel, conversation, connection) 多行（群内多成员）→ 应收敛为 1
      2) 第二个 Connection（inactive）→ 应保留
      3) connection_id NULL → 跳过
      4) external_conversation_id 空串 → 跳过
      5) external_conversation_id NULL → 跳过
    """
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id, name, status, tenant_id) VALUES (1,'u','active',1)"))
        conn.execute(text(
            "INSERT INTO channels (id, name, type, status, tenant_id) "
            "VALUES (1,'ch','wecom','active',1)"))
        conn.execute(text(
            "INSERT INTO agents (id, name, type, status, tenant_id) "
            "VALUES (1,'a1','local','active',1)"))
        conn.execute(text(
            "INSERT INTO agents (id, name, type, status, tenant_id) "
            "VALUES (2,'a2','local','active',1)"))
        conn.execute(text(
            "INSERT INTO connections (id, channel_id, agent_id, name, status, tenant_id) "
            "VALUES (1,1,1,'c1','active',1)"))
        conn.execute(text(
            "INSERT INTO connections (id, channel_id, agent_id, name, status, tenant_id) "
            "VALUES (2,1,2,'c2','inactive',1)"))
        conn.execute(text(
            "INSERT INTO identities (id, user_id, channel_id, external_id, status, tenant_id) "
            "VALUES (1,1,1,'ext-a','active',1)"))
        conn.execute(text(
            "INSERT INTO identities (id, user_id, channel_id, external_id, status, tenant_id) "
            "VALUES (2,1,1,'ext-b','active',1)"))

        def session(identity_id, connection_id, conversation):
            conn.execute(text(
                "INSERT INTO sessions "
                "(identity_id, channel_id, connection_id, external_conversation_id, "
                " status, tenant_id) "
                "VALUES (:i, 1, :c, :conv, 'active', 1)"),
                {"i": identity_id, "c": connection_id, "conv": conversation})

        session(1, 1, "CHAT-G1")     # ① 与下一行同三元组 → DISTINCT 收敛
        session(2, 1, "CHAT-G1")     # ① 群内第二个成员
        session(1, 2, "CHAT-G2")     # ② inactive Connection 仍回填
        session(1, None, "CHAT-G3")  # ③ connection_id NULL → 跳过
        session(1, 1, "")            # ④ 空串 → 跳过
        session(1, 1, None)          # ⑤ NULL → 跳过
    engine.dispose()


@_PG
def test_b1_migration_chain_contains_0007_and_head_is_chain_tip() -> None:
    """0007 仍在迁移链上；head 随新增迁移推进，且必须是**链末端**的一环。

    本守卫的语义是「0007 存在且链 head 可解析、且 head 就是迁移链的末端」，
    不是「head 永远是某个字面值」——因此后续新增迁移（0008 / 0009 / …）**无需**
    改动本用例；只有「0007 掉链」「head 不可解析」「head 不是链末端」才会失败。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_revision("0007") is not None, "0007 必须在迁移链上"
    head = script.get_current_head()
    assert script.get_revision(head) is not None
    # head 必须是迁移链的末端（链推进时自动跟随，**不写死字面值**）
    # 注意：alembic 的 walk_revisions 产出顺序是 **head → base**（逆时间序）。
    chain = [r.revision for r in script.walk_revisions()]
    assert chain[0] == head, f"head 必须是迁移链末端：{head} vs {chain}"
    assert "0007" in chain, "0007 必须在 head 的迁移链上（未被摘链）"


@_PG
def test_b2_ddl_after_upgrade_head() -> None:
    url = _pg_url()
    _reset_schema(url)
    _upgrade(url, "0007")
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            nullable = dict(conn.execute(text(
                "select column_name, is_nullable from information_schema.columns "
                "where table_name = 'connection_conversations'")).all())
            for col in ("tenant_id", "channel_id", "external_conversation_id",
                        "connection_id"):
                assert nullable[col] == "NO", f"{col} 必须是 NOT NULL"

            # 复合 FK 的 delete rule = 'a'（NO ACTION），不是 'n'(SET NULL)/'c'(CASCADE)
            rule, ddl = conn.execute(text(
                "select con.confdeltype, pg_get_constraintdef(con.oid) "
                "from pg_constraint con "
                "where con.conrelid = 'connection_conversations'::regclass "
                "  and con.conname = 'fk_connection_conversations_tenant_channel_connection'"
            )).one()
            assert rule == "a", f"delete rule={rule}（必须是 NO ACTION）"
            assert "connections(tenant_id, channel_id, id)" in ddl, ddl

            # supporting UNIQUE 已存在于 connections
            assert conn.execute(text(
                "select count(*) from pg_constraint "
                "where conname = 'uq_connections_tenant_channel_id'")).scalar_one() == 1
    finally:
        engine.dispose()


@_PG
def test_b3_rls_enabled_forced_with_frozen_policy() -> None:
    url = _pg_url()
    _reset_schema(url)
    _upgrade(url, "0007")
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            rls, forced = conn.execute(text(
                "select relrowsecurity, relforcerowsecurity from pg_class "
                "where relname = 'connection_conversations'")).one()
            assert rls is True and forced is True

            expr = conn.execute(text(
                "select pg_get_expr(polqual, polrelid) from pg_policy "
                "where polrelid = 'connection_conversations'::regclass")).scalar_one()
            assert "NULLIF" in expr, f"必须用 0005 的 NULLIF 形态：{expr}"
            assert "app.tenant_id" in expr
    finally:
        engine.dispose()


@_PG
def test_b4_backfill_creates_grants_from_historical_sessions() -> None:
    url = _pg_url()
    _reset_schema(url)
    _upgrade(url, "0006")
    _seed_historical_sessions(url)
    _upgrade(url, "0007")

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            rows = conn.execute(text(
                "select channel_id, external_conversation_id, connection_id "
                "from connection_conversations order by connection_id, "
                "external_conversation_id")).all()
        assert rows == [
            (1, "CHAT-G1", 1),   # ① 两行 sessions 收敛为一条授权
            (1, "CHAT-G2", 2),   # ② inactive Connection 仍保留（不按 status 过滤）
        ], rows
    finally:
        engine.dispose()


@_PG
def test_b5_backfill_skips_ineligible_rows() -> None:
    url = _pg_url()
    _reset_schema(url)
    _upgrade(url, "0006")
    _seed_historical_sessions(url)
    _upgrade(url, "0007")

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            convs = {r[0] for r in conn.execute(text(
                "select external_conversation_id from connection_conversations")).all()}
        assert convs == {"CHAT-G1", "CHAT-G2"}, convs
        assert "CHAT-G3" not in convs
        assert "" not in convs
        assert None not in convs
    finally:
        engine.dispose()


@_PG
def test_b6_empty_database_backfills_nothing() -> None:
    """空库 → 0 授权 → 全 DENY（新部署必须显式 Create Grant）。"""
    url = _pg_url()
    _reset_schema(url)
    _upgrade(url, "0007")
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            assert conn.execute(text(
                "select count(*) from connection_conversations")).scalar_one() == 0
    finally:
        engine.dispose()
