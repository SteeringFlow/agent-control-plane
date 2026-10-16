"""M6.6-C4 / CP4-1-B — Session Concurrency（唯一约束冲突 → 回滚 → 精确回读复用）。

被守护的不变量（CP4-1-A §20/§21、CP4-1-B §12）：

    B1  已有 Session ⇒ 复用（不产生第二行）
    B2  顺序创建 ⇒ 恰好一行
    B3  同一四元组并发 ⇒ 恰好 1 Session，两条都成功且引用同一 Session
    B4  PostgreSQL：唯一冲突 → 回滚 → 精确回读 → 复用（PG env 门控）
    B5  SQLite 写锁（OperationalError）→ 精确回读复用，绝不产生第二 Session
    B6  回滚后回读落空 ⇒ SESSION_RESOLUTION_FAILED（绝不谎报成功）
    B7  tenant 隔离：跨租户查找不可见 ⇒ 不跨租户误复用
    B8  同 tenant/identity/connection、不同 conversation ⇒ 两条 Session
    B9  同 tenant/connection/conversation、不同 identity ⇒ 两条 Session
    B10 Session 1:N Interaction（多 Interaction 共享同一 Session 合法）
    B11 replay：重复处理同一条消息 ⇒ 不新建 Session
    B12 worker reclaim：孤儿回收后重跑 ⇒ 继续复用原 Session
    B13 Bootstrap 集成：并发首条消息 ⇒ 1 Identity + 1 Grant + 1 Session + 2 Interaction

本轮**不做** migration / **不新增** error code / **不改** Runtime 阶段。
"""

import os
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session as SASession, sessionmaker

from app.adapters.agents.local import LocalAgentAdapter
from app.adapters.agents.registry import MultiAgentAdapter
from app.adapters.channels.local import LocalChannelAdapter
from app.adapters.channels.registry import MultiChannelAdapter
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.bootstrap import BootstrapCoordinator
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.errors import SessionResolutionFailedError
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import PersistingCoordinator
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.application.worker import Worker
from app.core.tenant import TENANT_LOCAL
from app.domain.agent import Agent
from app.domain.bootstrap_arm import BootstrapArm
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import (
    CHAT_TYPE_METADATA_KEY,
    CHAT_TYPE_SINGLE,
    InboundInteraction,
)
from app.domain.interaction_record import InteractionRecord
from app.domain.session import Session
from app.domain.user import User
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

_BACKEND = Path(__file__).resolve().parents[1]
_PG_URL_ENV = "CLOUD_TEST_DATABASE_URL"

#: local 通道形态：conversation == identity（单聊），CP4-1 的精确四元组键。
EXT = "wx-cp41"
CONV = EXT


# ================================================================== helpers


def _uow(engine, tenant_id: int | None = TENANT_LOCAL):
    return SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        tenant_id=tenant_id,
    )


def _factory(engine):
    return lambda: _uow(engine)


def _graph(engine, *, external_id: str = EXT, tenant_id: int = TENANT_LOCAL) -> dict:
    """user / channel / agent / connection / identity → ids。"""
    with _uow(engine, tenant_id) as uow:
        user = uow.users.create(User(name="cp41-user"))
        channel = uow.channels.create(Channel(name="cp41-ch", type="local"))
        agent = uow.agents.create(Agent(name="cp41-agent", type="local"))
        connection = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="cp41-path"))
        identity = uow.identities.create(Identity(
            user_id=None, channel_id=channel.id, external_id=external_id))
        uow.commit()
        assert all(x.id for x in (user, channel, agent, connection, identity))
        return {
            "tenant": tenant_id, "user": user.id, "channel": channel.id,
            "agent": agent.id, "connection": connection.id,
            "identity": identity.id,
        }


def _keys(g: dict, conversation: str = CONV, **overrides) -> dict:
    """SessionCreationExecutor.execute 的输入（精确四元组 + channel/agent）。"""
    kwargs = dict(
        identity_id=g["identity"], channel_id=g["channel"],
        connection_id=g["connection"], agent_id=g["agent"],
        external_conversation_id=conversation,
    )
    kwargs.update(overrides)
    return kwargs


def _session_count(engine) -> int:
    with SASession(engine) as s:
        return int(s.execute(
            select(SessionModel.id)).scalars().all().__len__())


def _session_ids(engine) -> list[int]:
    with SASession(engine) as s:
        return list(s.execute(
            select(SessionModel.id).order_by(SessionModel.id)).scalars().all())


def _interaction_rows(engine) -> list[tuple]:
    with SASession(engine) as s:
        return [
            tuple(r) for r in s.execute(text(
                "SELECT id, session_id, processing_status, delivery_status "
                "FROM interactions ORDER BY id")).fetchall()
        ]


def _executor(engine) -> SessionCreationExecutor:
    return SessionCreationExecutor(_factory(engine))


def _run_concurrently(targets: list) -> list:
    """并发跑 targets（各自可抛异常）→ [(ok, result_or_exc), ...]。"""
    out: list = [(False, None)] * len(targets)

    def wrap(i, fn):
        def run():
            try:
                out[i] = (True, fn())
            except BaseException as exc:  # noqa: BLE001 原样记录，交由断言解释
                out[i] = (False, exc)
        return run

    threads = [threading.Thread(target=wrap(i, fn))
               for i, fn in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return out


# ================================================================== B1/B2


def test_b1_existing_session_is_reused_not_duplicated(db_engine) -> None:
    """B1：已有 Session ⇒ find_or_create 命中复用，不产生第二行。"""
    g = _graph(db_engine)
    first = _executor(db_engine).execute(**_keys(g))
    second = _executor(db_engine).execute(**_keys(g))

    assert first.id == second.id, "同四元组必须复用同一 Session"
    assert _session_count(db_engine) == 1


def test_b2_sequential_create_materializes_one_row(db_engine) -> None:
    """B2：无 Session ⇒ 顺序创建一行。"""
    g = _graph(db_engine)
    created = _executor(db_engine).execute(**_keys(g))

    assert created.id is not None and created.status == "active"
    assert _session_count(db_engine) == 1


# ================================================================== B3/B5 并发


def test_b3_concurrent_same_four_tuple_yields_one_session(
    isolated_db_engine,
) -> None:
    """B3：同一四元组并发 ⇒ 恰好 1 Session，两条都成功且引用同一 Session。"""
    engine = isolated_db_engine
    g = _graph(engine)

    results = _run_concurrently([
        lambda: _executor(engine).execute(**_keys(g)),
        lambda: _executor(engine).execute(**_keys(g)),
    ])

    errors = [r[1] for r in results if not r[0]]
    assert not errors, f"并发两条都应成功（含竞争输家）：{errors}"
    ids = {r[1].id for r in results}
    assert len(ids) == 1, f"两条必须引用同一 Session：{ids}"
    assert _session_count(engine) == 1, "并发不得产生第二行"


def test_b5_sqlite_write_lock_is_recovered_by_exact_reread(
    isolated_db_engine, monkeypatch,
) -> None:
    """B5：SQLite 写锁（OperationalError）→ 精确回读复用，绝不产生第二 Session。

    确定性构造（不依赖真实锁竞争时序）：首查刻意落空 + create 抛写锁异常；
    赢家事实已由**另一事务**提交 ⇒ 恢复必须靠「回读精确四元组」而非「捕获异常」。
    """
    engine = isolated_db_engine
    g = _graph(engine)
    from app.persistence.repositories.session import SqlAlchemySessionRepository as Repo

    # 赢家：另一条连接先行提交
    with _uow(engine) as uow:
        winner = uow.sessions.create(Session(**_session_domain_kwargs(g)))
        uow.commit()
    assert winner.id is not None

    real_find = Repo.find_active
    calls = {"n": 0}

    def flaky_find(self, identity_id, connection_id, conv):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_find(
            self, identity_id, connection_id, conv)

    def locked(self, session):
        raise OperationalError("INSERT", {}, Exception("database is locked"))

    monkeypatch.setattr(Repo, "find_active", flaky_find)
    monkeypatch.setattr(Repo, "create", locked)

    got = _executor(engine).execute(**_keys(g))

    assert calls["n"] >= 2, "恢复路径必须经过**第二次**回读"
    assert got.id == winner.id, "写锁输家必须复用赢家 Session"
    assert _session_count(engine) == 1, "写锁不得产生第二 Session"


# ================================================================== B6 回读落空


def test_b6_reread_miss_fails_closed(db_engine, monkeypatch) -> None:
    """B6：create 失败 + 回读落空 ⇒ SESSION_RESOLUTION_FAILED（绝不谎报成功）。"""
    g = _graph(db_engine)
    from app.persistence.repositories.session import SqlAlchemySessionRepository as Repo

    def conflict(self, session):
        raise IntegrityError("INSERT", {}, Exception("unique violation"))

    monkeypatch.setattr(Repo, "create", conflict)

    with pytest.raises(SessionResolutionFailedError):
        _executor(db_engine).execute(**_keys(g))

    assert _session_count(db_engine) == 0


# ================================================================== B7/B8/B9 隔离


def test_b7_cross_tenant_lookup_does_not_reuse(db_engine) -> None:
    """B7：tenant 参与精确四元组 —— 另一租户看不到本租户 Session，不误复用。

    对应 CP4-1-B §14（RLS）：tenant B 的 loser reread 绝不能读到 tenant A 的 Session。
    """
    g = _graph(db_engine, tenant_id=TENANT_LOCAL)
    with _uow(db_engine, TENANT_LOCAL) as uow:
        a = uow.sessions.find_or_create(Session(**_session_domain_kwargs(g)))
        uow.commit()

    with _uow(db_engine, None) as uow:
        other_tenant = uow.tenants.create(name="cp41-tenant-two")
        uow.commit()
    assert other_tenant != TENANT_LOCAL

    # tenant B：**完全相同**的四元组查找 → 必须看不到 A 的 Session
    with _uow(db_engine, other_tenant) as uow:
        assert uow.sessions.find_active(
            g["identity"], g["connection"], CONV) is None
        b = uow.sessions.find_or_create(Session(**_session_domain_kwargs(g)))
        uow.commit()

    assert b.id != a.id, "跨租户绝不复用同一 Session"
    assert _session_count(db_engine) == 2


def test_b8_different_conversation_yields_two_sessions(db_engine) -> None:
    """B8：同 tenant/identity/connection、不同 conversation ⇒ 两条 Session。"""
    g = _graph(db_engine)
    first = _executor(db_engine).execute(**_keys(g, conversation="conv-A"))
    second = _executor(db_engine).execute(**_keys(g, conversation="conv-B"))

    assert first.id != second.id
    assert _session_count(db_engine) == 2


def test_b9_different_identity_yields_two_sessions(db_engine) -> None:
    """B9：同 tenant/connection/conversation、不同 identity ⇒ 两条 Session。"""
    g = _graph(db_engine)
    with _uow(db_engine) as uow:
        other = uow.identities.create(Identity(
            user_id=None, channel_id=g["channel"], external_id="wx-cp41-2"))
        uow.commit()

    first = _executor(db_engine).execute(**_keys(g))
    second = _executor(db_engine).execute(**_keys(g, identity_id=other.id))

    assert first.id != second.id
    assert _session_count(db_engine) == 2


# ================================================================== B10 1:N


def test_b10_multiple_interactions_share_one_session(db_engine) -> None:
    """B10：Session 1:N Interaction —— 多 Interaction 共享同一 Session 合法。"""
    g = _graph(db_engine)
    session = _executor(db_engine).execute(**_keys(g))

    with _uow(db_engine) as uow:
        for i in range(3):
            uow.interactions.create(InteractionRecord(
                channel_id=g["channel"], external_interaction_id=f"m-{i}",
                session_id=session.id))
        uow.commit()

    rows = _interaction_rows(db_engine)
    assert len(rows) == 3
    assert {r[1] for r in rows} == {session.id}, "三条 Interaction 必须共享同一 Session"
    assert _session_count(db_engine) == 1


# ================================================================== 运行时链路


def _chain(engine, outbox_path, *, with_bootstrap: bool = False):
    """完整七段 Runtime + local 适配器（+ 可选 Bootstrap）。"""
    f = _factory(engine)
    channel_adapter = MultiChannelAdapter(
        {"local": LocalChannelAdapter(outbox_path=outbox_path)}, uow_factory=f)
    agent_adapter = MultiAgentAdapter({"local": LocalAgentAdapter()}, uow_factory=f)
    runtime = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(f),
        access_authorization=AccessAuthorizationExecutor(f),
        routing=RoutingExecutor(f),
        session_resolution=SessionResolutionExecutor(f),
        session_creation=SessionCreationExecutor(f),
        agent_dispatch=AgentDispatchExecutor(agent_adapter=agent_adapter,
                                             uow_factory=f),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )
    phases = PersistingCoordinator(inner=runtime, uow_factory=f)
    bootstrap = BootstrapCoordinator(uow_factory=f) if with_bootstrap else None
    worker = Worker(runtime=runtime, phases=phases, uow_factory=f,
                    bootstrap=bootstrap)
    return phases, worker


def _inbound(channel_id: int, *, msg_id: str = "m-1", conversation: str = CONV,
             external_id: str = EXT) -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id,
        external_interaction_id=msg_id,
        external_conversation_id=conversation,
        external_identity_id=external_id,
        content="hello",
        metadata={CHAT_TYPE_METADATA_KEY: CHAT_TYPE_SINGLE},
    )


def _grant(engine, g: dict, conversation: str = CONV) -> None:
    """既有管理面路径显式授权（非 Bootstrap）：Conversation → Connection。"""
    with _uow(engine) as uow:
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=g["channel"], external_conversation_id=conversation,
            connection_id=g["connection"]))
        uow.commit()


def _arm(engine, g: dict) -> int:
    with _uow(engine) as uow:
        arm = uow.bootstrap_arms.arm(
            BootstrapArm(channel_id=g["channel"], connection_id=g["connection"],
                         initiator_user_id=g["user"]),
            ttl_seconds=600)
        uow.commit()
    assert arm.id is not None
    return arm.id


def _backdate(engine, interaction_id: int, seconds: int) -> None:
    """时间机器：把 processing_started_at 回拨（reclaim cutoff 由 DB 计算）。"""
    with SASession(engine) as s:
        s.execute(text(
            "UPDATE interactions SET processing_started_at = "
            "datetime('now', :minus) WHERE id = :i"),
            {"minus": f"-{seconds} seconds", "i": interaction_id})
        s.commit()


# ================================================================== B11 replay


def test_b11_replay_does_not_create_a_second_session(db_engine, tmp_path) -> None:
    """B11：同一条消息被重复处理 ⇒ claim 已失 ⇒ 不新建 Session。"""
    g = _graph(db_engine)
    _grant(db_engine, g)
    phases, worker = _chain(db_engine, tmp_path / "outbox.jsonl")

    interaction_id = phases.create_pending(_inbound(g["channel"]))
    assert worker.process(interaction_id) is True
    assert _session_count(db_engine) == 1

    # replay：同一 interaction 再次处理 → claim CAS 拒绝 → 不再建 Session
    assert worker.process(interaction_id) is False
    assert _session_count(db_engine) == 1, "replay 不得新建 Session"


# ================================================================== B12 reclaim


def test_b12_worker_reclaim_reuses_existing_session(db_engine, tmp_path) -> None:
    """B12：孤儿回收后重跑 ⇒ 继续复用（不新建）原 Session。"""
    g = _graph(db_engine)
    _grant(db_engine, g)
    phases, worker = _chain(db_engine, tmp_path / "outbox.jsonl")

    interaction_id = phases.create_pending(_inbound(g["channel"]))
    # 1) 某个 Worker claim 后崩溃（未落终态）；其执行中途已建立 Session
    assert phases.claim_processing(interaction_id) is True
    with _uow(db_engine) as uow:
        existing = uow.sessions.find_or_create(Session(**_session_domain_kwargs(g)))
        uow.commit()
    assert existing.id is not None
    _backdate(db_engine, interaction_id, seconds=120)

    # 2) reclaim 孤儿 → PENDING
    assert phases.reclaim_stale_processing(30) == 1

    # 3) 新 Worker 重新 pickup 执行 → REUSE_ACTIVE，不新建 Session
    assert worker.process(interaction_id) is True
    assert _session_count(db_engine) == 1

    rows = _interaction_rows(db_engine)
    assert rows[0][1] == existing.id, "reclaim 重跑必须挂回原 Session"


# ================================================================== B13 Bootstrap 集成


def test_b13_concurrent_first_messages_one_identity_grant_session(
    isolated_db_engine, tmp_path,
) -> None:
    """B13：并发首条消息 ⇒ 1 Identity + 1 Grant + 1 Session + 2 Interaction。"""
    engine = isolated_db_engine
    g = _graph(engine)
    _arm(engine, g)
    phases, worker = _chain(engine, tmp_path / "outbox.jsonl", with_bootstrap=True)

    ids = [phases.create_pending(_inbound(g["channel"], msg_id=f"m-{i}"))
           for i in (1, 2)]
    results = _run_concurrently([
        lambda: worker.process(ids[0]),
        lambda: worker.process(ids[1]),
    ])
    errors = [r[1] for r in results if not r[0]]
    assert not errors, f"并发首条消息不得抛错：{errors}"

    with SASession(engine) as s:
        identities = int(s.execute(text("SELECT count(*) FROM identities")).scalar_one())
        grants = int(s.execute(
            text("SELECT count(*) FROM connection_conversations")).scalar_one())
        users = int(s.execute(text("SELECT count(*) FROM users")).scalar_one())

    assert identities == 1, "并发首条消息只能建立一条 Identity（Bootstrap CAS）"
    assert grants == 1, "并发首条消息只能建立一条 Grant"
    assert users == 1, "绝不创建 ACP User（只有种子 admin）"
    assert _session_count(engine) == 1, "并发首条消息只能有一条 Session"

    rows = _interaction_rows(engine)
    assert len(rows) == 2, "两条消息各自留下一条 Interaction"
    assert {r[1] for r in rows} == set(_session_ids(engine)), \
        "两条 Interaction 必须引用同一 Session（reuse，不是 dedup）"
    assert all(r[2] == "succeeded" for r in rows), rows


def _session_domain_kwargs(g: dict, conversation: str = CONV) -> dict:
    return dict(
        identity_id=g["identity"], channel_id=g["channel"],
        connection_id=g["connection"], agent_id=g["agent"],
        external_conversation_id=conversation,
    )


# ================================================================== B4 PostgreSQL


def _pg_url() -> str | None:
    return os.environ.get(_PG_URL_ENV)


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
    url = _pg_url()
    _pg_reset_schema(url)
    _pg_alembic_upgrade(url)
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytestmark_pg
def test_b4_postgres_unique_race_recovers_to_reuse(pg_engine) -> None:
    """B4：PostgreSQL READ COMMITTED —— 唯一冲突 → 回滚 → 精确回读 → 复用。"""
    engine = pg_engine
    g = _graph(engine, tenant_id=TENANT_LOCAL)

    results = _run_concurrently([
        lambda: _executor(engine).execute(**_keys(g)),
        lambda: _executor(engine).execute(**_keys(g)),
    ])

    errors = [r[1] for r in results if not r[0]]
    assert not errors, f"PG 并发两条都应成功：{errors}"
    assert len({r[1].id for r in results}) == 1
    with _uow(engine) as uow:
        total = int(uow.session.execute(
            text("SELECT count(*) FROM sessions")).scalar_one())
    assert total == 1


@pytestmark_pg
def test_b4b_postgres_reread_stays_within_tenant(pg_engine) -> None:
    """B4b：PG 上 reread 走 tenant 作用域 + RLS —— 不跨租户误复用。"""
    engine = pg_engine
    g = _graph(engine, tenant_id=TENANT_LOCAL)
    with _uow(engine, TENANT_LOCAL) as uow:
        created = uow.sessions.find_or_create(Session(**_session_domain_kwargs(g)))
        uow.commit()
    assert created.id is not None

    with _uow(engine, TENANT_LOCAL + 1) as uow:
        assert uow.sessions.find_active(
            g["identity"], g["connection"], CONV) is None
