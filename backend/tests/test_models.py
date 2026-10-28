"""Step 5.3 验收：8 张表确实表达了 4.2 冻结的数据关系，且没有偷偷长出新概念。

Test A  Base.metadata 恰好包含 8 张表（多一张少一张都算漂移）
Test B  逐表 Schema：PK / FK / 必填 / nullable / 默认值 / 时间戳 / JSON
Test C  唯一约束真实拦截重复插入（DB 级，不是 ORM 级）
Test D  外键真实拦截悬挂引用（依赖 PRAGMA foreign_keys=ON）
Test E  全链路关系完整性（User→Identity→Session→Interaction→Activity + Channel/Agent/Connection）
Test F  Context Isolation：不同 identity × 不同 external_conversation_id 不被错误合并
附加    mappers/repositories 仍是空壳（5.3 不提前实现）
"""

import pytest
from sqlalchemy import UniqueConstraint, insert, select
from sqlalchemy.exc import IntegrityError

from tests.conftest import CORE_TABLES
from app.persistence.database import Base
from app.persistence.models import (
    ActivityModel,
    AgentModel,
    ChannelModel,
    ConnectionModel,
    IdentityModel,
    InteractionModel,
    SessionModel,
    UserModel,
)


# ---------------------------------------------------------------- Test A


def test_a_metadata_contains_core_tables_plus_tenants() -> None:
    """8 张 4.2 冻结业务表 + `tenants` root + Cloud-0.1.1-C 的 2 张 Browser Auth 表
    + Implementation Package 2 的 `connection_conversations`（共 12 张）。

    `tenants` 是 Cloud-0.1.1-B 引入的租户根表，不是第 9 张业务表——
    它是 8 张业务表 `tenant_id` FK 的目标根。
    `user_credentials` / `browser_sessions` 是 Cloud-0.1.1-C 引入的 Browser Auth
    表（凭据 / opaque 会话），同样不是 4.2 业务表。
    `connection_conversations` 是 Implementation Package 2 / Step 1 引入的
    **V0 唯一授权状态存储**（ADR-3 Conversation → Connection；由 ADR-11 授权），
    同样不是 4.2 业务表。
    """
    expected = set(CORE_TABLES) | {
        "tenants", "user_credentials", "browser_sessions",
        "connection_conversations",
        # M6.6-C3 / ADR-12 迁移 0008：Bootstrap 窗口存储（不是授权状态）。
        "bootstrap_arms"}
    assert set(Base.metadata.tables) == expected, (
        f"表集合漂移：多出 {set(Base.metadata.tables) - expected}，"
        f"缺失 {expected - set(Base.metadata.tables)}"
    )


def test_a_table_count_is_twelve() -> None:
    # 12 = 8 张 4.2 业务表 + tenants + 2 张 Browser Auth + connection_conversations
    # M6.6-C3 / ADR-12：+ bootstrap_arms ⇒ 13
    assert len(Base.metadata.tables) == 13


# ---------------------------------------------------------------- Test B


def _table(name: str):
    return Base.metadata.tables[name]


def _assert_timestamps(t, has_updated_at: bool = True) -> None:
    assert not t.columns["created_at"].nullable
    assert t.columns["created_at"].server_default is not None
    if has_updated_at:
        assert not t.columns["updated_at"].nullable
        assert t.columns["updated_at"].server_default is not None


def _fk_target(t, column: str) -> str:
    """返回某列首个外键指向的表名（如 users.id -> users）。

    ForeignKey 没有 referenced_table 属性；用 target_fullname 解析，
    避免触发未绑定 metadata 的懒解析。
    """
    fk = next(iter(t.columns[column].foreign_keys))
    return fk.target_fullname.partition(".")[0]


def test_b_users_schema() -> None:
    t = _table("users")
    assert [c.name for c in t.columns] == [
        "id", "name", "status", "created_at", "updated_at", "tenant_id",
    ]
    assert t.columns["id"].primary_key
    assert not t.columns["name"].nullable
    assert t.columns["status"].server_default is not None
    _assert_timestamps(t)


def test_b_channels_schema() -> None:
    t = _table("channels")
    assert [c.name for c in t.columns] == [
        "id", "name", "type", "status", "config", "created_at", "updated_at",
        "tenant_id",
    ]
    assert not t.columns["name"].nullable
    assert not t.columns["type"].nullable
    assert t.columns["config"].nullable
    _assert_timestamps(t)


def test_b_agents_schema() -> None:
    t = _table("agents")
    assert [c.name for c in t.columns] == [
        "id", "name", "type", "endpoint", "status", "config",
        "credential_ref", "created_at", "updated_at", "tenant_id",
    ]
    assert t.columns["endpoint"].nullable  # 4.2 未冻结 nullability，MVP 取宽松
    assert t.columns["credential_ref"].nullable
    _assert_timestamps(t)
    # 凭据明文绝不入库：只允许 credential_ref
    plaintext_markers = ("secret", "password", "token", "api_key")
    assert not [c.name for c in t.columns if any(m in c.name for m in plaintext_markers)]


def test_b_identities_schema() -> None:
    t = _table("identities")
    assert [c.name for c in t.columns] == [
        "id", "user_id", "channel_id", "external_id", "display_name",
        "status", "metadata", "created_at", "updated_at", "tenant_id",
    ]
    assert t.columns["user_id"].foreign_keys
    assert _fk_target(t, "user_id") == "users"
    assert _fk_target(t, "channel_id") == "channels"
    assert not t.columns["external_id"].nullable
    assert t.columns["display_name"].nullable
    # Implementation Package 1 / Gate 2.6 D1/D2：ACP User 关联可选
    # （user_id IS NULL = 未与 ACP Account 建立管理绑定）
    assert t.columns["user_id"].nullable, \
        "identities.user_id 应为可空（未绑定身份是合法的一等运行时身份）"
    # Gate 3 A-2：FK 阻断式——无 ondelete 子句 = NO ACTION
    # （不是 SET NULL / 不是 CASCADE；「保留 Identity」靠阻断删除实现）
    assert next(iter(t.columns["user_id"].foreign_keys)).ondelete is None, \
        "identities.user_id FK 不得引入 ondelete（隐式解绑被明确拒绝）"
    uqs = [u for u in t.constraints
           if u.name == "uq_identities_tenant_channel_external_id"]
    assert uqs, "identities 缺少 UNIQUE(tenant_id, channel_id, external_id)"
    assert {c.name for c in uqs[0].columns} == {
        "tenant_id", "channel_id", "external_id"}
    _assert_timestamps(t)


def test_b_connections_schema() -> None:
    t = _table("connections")
    assert [c.name for c in t.columns] == [
        "id", "channel_id", "agent_id", "name", "status", "config",
        "credential_ref", "created_at", "updated_at", "tenant_id",
    ]
    assert _fk_target(t, "channel_id") == "channels"
    assert _fk_target(t, "agent_id") == "agents"
    uqs = [u for u in t.constraints
           if u.name == "uq_connections_tenant_channel_agent_id"]
    assert uqs, "connections 缺少 UNIQUE(tenant_id, channel_id, agent_id)"
    assert {c.name for c in uqs[0].columns} == {
        "tenant_id", "channel_id", "agent_id"}
    plaintext_markers = ("secret", "password", "token", "api_key")
    assert not [c.name for c in t.columns if any(m in c.name for m in plaintext_markers)]
    _assert_timestamps(t)


def test_b_sessions_schema() -> None:
    t = _table("sessions")
    assert [c.name for c in t.columns] == [
        "id", "identity_id", "channel_id", "connection_id", "agent_id",
        "external_session_id", "external_conversation_id", "status",
        "metadata", "created_at", "updated_at", "tenant_id",
    ]
    fk_targets = {c.name: _fk_target(t, c.name) for c in t.columns if c.foreign_keys}
    assert fk_targets == {
        "identity_id": "identities",
        "channel_id": "channels",
        "connection_id": "connections",
        "agent_id": "agents",
        "tenant_id": "tenants",
    }
    assert not t.columns["identity_id"].nullable
    assert not t.columns["channel_id"].nullable
    # external 标识都是可空的引用语义，不是本表身份
    assert t.columns["external_session_id"].nullable
    assert t.columns["external_conversation_id"].nullable
    _assert_timestamps(t)


def test_b_interactions_schema() -> None:
    t = _table("interactions")
    # 10.5.2：channel_id 物化为一等持久化身份列（原只在 request_payload JSON）
    # 12.9.4.12：processing_started_at 为 attempt fence 的 CAS 时间戳列
    assert [c.name for c in t.columns] == [
        "id", "channel_id", "session_id", "direction",
        "external_interaction_id",
        "request_payload", "response_payload", "processing_status",
        "delivery_status", "failure_stage", "failure_code", "retryable",
        "processing_started_at",
    # P2 Delivery Lifecycle：投递 attempt 计数 / 租约起点 / 协议世代
    "delivery_attempts", "delivery_started_at", "delivery_protocol",
    "created_at", "updated_at", "tenant_id",
    ]
    assert _fk_target(t, "channel_id") == "channels"
    assert _fk_target(t, "session_id") == "sessions"
    assert not t.columns["channel_id"].nullable
    # 双状态必须独立存在，且没有合成的 status 列
    assert "status" not in t.columns
    assert t.columns["processing_status"].server_default is not None
    assert t.columns["delivery_status"].server_default is not None
    assert t.columns["failure_stage"].nullable
    assert t.columns["failure_code"].nullable
    assert not t.columns["retryable"].nullable
    # 10.5.2：Interaction Identity = (channel_id, external_interaction_id)
    uqs = [c for c in t.constraints if isinstance(c, UniqueConstraint)]
    assert len(uqs) == 1
    assert uqs[0].name == "uq_interactions_tenant_channel_external_id"
    assert {c.name for c in uqs[0].columns} == {
        "tenant_id", "channel_id", "external_interaction_id"}
    _assert_timestamps(t)


def test_b_activities_schema() -> None:
    t = _table("activities")
    assert [c.name for c in t.columns] == [
        "id", "interaction_id", "session_id", "type", "stage",
        "status", "message", "metadata", "created_at", "tenant_id",
    ]
    assert _fk_target(t, "interaction_id") == "interactions"
    assert _fk_target(t, "session_id") == "sessions"
    # append-only：只有 created_at，绝无 updated_at
    assert "updated_at" not in t.columns
    assert not t.columns["status"].server_default  # 事实由写入方显式声明
    assert not t.columns["type"].nullable


# ---------------------------------------------------------------- Test C


def test_c_identity_duplicate_channel_external_rejected(db) -> None:
    user = _make_user(db)
    channel = ChannelModel(name="wecom", type="wecom")
    db.add(channel)
    db.flush()
    db.add(IdentityModel(user_id=user.id, channel_id=channel.id, external_id="ext-1"))
    db.flush()

    with pytest.raises(IntegrityError):
        db.execute(
            insert(IdentityModel).values(
                user_id=user.id, channel_id=channel.id, external_id="ext-1"
            )
        )


def test_c_connection_duplicate_channel_agent_rejected(db) -> None:
    channel = ChannelModel(name="wecom", type="wecom")
    agent = AgentModel(name="helper", type="acp")
    db.add_all([channel, agent])
    db.flush()
    db.add(ConnectionModel(channel_id=channel.id, agent_id=agent.id))
    db.flush()

    with pytest.raises(IntegrityError):
        db.execute(
            insert(ConnectionModel).values(channel_id=channel.id, agent_id=agent.id)
        )


def test_c_interaction_duplicate_channel_external_rejected(db) -> None:
    """10.5.2：Interaction Identity 唯一约束（与 identity/connection 同纪律）。
    同 Channel + 同 external_interaction_id → DB conflict；跨 Channel 共存合法。"""
    user = _make_user(db)
    channel_a = ChannelModel(name="ch-a", type="wecom")
    channel_b = ChannelModel(name="ch-b", type="wecom")
    db.add_all([channel_a, channel_b])
    db.flush()
    db.add(InteractionModel(
        channel_id=channel_a.id, direction="inbound",
        external_interaction_id="msg-1"))
    db.commit()  # 基线持久化（后续 rollback 不受影响）

    with pytest.raises(IntegrityError):
        db.add(InteractionModel(
            channel_id=channel_a.id, direction="inbound",
            external_interaction_id="msg-1"))
        db.flush()
    db.rollback()  # 冲突后 session 必须先回滚才能继续使用

    # 跨 Channel 的同名 external id 不冲突（Channel Scope 内解释）
    db.add(InteractionModel(
        channel_id=channel_b.id, direction="inbound",
        external_interaction_id="msg-1"))
    db.flush()


def _make_user(db) -> UserModel:
    user = UserModel(name="zhang-san")
    db.add(user)
    db.flush()
    return user


# ---------------------------------------------------------------- Test D


def test_d_fk_rejects_identity_with_missing_user(db) -> None:
    channel = ChannelModel(name="wecom", type="wecom")
    db.add(channel)
    db.flush()
    db.add(IdentityModel(user_id=99999, channel_id=channel.id, external_id="ext-x"))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        db.flush()


def test_d_fk_rejects_session_with_missing_connection(db) -> None:
    user = _make_user(db)
    channel = ChannelModel(name="wecom", type="wecom")
    db.add(channel)
    db.flush()
    identity = IdentityModel(
        user_id=user.id, channel_id=channel.id, external_id="ext-1"
    )
    db.add(identity)
    db.flush()
    db.add(SessionModel(identity_id=identity.id, channel_id=channel.id, connection_id=424242))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        db.flush()


# ---------------------------------------------------------------- Test E


def _build_full_chain(db) -> dict:
    """构造最小真实数据：完整链路一次建立。"""
    user = UserModel(name="zhang-san")
    channel = ChannelModel(name="wecom", type="wecom")
    agent = AgentModel(name="helper", type="acp", endpoint="https://agent.example/acp")
    db.add_all([user, channel, agent])
    db.flush()

    connection = ConnectionModel(
        channel_id=channel.id, agent_id=agent.id, name="wecom-helper"
    )
    identity = IdentityModel(
        user_id=user.id, channel_id=channel.id,
        external_id="wecom:zhangsan", display_name="张三",
    )
    db.add_all([connection, identity])
    db.flush()

    session = SessionModel(
        identity_id=identity.id, channel_id=channel.id,
        connection_id=connection.id, agent_id=agent.id,
        external_session_id="agent-sess-1",
        external_conversation_id="wecom-chat-100",
    )
    db.add(session)
    db.flush()

    interaction = InteractionModel(
        channel_id=channel.id, session_id=session.id, direction="inbound",
        external_interaction_id="msg-9001",
        request_payload={"text": "你好"},
        response_payload={"text": "你好，我是助手"},
    )
    db.add(interaction)
    db.flush()

    activity = ActivityModel(
        interaction_id=interaction.id, session_id=session.id,
        type="agent_invocation", stage="agent", status="succeeded",
        message="agent responded",
    )
    db.add(activity)
    db.commit()

    return {
        "user": user, "channel": channel, "agent": agent,
        "connection": connection, "identity": identity,
        "session": session, "interaction": interaction, "activity": activity,
    }


def test_e_full_chain_persists_and_reads_back(db) -> None:
    chain = _build_full_chain(db)

    session = db.execute(
        select(SessionModel).where(SessionModel.id == chain["session"].id)
    ).scalar_one()
    assert session.identity_id == chain["identity"].id
    assert session.connection_id == chain["connection"].id
    assert session.agent_id == chain["agent"].id

    activity = db.execute(
        select(ActivityModel).where(ActivityModel.id == chain["activity"].id)
    ).scalar_one()
    assert activity.interaction_id == chain["interaction"].id
    assert activity.session_id == chain["session"].id

    interaction = db.get(InteractionModel, chain["interaction"].id)
    assert interaction.processing_status == "pending"
    assert interaction.delivery_status == "pending"
    assert interaction.retryable is False


# ---------------------------------------------------------------- Test F


def test_f_context_isolation(db) -> None:
    """identity × external_conversation_id 的组合不得被错误合并。"""
    user = UserModel(name="zhang-san")
    other = UserModel(name="li-si")
    channel = ChannelModel(name="wecom", type="wecom")
    db.add_all([user, other, channel])
    db.flush()

    identity_a = IdentityModel(
        user_id=user.id, channel_id=channel.id, external_id="ext-A"
    )
    identity_b = IdentityModel(
        user_id=other.id, channel_id=channel.id, external_id="ext-B"
    )
    db.add_all([identity_a, identity_b])
    db.flush()

    s1 = SessionModel(
        identity_id=identity_a.id, channel_id=channel.id,
        external_conversation_id="conv-A",
    )
    s2 = SessionModel(
        identity_id=identity_a.id, channel_id=channel.id,
        external_conversation_id="conv-B",
    )
    s3 = SessionModel(
        identity_id=identity_b.id, channel_id=channel.id,
        external_conversation_id="conv-A",
    )
    db.add_all([s1, s2, s3])
    db.commit()

    # conv-A 上有两个 Session：张三和李四各一条，互相独立
    conv_a = db.execute(
        select(SessionModel).where(SessionModel.external_conversation_id == "conv-A")
    ).scalars().all()
    assert {s.identity_id for s in conv_a} == {identity_a.id, identity_b.id}

    # 同一 identity 的两个 conversation 是两条独立 Session
    mine = db.execute(
        select(SessionModel).where(SessionModel.identity_id == identity_a.id)
    ).scalars().all()
    assert {s.external_conversation_id for s in mine} == {"conv-A", "conv-B"}


# ---------------------------------------------------------------- 附加：边界


def test_mappers_and_repositories_started_by_first_slice() -> None:
    """5.3 时代『mappers/repositories 保持空壳』的断言已随 5.4 翻转：
    First Vertical Slice（User/Channel/Agent）正式启用 mappers 与 adapter。
    其余五个实体仍未实现，由 tests/test_repositories.py 的防漂移测试守护。
    """
    from pathlib import Path

    persistence_dir = Path(__file__).resolve().parents[1] / "app" / "persistence"
    for sub in ("mappers", "repositories"):
        files = sorted(p.name for p in (persistence_dir / sub).glob("*.py"))
        assert "user.py" in files, f"{sub} 未按 First Vertical Slice 落地 user.py"
        assert "channel.py" in files, f"{sub} 未按 First Vertical Slice 落地 channel.py"
        assert "agent.py" in files, f"{sub} 未按 First Vertical Slice 落地 agent.py"
