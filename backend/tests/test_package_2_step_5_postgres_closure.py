"""Package 2 / Step 5 — PostgreSQL Closure（真实 PG 证据，env 门控）。

目标不是跑测试，而是把两条线**重新汇合**：

    ①「测试通过」        —— PG-gated pytest
    ②「Cloud V0 数据库证据通过」—— closure_evidence.py（同一 DSN、同一次运行）

本文件负责 ① 中真正需要 PostgreSQL 的部分（SQLite 无法表达）：

    Migration   0001→…→0007 单 head / linear / upgrade 成功 / downgrade 按登记限制
    Schema      connection_conversations 符合冻结 DDL（列 / 约束 / 索引 / 复合 FK）
    RLS         ENABLE + FORCE + 策略；tenant A ≠ tenant B（非超级用户角色实测）
    Integrity   wrong tenant / wrong channel / wrong connection / inactive / duplicate
    Backfill    0006 → seed sessions → 0007 → grants（含"绝不静默 0 行"）
    Runtime     user_id=NULL + valid grant → ALLOW；no grant → UNAUTHORIZED
    API         Create / List / Delete / known-conversations；known ≠ authorized

运行：

    CLOUD_TEST_DATABASE_URL=postgresql+psycopg://acp:acp@127.0.0.1:55432/acp \
        python -m pytest tests/test_package_2_step_5_postgres_closure.py -q

**安全前提**：pg_engine 会 DROP/CREATE public schema——必须指向**可重置的专用库**。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

BACKEND = Path(__file__).resolve().parents[1]

_PG_URL_ENV = "CLOUD_TEST_DATABASE_URL"
TABLE = "connection_conversations"


def _pg_url() -> str:
    return os.environ.get(_PG_URL_ENV, "").strip()


pytestmark = pytest.mark.skipif(
    not _pg_url(),
    reason=f"需要 {_PG_URL_ENV} 指向可重置的 PostgreSQL（Cloud profile）",
)


# ------------------------------------------------------------------ PG 工具


def _reset_schema(url: str) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def _alembic(to_revision: str, *, direction: str = "upgrade") -> None:
    """跑一次 migration（升级/降级），复用 Alembic 的 DATABASE_URL 约定。"""
    from alembic import command
    from alembic.config import Config

    url = _pg_url()
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        if direction == "upgrade":
            command.upgrade(cfg, to_revision)
        else:
            command.downgrade(cfg, to_revision)
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev


@pytest.fixture()
def pg_engine():
    """fresh PG：public 归零 → `alembic upgrade head` → 绑定 engine。"""
    url = _pg_url()
    _reset_schema(url)
    _alembic("head")
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


def _uow_factory(engine, tenant_id: int | None = None):
    from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        tenant_id=tenant_id,
    )


def _local_tenant() -> int:
    from app.core.tenant import TENANT_LOCAL

    return TENANT_LOCAL


def _seed_graph(engine, *, tenant_id: int | None = None,
                connection_status: str = "active",
                extra_inactive_connection: bool = False) -> dict:
    """最小图：user / channel / agent / connection(/channel2 可选)。"""
    from app.domain.agent import Agent
    from app.domain.channel import Channel
    from app.domain.connection import Connection
    from app.domain.identity import Identity
    from app.domain.user import User

    tid = _local_tenant() if tenant_id is None else tenant_id
    with _uow_factory(engine, tid)() as uow:
        user = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        channel_b = uow.channels.create(Channel(name="wecom-2", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        identity = uow.identities.create(Identity(
            user_id=user.id, channel_id=channel.id, external_id="wx_a"))
        conn = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status=connection_status))
        conn_inactive = conn_b = None
        if extra_inactive_connection:
            # 同 channel 的第二条连接必须换 agent：
            # UNIQUE(tenant, channel, agent) 是 Access Path 的 identity
            agent_idle = uow.agents.create(Agent(name="agent-idle", type="acp"))
            conn_inactive = uow.connections.create(Connection(
                channel_id=channel.id, agent_id=agent_idle.id, name="path-idle",
                status="inactive"))
        conn_b = uow.connections.create(Connection(
            channel_id=channel_b.id, agent_id=agent.id, name="path-b"))
        uow.commit()
        return {"tenant": tid, "user": user.id, "channel": channel.id,
                "channel_b": channel_b.id, "agent": agent.id,
                "identity": identity.id, "connection": conn.id,
                "connection_inactive": conn_inactive.id if conn_inactive else None,
                "connection_b": conn_b.id}


def _grant_rows(engine, tenant_id: int) -> list[tuple]:
    with engine.connect() as conn:
        return list(conn.execute(text(
            f"SELECT channel_id, external_conversation_id, connection_id "
            f"FROM {TABLE} WHERE tenant_id = :tid "
            f"ORDER BY channel_id, external_conversation_id, connection_id"),
            {"tid": tenant_id}))


# ================================================== 1. Migration topology


def test_migration_chain_is_linear_with_single_head() -> None:
    """单 head / linear：0001 → … → 0008，且无分支、无 merge。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    script = ScriptDirectory.from_config(cfg)

    # M6.6-C3 / ADR-12：0008（bootstrap_arms，非授权状态）+ 链仍单根线性
    heads = list(script.get_heads())
    assert heads == ["0008"], f"head 漂移：{heads}"

    chain: list[str] = []
    revision = script.get_revision("0008")
    while revision is not None:
        chain.append(revision.revision)
        downs = revision.down_revision
        if isinstance(downs, (list, tuple)):
            raise AssertionError(f"{revision.revision} 是 merge revision：{downs}")
        revision = script.get_revision(downs) if downs else None
    assert chain == ["0008", "0007", "0006", "0005", "0004", "0003", "0002", "0001"], chain


def test_upgrade_head_then_downgrade_0006_then_upgrade_again(pg_engine) -> None:
    """upgrade 成功；`downgrade 0006` 按**已登记的有损限制**丢弃全部授权行。"""
    g = _seed_graph(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(text(
            f"INSERT INTO {TABLE} (tenant_id, channel_id, external_conversation_id, "
            f"connection_id) VALUES (:t, :c, 'conv-lossy', :k)"),
            {"t": g["tenant"], "c": g["channel"], "k": g["connection"]})
    assert len(_grant_rows(pg_engine, g["tenant"])) == 1

    _alembic("0006", direction="downgrade")
    tables = set(inspect(pg_engine).get_table_names())
    assert TABLE not in tables, "downgrade 0006 未丢弃授权表"
    with pg_engine.connect() as conn:
        names = {c["name"] for c in inspect(pg_engine).get_unique_constraints("connections")}
    assert "uq_connections_tenant_channel_id" not in names

    _alembic("head")
    tables = set(inspect(pg_engine).get_table_names())
    assert TABLE in tables
    # 有损：该授权行**没有**对应 Session（不可回填）→ 回来时仍是 0 行
    assert _grant_rows(pg_engine, g["tenant"]) == [], \
        "downgrade 是登记的**有损**操作，不得静默恢复授权"


# ================================================== 2. Schema / 冻结 DDL


def test_frozen_ddl_conformance(pg_engine) -> None:
    """列 / 约束 / 索引 / 复合 FK 与 0007 冻结 DDL 逐项一致。"""
    inspector = inspect(pg_engine)
    cols = {c["name"]: c for c in inspector.get_columns(TABLE)}
    assert set(cols) == {
        "id", "tenant_id", "channel_id", "external_conversation_id",
        "connection_id", "created_at", "updated_at"}
    for required in ("tenant_id", "channel_id", "external_conversation_id",
                     "connection_id", "created_at", "updated_at"):
        assert cols[required]["nullable"] is False, required
    # 不携带状态 / 效果 / 身份（deny 由**缺行**表达）
    for banned in ("status", "effect", "priority", "agent_id", "identity_id",
                   "user_id", "decision"):
        assert banned not in cols, f"授权表长出 {banned}"

    uniques = {c["name"] for c in inspector.get_unique_constraints(TABLE)}
    assert "uq_connection_conversations_tenant_channel_conv_conn" in uniques
    indexes = {i["name"] for i in inspector.get_indexes(TABLE)}
    assert {f"ix_{TABLE}_tenant_id", f"ix_{TABLE}_connection_id"} <= indexes
    assert inspector.get_pk_constraint(TABLE)["name"] is not None

    conn_uniques = {c["name"] for c in
                    inspector.get_unique_constraints("connections")}
    assert "uq_connections_tenant_channel_id" in conn_uniques, \
        "复合 FK 的 supporting UNIQUE 缺失"

    fks = {fk["name"]: fk for fk in inspector.get_foreign_keys(TABLE)}
    composite = fks["fk_connection_conversations_tenant_channel_connection"]
    assert composite["constrained_columns"] == ["tenant_id", "channel_id", "connection_id"]
    assert composite["referred_table"] == "connections"
    assert composite["referred_columns"] == ["tenant_id", "channel_id", "id"]
    # 阻断式：无 ondelete（NO ACTION）——不 cascade
    assert composite["options"].get("ondelete") is None, \
        "复合 FK 出现 ondelete（隐式级联被明确拒绝）"


# ================================================== 3. RLS


def test_rls_enabled_and_forced_on_grants(pg_engine) -> None:
    with pg_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = :t"), {"t": TABLE}).one()
        assert row == (True, True), f"RLS 未 ENABLE+FORCE：{row}"

        policies = list(conn.execute(text(
            "SELECT qual, with_check FROM pg_policies "
            "WHERE schemaname='public' AND tablename=:t"), {"t": TABLE}))
        assert len(policies) == 1, policies
        qual, with_check = policies[0]
        assert "app.tenant_id" in qual and "app.tenant_id" in with_check
        assert "NULLIF" in qual, "RLS 表达式必须是 0005 的 NULLIF 形态"


def test_rls_isolates_tenants_on_grants_with_app_role(pg_engine) -> None:
    """非超级用户角色实测：tenant A ≠ tenant B；未设上下文 → 看不见任何行。"""
    from app.core.tenant import TENANT_LOCAL

    g = _seed_graph(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(text(
            f"INSERT INTO {TABLE} (tenant_id, channel_id, external_conversation_id, "
            f"connection_id) VALUES (:t, :c, :conv, :k)"),
            {"t": g["tenant"], "c": g["channel"], "conv": "conv-rls",
             "k": g["connection"]})

    role, password = "acp_closure_app", "closure-pw"
    with pg_engine.begin() as conn:
        conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        conn.execute(text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
            f"TO {role}"))
        conn.execute(text(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}"))

    base = _pg_url()
    head, sep, tail = base.partition("://")
    _, _, hostpart = tail.partition("@")
    app_engine = create_engine(f"{head}{sep}{role}:{password}@{hostpart}")
    try:
        with app_engine.connect() as conn:
            # 未设租户上下文 → 策略谓词为 NULL → 零行（fail-closed）
            assert conn.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar() == 0

            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": str(g["tenant"])})
            assert conn.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar() == 1

            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": "999999"})       # tenant B（不存在）
            assert conn.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar() == 0
    finally:
        app_engine.dispose()
        with pg_engine.begin() as conn:
            conn.execute(text(f"DROP OWNED BY {role}"))
            conn.execute(text(f"DROP ROLE IF EXISTS {role}"))


# ================================================== 4. Grant integrity（DB 约束）


def _insert_grant(engine, *, tenant, channel, conversation, connection) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            f"INSERT INTO {TABLE} (tenant_id, channel_id, external_conversation_id, "
            f"connection_id) VALUES (:t, :c, :conv, :k)"),
            {"t": tenant, "c": channel, "conv": conversation, "k": connection})


@pytest.mark.parametrize("case", ["wrong_tenant", "wrong_channel", "wrong_connection"])
def test_wrong_tenant_channel_or_connection_is_rejected(pg_engine, case) -> None:
    """复合 FK 逐行锁住三种错配——DB 层拒绝，不依赖应用层自觉。"""
    g = _seed_graph(pg_engine)
    payload = {"tenant": g["tenant"], "channel": g["channel"],
               "conversation": f"conv-{case}", "connection": g["connection"]}
    if case == "wrong_tenant":
        payload["tenant"] = g["tenant"] + 1          # 跨租户
    elif case == "wrong_channel":
        payload["channel"] = g["channel_b"]          # 同租户、跨 channel
    else:
        payload["connection"] = 999999               # 不存在的 connection
    with pytest.raises(IntegrityError):
        _insert_grant(pg_engine, **payload)


def test_inactive_connection_grant_is_allowed(pg_engine) -> None:
    """授权与可运维性**正交**：inactive 连接照常可被授权（B3 冻结）。

    后果是 `granted ≠ ∅` 但 `active ∩ granted = ∅` → ROUTING_FAILED（比 UNAUTHORIZED
    更准确：曾授权、路径不可用）。
    """
    g = _seed_graph(pg_engine, extra_inactive_connection=True)
    _insert_grant(pg_engine, tenant=g["tenant"], channel=g["channel"],
                  conversation="conv-idle", connection=g["connection_inactive"])
    rows = _grant_rows(pg_engine, g["tenant"])
    assert rows == [(g["channel"], "conv-idle", g["connection_inactive"])]


def test_duplicate_grant_is_rejected(pg_engine) -> None:
    g = _seed_graph(pg_engine)
    payload = {"tenant": g["tenant"], "channel": g["channel"],
               "conversation": "conv-dup", "connection": g["connection"]}
    _insert_grant(pg_engine, **payload)
    with pytest.raises(IntegrityError):
        _insert_grant(pg_engine, **payload)


# ================================================== 5. Backfill（0006 → 0007）


def test_backfill_from_sessions_at_0007(pg_engine) -> None:
    """真实回填：0006 state → seed sessions → 0007 → grants，逐条验收冻结语义。"""
    from app.core.tenant import TENANT_LOCAL
    from app.domain.agent import Agent
    from app.domain.channel import Channel
    from app.domain.connection import Connection
    from app.domain.identity import Identity
    from app.domain.session import Session as SessionEntity
    from app.domain.user import User

    url = _pg_url()
    _reset_schema(url)
    _alembic("0006")

    engine = create_engine(url)
    try:  # ---- ① 0006 state：播种历史 Session（grant 表此刻**不存在**）
        assert TABLE not in set(inspect(engine).get_table_names()), \
            "0006 不应存在授权表"
        with _uow_factory(engine, TENANT_LOCAL)() as uow:
            user_a = uow.users.create(User(name="alice"))
            user_b = uow.users.create(User(name="bob"))
            channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
            agent = uow.agents.create(Agent(name="agent-a", type="acp"))
            identity_a = uow.identities.create(Identity(
                user_id=user_a.id, channel_id=channel.id, external_id="wx_a"))
            identity_b = uow.identities.create(Identity(
                user_id=user_b.id, channel_id=channel.id, external_id="wx_b"))
            agent_idle = uow.agents.create(Agent(name="agent-idle", type="acp"))
            conn_active = uow.connections.create(Connection(
                channel_id=channel.id, agent_id=agent.id, name="path-a",
                status="active"))
            conn_idle = uow.connections.create(Connection(
                channel_id=channel.id, agent_id=agent_idle.id, name="path-idle",
                status="inactive"))

            def _session(identity_id, conversation, connection_id, status="active"):
                return uow.sessions.create(SessionEntity(
                    identity_id=identity_id, channel_id=channel.id,
                    connection_id=connection_id,
                    external_conversation_id=conversation, status=status))

            # 群聊同一会话的**多成员**（不同 identity，同一 (channel, G1, conn)）
            # → 源侧多行、**必须收敛为 1 条授权**
            # （sessions 的 UNIQUE 含 identity_id，所以"同三元组多行"只能由
            #   不同成员产生——这正是群聊的真实形态）
            _session(identity_a.id, "G1", conn_active.id)
            _session(identity_b.id, "G1", conn_active.id)
            # 同一会话的**多 Connection**（历史迁移）→ 2 条授权
            _session(identity_a.id, "G1", conn_idle.id)          # inactive 照常回填
            _session(identity_a.id, "G2", conn_idle.id)
            # 空 / 空白会话键 → 跳过
            _session(identity_a.id, "", conn_active.id)
            _session(identity_a.id, "   ", conn_active.id)
            # 无 connection 的 Session → 跳过
            _session(identity_a.id, "G3", None)
            uow.commit()
        expected = {(channel.id, "G1", conn_active.id),
                    (channel.id, "G1", conn_idle.id),
                    (channel.id, "G2", conn_idle.id)}

        # ---- ② 0007：迁移内回填 + 每租户自检（自检失败会 raise → upgrade 失败）
        _alembic("head")

        # ---- ③ grants：DISTINCT 收敛 + 不过滤 status + 滤空 + 绝不静默 0 行
        rows = _grant_rows(engine, TENANT_LOCAL)
        assert set(rows) == expected, rows
        assert len(rows) == len(expected) == 3, "同会话多成员未收敛为 1 条"
        assert rows != [], "回填**静默 0 行**（本 Package 点名的最大风险）"
        assert (channel.id, "G1", conn_idle.id) in rows, "inactive Connection 被丢弃"
        assert not [r for r in rows if not r[1].strip()], "空白会话键未被跳过"
    finally:
        engine.dispose()


def test_backfill_on_empty_database_is_noop_not_failure(pg_engine) -> None:
    """空库 / 新部署：回填 no-op → 0 授权（ADR-6：全 DENY 是**正确**行为）。"""
    assert _grant_rows(pg_engine, _local_tenant()) == []


# ================================================== 6. Runtime（真实 PG）


def test_runtime_unbound_identity_with_grant_is_allowed_on_pg(pg_engine) -> None:
    """★ user_id=NULL 的外部身份 + 有效 grant → 全链 ALLOW（Package 1 × 2 接缝）。"""
    from app.application.access_authorization import AccessAuthorizationExecutor
    from app.application.agent_dispatch import AgentDispatchExecutor
    from app.application.coordinator import RuntimeCoordinator
    from app.application.delivery import DeliveryExecutor
    from app.application.identity_resolution import IdentityResolutionExecutor
    from app.application.routing import RoutingExecutor
    from app.application.session_creation import SessionCreationExecutor
    from app.application.session_resolution import SessionResolutionExecutor
    from app.domain.agent_response import AgentResponse
    from app.domain.connection_conversation import ConnectionConversation
    from app.domain.inbound_interaction import InboundInteraction
    from app.domain.outbound_response import OutboundResponse

    tid = _local_tenant()
    with _uow_factory(pg_engine, tid)() as uow:
        from app.domain.agent import Agent
        from app.domain.channel import Channel
        from app.domain.connection import Connection
        from app.domain.identity import Identity

        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        identity = uow.identities.create(Identity(
            user_id=None, channel_id=channel.id, external_id="wx_unbound"))
        conn = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a"))
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="G1",
            connection_id=conn.id))
        uow.commit()
        channel_id, identity_id = channel.id, identity.id

    class _Agent:
        def send(self, request, connection) -> AgentResponse:
            return AgentResponse(content="ok")

    class _Channel:
        def __init__(self) -> None:
            self.delivered: list[OutboundResponse] = []

        def deliver(self, outbound: OutboundResponse) -> None:
            self.delivered.append(outbound)

    uow_factory = _uow_factory(pg_engine, tid)
    sink = _Channel()
    coordinator = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow_factory),
        access_authorization=AccessAuthorizationExecutor(uow_factory),
        routing=RoutingExecutor(uow_factory),
        session_resolution=SessionResolutionExecutor(uow_factory),
        session_creation=SessionCreationExecutor(uow_factory),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=_Agent(), uow_factory=uow_factory),
        delivery=DeliveryExecutor(channel_adapter=sink),
    )
    result = coordinator.execute(InboundInteraction(
        channel_id=channel_id, external_interaction_id="pg-1",
        external_conversation_id="G1", external_identity_id="wx_unbound",
        content="hello"))

    assert result.session_id is not None
    assert sink.delivered and sink.delivered[-1].content == "ok"
    with _uow_factory(pg_engine, tid)() as uow:
        assert uow.identities.get_by_id(identity_id).user_id is None, \
            "授权通过后仍不得补建身份绑定"


def test_runtime_without_grant_is_unauthorized_on_pg(pg_engine) -> None:
    """no grant → UNAUTHORIZED，且失败形状按冻结五元组落库（真实 PG）。"""
    from app.application.access_authorization import AccessAuthorizationExecutor
    from app.application.errors import AccessAuthorizationDeniedError
    from app.application.interaction_recording import PersistingCoordinator

    g = _seed_graph(pg_engine)
    executor = AccessAuthorizationExecutor(_uow_factory(pg_engine, g["tenant"]))
    with pytest.raises(AccessAuthorizationDeniedError):
        executor.execute(g["channel"], "G-no-grant")

    class _Void:
        def execute(self, inbound) -> None:
            executor.execute(inbound.channel_id, inbound.external_conversation_id)

    from app.domain.inbound_interaction import InboundInteraction

    wrapped = PersistingCoordinator(
        _Void(), _uow_factory(pg_engine, g["tenant"]))
    with pytest.raises(AccessAuthorizationDeniedError):
        wrapped.execute(InboundInteraction(
            channel_id=g["channel"], external_interaction_id="pg-deny",
            external_conversation_id="G-no-grant",
            external_identity_id="wx_a", content="hi"))

    with pg_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT processing_status, delivery_status, failure_stage, "
            "failure_code, retryable, session_id, response_payload "
            "FROM interactions WHERE external_interaction_id = 'pg-deny'")).one()
    assert row[0] == "failed" and row[1] == "pending"
    assert row[2] == "authorization" and row[3] == "UNAUTHORIZED"
    assert row[4] is False and row[5] is None and row[6] is None


# ================================================== 7. Management API（真实 PG）


def _api_client(engine, tenant_id: int):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.management.connection_conversations import (
        build_connection_conversations_api,
    )
    from app.application.connection_conversations import (
        ConnectionConversationApplicationService,
    )

    service = ConnectionConversationApplicationService(
        uow_factory=_uow_factory(engine, tenant_id))
    app = FastAPI()
    app.include_router(build_connection_conversations_api(service=service))
    return TestClient(app)


def test_management_crud_and_discovery_on_pg(pg_engine) -> None:
    """Create / List / Delete / known-conversations 在真实 PG 上成立。"""
    from app.core.tenant import TENANT_LOCAL
    from app.domain.session import Session as SessionEntity

    g = _seed_graph(pg_engine)
    client = _api_client(pg_engine, g["tenant"])
    payload = {"channel_id": g["channel"], "external_conversation_id": "G-pg",
               "connection_id": g["connection"]}

    created = client.post("/api/connection-conversations", json=payload)
    assert created.status_code == 201, created.text
    grant_id = created.json()["id"]
    assert client.post("/api/connection-conversations", json=payload).status_code == 409

    listed = client.get("/api/connection-conversations",
                        params={"connection_id": g["connection"]})
    assert listed.status_code == 200 and listed.json()["total"] == 1

    # known-conversations：会话出现即可见（此处先无 Session → 空）
    known = client.get(f"/api/channels/{g['channel']}/known-conversations")
    assert known.status_code == 200 and known.json()["conversations"] == []

    with _uow_factory(pg_engine, TENANT_LOCAL)() as uow:
        uow.sessions.create(SessionEntity(
            identity_id=g["identity"], channel_id=g["channel"],
            connection_id=g["connection"],
            external_conversation_id="G-known", status="active"))
        uow.commit()
    known = client.get(f"/api/channels/{g['channel']}/known-conversations")
    assert known.json()["conversations"] == ["G-known"]

    assert client.delete(
        f"/api/connection-conversations/{grant_id}").status_code == 204
    assert client.delete(
        f"/api/connection-conversations/{grant_id}").status_code == 404


def test_known_is_not_authorized_on_pg(pg_engine) -> None:
    """★ 核心红线（真实 PG）：known conversation 仍被 Runtime 拒绝。"""
    from app.application.access_authorization import AccessAuthorizationExecutor
    from app.application.errors import AccessAuthorizationDeniedError
    from app.core.tenant import TENANT_LOCAL
    from app.domain.session import Session as SessionEntity

    g = _seed_graph(pg_engine)
    with _uow_factory(pg_engine, TENANT_LOCAL)() as uow:
        uow.sessions.create(SessionEntity(
            identity_id=g["identity"], channel_id=g["channel"],
            connection_id=g["connection"],
            external_conversation_id="G-only-known", status="active"))
        uow.commit()

    client = _api_client(pg_engine, g["tenant"])
    known = client.get(f"/api/channels/{g['channel']}/known-conversations").json()
    assert known["conversations"] == ["G-only-known"]

    with pytest.raises(AccessAuthorizationDeniedError):
        AccessAuthorizationExecutor(
            _uow_factory(pg_engine, g["tenant"])).execute(
            g["channel"], "G-only-known")
    assert _grant_rows(pg_engine, g["tenant"]) == [], "discovery 产生了授权"
