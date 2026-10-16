"""M6.6-C3-CP3-G — Bootstrap 运行时：原子建立 / 群聊拒绝 / 并发 / 重放 / 落点。

被守护的不变量（ADR-12 §6~§14、CP3-F §10~§17）：

    G8   群聊 ⇒ 不消费 Arm / 不建 Identity / 不建 Grant / 不新造失败码；Arm 保持 armed
    G9   Identity 恒 `user_id = NULL`；**不创建 ACP User**
    G10  Identity + Grant + Arm 消费在同一短事务；回滚 ⇒ Arm 回到 armed
    G11  CAS 赢家唯一；输家回读复用，**不**变成 IDENTITY_RESOLUTION_FAILED
    G12  重放不产生第二套 Identity / Grant
    G13  三个 Adapter 显式声明 `chat_type`
    G14  Bootstrap 只在 `Worker.process` 注入（三条 Trigger 同一边界）
    G15  未 Arm 路径与今天**逐字节等价**（IDENTITY_RESOLUTION_FAILED）
"""

import json
import threading
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.adapters.agents.local import LocalAgentAdapter
from app.adapters.agents.registry import MultiAgentAdapter
from app.adapters.channels.local import LocalChannelAdapter
from app.adapters.channels.registry import MultiChannelAdapter
from app.adapters.channels.wecom import _inbound_from_robot_json
from app.adapters.channels.wecom_bot import inbound_from_callback
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.bootstrap import (
    ALREADY,
    APPLIED,
    NOT_APPLICABLE,
    BootstrapCoordinator,
)
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.errors import IdentityResolutionFailedError
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import PersistingCoordinator
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.application.worker import Worker
from app.domain.agent import Agent
from app.domain.bootstrap_arm import STATUS_ARMED, STATUS_CONSUMED, BootstrapArm
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.inbound_interaction import (
    CHAT_TYPE_GROUP,
    CHAT_TYPE_METADATA_KEY,
    CHAT_TYPE_SINGLE,
    InboundInteraction,
)
from app.domain.user import User
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

EXTERNAL_ID = "zhangsan"          # local：conversation == identity
CONVERSATION = EXTERNAL_ID
GROUP_CONVERSATION = "chatid-group-1"


def _uow(engine, tenant_id=1):
    return SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        tenant_id=tenant_id,
    )


def _factory(engine):
    return lambda: _uow(engine)


def _seed(engine) -> tuple[int, int, int, int]:
    """user / channel / agent / connection → (user_id, channel_id, agent_id, connection_id)。"""
    with _uow(engine) as uow:
        admin = uow.users.create(User(name="runtime-admin"))
        channel = uow.channels.create(Channel(name="rt-ch", type="local"))
        agent = uow.agents.create(Agent(name="rt-agent", type="local"))
        assert admin.id and channel.id and agent.id
        connection = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="rt-path"))
        assert connection.id
        uow.commit()
        return admin.id, channel.id, agent.id, connection.id


def _arm(engine, channel_id: int, connection_id: int, initiator: int,
         ttl_seconds: int = 600) -> int:
    with _uow(engine) as uow:
        arm = uow.bootstrap_arms.arm(
            BootstrapArm(channel_id=channel_id, connection_id=connection_id,
                         initiator_user_id=initiator),
            ttl_seconds=ttl_seconds)
        uow.commit()
    assert arm.id is not None
    return arm.id


def _chain(engine, outbox_path):
    """完整七段 Runtime + local 入站/出站 Adapter + **带 Bootstrap 的 Worker**。"""
    f = _factory(engine)
    channel_adapter = MultiChannelAdapter(
        {"local": LocalChannelAdapter(outbox_path=outbox_path)},
        uow_factory=f,
    )
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
    worker = Worker(runtime=runtime, phases=phases, uow_factory=f,
                    bootstrap=BootstrapCoordinator(uow_factory=f))
    return phases, worker


def _inbound(channel_id: int, *, msg_id: str = "m-1",
             conversation: str = CONVERSATION, chat_type: str | None = CHAT_TYPE_SINGLE,
             identity: str = EXTERNAL_ID) -> InboundInteraction:
    metadata = None if chat_type is None else {CHAT_TYPE_METADATA_KEY: chat_type}
    return InboundInteraction(
        channel_id=channel_id,
        external_interaction_id=msg_id,
        external_conversation_id=conversation,
        external_identity_id=identity,
        content="hello",
        metadata=metadata,
    )


def _counts(engine) -> tuple[int, int, int]:
    with _uow(engine) as uow:
        users = uow.session.execute(
            text("SELECT count(*) FROM users")).scalar_one()
        identities = uow.session.execute(
            text("SELECT count(*) FROM identities")).scalar_one()
        grants = uow.session.execute(
            text("SELECT count(*) FROM connection_conversations")).scalar_one()
    return int(users), int(identities), int(grants)


def _interaction(engine) -> dict:
    with _uow(engine) as uow:
        row = uow.session.execute(
            text("SELECT processing_status, delivery_status, failure_stage, "
                 "failure_code FROM interactions ORDER BY id")).fetchall()
    return {
        "count": len(row),
        "rows": [tuple(r) for r in row],
    }


# ============================================================ A. 未 Arm（G15）


def test_unarmed_path_is_unchanged(db_engine, tmp_path) -> None:
    """无 Arm ⇒ 与今天逐字节等价：IDENTITY_RESOLUTION_FAILED，零 Identity / Grant。"""
    _admin, channel_id, _agent, _conn = _seed(db_engine)
    phases, worker = _chain(db_engine, tmp_path / "outbox.jsonl")

    interaction_id = phases.create_pending(_inbound(channel_id))
    # Worker 落定终态后**原样向上抛**（既有语义：异常通道即执行事实通道）
    with pytest.raises(IdentityResolutionFailedError):
        worker.process(interaction_id)

    users, identities, grants = _counts(db_engine)
    assert identities == 0 and grants == 0
    assert users == 1                       # 只有种子 admin；未创建任何 ACP User
    rows = _interaction(db_engine)["rows"]
    assert rows[0][0] == "failed"
    assert rows[0][2] == "identity_resolution"
    assert rows[0][3] == "IDENTITY_RESOLUTION_FAILED"


# ============================================================ B. Bootstrap 建立（G9/G10）


def test_bootstrap_establishes_identity_and_grant(db_engine, tmp_path) -> None:
    admin, channel_id, _agent, connection_id = _seed(db_engine)
    phases, worker = _chain(db_engine, tmp_path / "outbox.jsonl")
    arm_id = _arm(db_engine, channel_id, connection_id, admin)

    interaction_id = phases.create_pending(_inbound(channel_id))
    assert worker.process(interaction_id) is True

    # Identity：存在、user_id = NULL（绝不等于 initiator）
    with _uow(db_engine) as uow:
        identity = uow.identities.find_by_channel_and_external_id(
            channel_id, EXTERNAL_ID)
        grant = uow.connection_conversations.find_by_binding(
            channel_id, CONVERSATION, connection_id)
        arm = uow.bootstrap_arms.get_by_id(arm_id)

    assert identity is not None and identity.user_id is None
    assert identity.user_id != admin
    assert grant is not None and grant.connection_id == connection_id
    assert arm is not None and arm.status == STATUS_CONSUMED
    assert arm.consumed_by_interaction_id == interaction_id
    assert arm.consumed_external_identity_id == EXTERNAL_ID

    # 未创建 ACP User（ADR-12 §6）
    users, identities, grants = _counts(db_engine)
    assert users == 1 and identities == 1 and grants == 1

    # 管线照常跑完：First Success（processing × delivery 双状态）
    rows = _interaction(db_engine)["rows"]
    assert rows[0][0] == "succeeded" and rows[0][1] == "delivered"


def test_bootstrap_outcome_constants(db_engine, tmp_path) -> None:
    admin, channel_id, _agent, connection_id = _seed(db_engine)
    phases, worker = _chain(db_engine, tmp_path / "outbox.jsonl")
    _arm(db_engine, channel_id, connection_id, admin)
    bootstrap = worker._bootstrap  # noqa: SLF001  （测试直读注入的组件）

    assert bootstrap.ensure(_inbound(channel_id), 1) == APPLIED
    # 已建立 ⇒ 不消费（Arm 已被消费 ⇒ 无 active ⇒ NOT_APPLICABLE）
    assert bootstrap.ensure(_inbound(channel_id, msg_id="m-2"), 2) == NOT_APPLICABLE


def test_second_message_reuses_identity_and_grant(db_engine, tmp_path) -> None:
    """同一会话的第二条消息：复用既有 Identity / Grant，不产生第二套。"""
    admin, channel_id, _agent, connection_id = _seed(db_engine)
    phases, worker = _chain(db_engine, tmp_path / "outbox.jsonl")
    _arm(db_engine, channel_id, connection_id, admin)

    worker.process(phases.create_pending(_inbound(channel_id, msg_id="m-1")))
    worker.process(phases.create_pending(_inbound(channel_id, msg_id="m-2")))

    _users, identities, grants = _counts(db_engine)
    assert identities == 1 and grants == 1


# ============================================================ C. 群聊（G8）


def test_group_chat_does_not_consume_arm(db_engine, tmp_path) -> None:
    """群聊 ⇒ 不消费、不建 Identity、不建 Grant、不新造失败码；Arm 保持 armed。"""
    admin, channel_id, _agent, connection_id = _seed(db_engine)
    phases, worker = _chain(db_engine, tmp_path / "outbox.jsonl")
    arm_id = _arm(db_engine, channel_id, connection_id, admin)
    bootstrap = worker._bootstrap  # noqa: SLF001

    outcome = bootstrap.ensure(
        _inbound(channel_id, conversation=GROUP_CONVERSATION,
                 chat_type=CHAT_TYPE_GROUP, identity="lisi"), 99)

    assert outcome == NOT_APPLICABLE
    _users, identities, grants = _counts(db_engine)
    assert identities == 0 and grants == 0
    with _uow(db_engine) as uow:
        arm = uow.bootstrap_arms.get_by_id(arm_id)
    assert arm is not None and arm.status == STATUS_ARMED      # 保持 armed


def test_missing_chat_type_is_rejected(db_engine, tmp_path) -> None:
    """Adapter 未声明会话形态 ⇒ 不消费（Core 不做启发式推断）。"""
    admin, channel_id, _agent, connection_id = _seed(db_engine)
    _phases, worker = _chain(db_engine, tmp_path / "outbox.jsonl")
    _arm(db_engine, channel_id, connection_id, admin)

    outcome = worker._bootstrap.ensure(  # noqa: SLF001
        _inbound(channel_id, chat_type=None), 99)

    assert outcome == NOT_APPLICABLE
    _users, identities, grants = _counts(db_engine)
    assert identities == 0 and grants == 0


def test_group_chat_with_existing_grant_still_works(db_engine, tmp_path) -> None:
    """「Bootstrap 不支持群」≠「ACP 不支持群」：既有显式 Grant 的群聊照常运行。"""
    admin, channel_id, _agent, connection_id = _seed(db_engine)
    with _uow(db_engine) as uow:
        from app.domain.connection_conversation import ConnectionConversation
        from app.domain.identity import Identity

        # 群成员身份 + 群会话授权都由**既有管理面路径**显式建立（与 Bootstrap 无关）
        uow.identities.create(Identity(
            user_id=None, channel_id=channel_id, external_id="lisi"))
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel_id,
            external_conversation_id=GROUP_CONVERSATION,
            connection_id=connection_id))
        uow.commit()

    phases, worker = _chain(db_engine, tmp_path / "outbox.jsonl")
    interaction_id = phases.create_pending(
        _inbound(channel_id, conversation=GROUP_CONVERSATION,
                 chat_type=CHAT_TYPE_GROUP, identity="lisi"))
    assert worker.process(interaction_id) is True

    rows = _interaction(db_engine)["rows"]
    assert rows[0][0] == "succeeded" and rows[0][1] == "delivered"


# ============================================================ D. 并发（G11）


def test_concurrent_first_messages_same_conversation(isolated_db_engine,
                                                      tmp_path) -> None:
    """N 条并发首条消息（同一会话）：恰好 1 Identity + 1 Grant，全部到达终态。"""
    engine = isolated_db_engine
    admin, channel_id, _agent, connection_id = _seed(engine)
    _arm(engine, channel_id, connection_id, admin)
    phases, worker = _chain(engine, tmp_path / "outbox.jsonl")

    ids = [phases.create_pending(_inbound(channel_id, msg_id=f"m-{i}"))
           for i in (1, 2)]

    outcomes: list[bool] = [False, False]
    errors: list[BaseException | None] = [None, None]

    def run(index: int) -> None:
        try:
            outcomes[index] = worker.process(ids[index])
        except BaseException as exc:  # noqa: BLE001  Worker 原样上抛 typed error
            errors[index] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # ① Bootstrap 不变量：至多一次授权建立
    _users, identities, grants = _counts(engine)
    assert identities == 1, "并发不得产生第二条 Identity"
    assert grants == 1, "并发不得产生第二条 Grant"
    with _uow(engine) as uow:
        assert uow.bootstrap_arms.find_active_by_channel(channel_id) is None

    # ② 至少一条完整走完管线（First Success 可达）
    assert any(outcomes), f"没有任何一条取得执行权：{errors}"

    # ③ 两条消息都到达终态（不出现「无终态」的悬挂行）
    rows = _interaction(engine)["rows"]
    assert len(rows) == 2
    assert all(row[0] in ("succeeded", "failed") for row in rows)

    # ④ 登记（不断言）：并发第二条消息若落 `SESSION_RESOLUTION_FAILED`，那是
    #    **既有 M3 缺陷**（Session 唯一约束冲突未回读复用），属 CP-4 修复范围，
    #    与 Bootstrap 无关 —— 本测试只保证 Bootstrap 侧不产生第二套授权。


def test_cas_loser_reuses_winner_facts(db_engine, tmp_path) -> None:
    """CAS 输家：回读复用赢家成果（ALREADY），而不是回落到失败。"""
    admin, channel_id, _agent, connection_id = _seed(db_engine)
    phases, worker = _chain(db_engine, tmp_path / "outbox.jsonl")
    _arm(db_engine, channel_id, connection_id, admin)
    bootstrap = worker._bootstrap  # noqa: SLF001

    # 赢家：直接消费 Arm 并建好事实（模拟另一条消息抢先提交）
    with _uow(db_engine) as uow:
        arm = uow.bootstrap_arms.find_active_by_channel(channel_id)
        assert arm is not None and arm.id is not None
        assert uow.bootstrap_arms.consume(
            arm.id, interaction_id=100, external_identity_id=EXTERNAL_ID,
            external_conversation_id=CONVERSATION) is True
        from app.domain.connection_conversation import ConnectionConversation
        from app.domain.identity import Identity

        uow.identities.find_or_create(Identity(
            user_id=None, channel_id=channel_id, external_id=EXTERNAL_ID))
        uow.connection_conversations.find_or_create(ConnectionConversation(
            channel_id=channel_id, external_conversation_id=CONVERSATION,
            connection_id=connection_id))
        uow.commit()

    # 输家：Arm 已消费 ⇒ 事实已存在 ⇒ 走正常管线（授权已建立）
    outcome = bootstrap.ensure(_inbound(channel_id, msg_id="m-9"), 101)
    assert outcome == NOT_APPLICABLE        # 无 active Arm ⇒ 不消费
    _users, identities, grants = _counts(db_engine)
    assert identities == 1 and grants == 1  # 未重复建立


# ============================================================ E. 重放（G12）


def test_replay_creates_nothing_new(db_engine, tmp_path) -> None:
    """重放：Arm 是一次性**授权建立**，不是一次性消息处理；重跑不产生第二套。"""
    admin, channel_id, _agent, connection_id = _seed(db_engine)
    phases, worker = _chain(db_engine, tmp_path / "outbox.jsonl")
    _arm(db_engine, channel_id, connection_id, admin)

    interaction_id = phases.create_pending(_inbound(channel_id))
    assert worker.process(interaction_id) is True
    # 同一条 interaction 再次执行：claim 失败 ⇒ 绝不执行 Runtime
    assert worker.process(interaction_id) is False

    _users, identities, grants = _counts(db_engine)
    assert identities == 1 and grants == 1
    with _uow(db_engine) as uow:
        assert uow.bootstrap_arms.find_active_by_channel(channel_id) is None


# ============================================================ F. Adapter chat_type（G13）


def test_local_adapter_declares_single_chat(db_engine, tmp_path) -> None:
    _admin, channel_id, _agent, _conn = _seed(db_engine)
    with _uow(db_engine) as uow:
        channel = uow.channels.get_by_id(channel_id)
    assert channel is not None

    adapter = LocalChannelAdapter(outbox_path=tmp_path / "o.jsonl")
    inbound = adapter.parse_message(
        channel, signature="", timestamp="", nonce="",
        body=json.dumps({"external_id": EXTERNAL_ID, "text": "hi"}))

    assert inbound is not None
    assert inbound.metadata is not None
    assert inbound.metadata[CHAT_TYPE_METADATA_KEY] == CHAT_TYPE_SINGLE


def test_wecom_bot_adapter_declares_actual_chattype() -> None:
    single = inbound_from_callback({
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r1"},
        "body": {"msgid": "m1", "msgtype": "text", "chattype": "single",
                 "from": {"userid": EXTERNAL_ID}, "text": {"content": "hi"}},
    }, channel_id=1)
    group = inbound_from_callback({
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r2"},
        "body": {"msgid": "m2", "msgtype": "text", "chattype": "group",
                 "chatid": GROUP_CONVERSATION,
                 "from": {"userid": "lisi"}, "text": {"content": "hi"}},
    }, channel_id=1)

    assert single is not None and single.metadata is not None
    assert single.metadata[CHAT_TYPE_METADATA_KEY] == CHAT_TYPE_SINGLE
    assert group is not None and group.metadata is not None
    assert group.metadata[CHAT_TYPE_METADATA_KEY] == CHAT_TYPE_GROUP
    assert group.external_conversation_id == GROUP_CONVERSATION


def test_wecom_adapter_declares_single_chat() -> None:
    inbound = _inbound_from_robot_json({
        "msgid": "m1", "msgtype": "text", "chattype": "single",
        "from": {"userid": EXTERNAL_ID}, "text": {"content": "hi"},
        "response_url": "https://example.invalid/reply",
    }, channel_id=1)

    assert inbound is not None and inbound.metadata is not None
    assert inbound.metadata[CHAT_TYPE_METADATA_KEY] == CHAT_TYPE_SINGLE
    # 回信句柄不受影响（既有 delivery context 保留）
    assert "response_url" in inbound.metadata


# ============================================================ G. 落点（G14）


def test_bootstrap_is_injected_only_in_worker() -> None:
    """G14：三条 Trigger 汇聚到 Worker ⇒ Bootstrap 只在 Worker 内注入一次。"""
    backend = Path(__file__).resolve().parents[1]
    app_dir = backend / "app"

    worker_src = (app_dir / "application" / "worker.py").read_text(encoding="utf-8")
    main_src = (app_dir / "main.py").read_text(encoding="utf-8")

    assert "bootstrap: BootstrapCoordinator | None = None" in worker_src
    assert "self._bootstrap.ensure(inbound, interaction_id)" in worker_src
    assert "BootstrapCoordinator(uow_factory=runtime_uow_factory)" in main_src
    assert "bootstrap=bootstrap" in main_src

    # 其它 Trigger / 编排层**不得**触碰 Bootstrap（否则出现 transport-specific 分叉）
    for rel in ("application/channel_inbound.py", "application/driver.py",
                "application/coordinator.py", "application/runtime.py",
                "application/identity_resolution.py",
                "application/access_authorization.py"):
        src = (app_dir / rel).read_text(encoding="utf-8")
        assert "BootstrapCoordinator" not in src, f"{rel} 不得引用 Bootstrap"


def test_worker_without_bootstrap_keeps_legacy_behavior(db_engine, tmp_path) -> None:
    """`bootstrap=None`（未注入）⇒ 行为与注入前一致（未 Arm 即 Fail Closed）。"""
    _admin, channel_id, _agent, _conn = _seed(db_engine)
    f = _factory(db_engine)
    channel_adapter = MultiChannelAdapter(
        {"local": LocalChannelAdapter(outbox_path=tmp_path / "o.jsonl")},
        uow_factory=f)
    runtime = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(f),
        access_authorization=AccessAuthorizationExecutor(f),
        routing=RoutingExecutor(f),
        session_resolution=SessionResolutionExecutor(f),
        session_creation=SessionCreationExecutor(f),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=MultiAgentAdapter({"local": LocalAgentAdapter()},
                                            uow_factory=f),
            uow_factory=f),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )
    phases = PersistingCoordinator(inner=runtime, uow_factory=f)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=f)   # 无 bootstrap

    with pytest.raises(IdentityResolutionFailedError):
        worker.process(phases.create_pending(_inbound(channel_id)))

    _users, identities, grants = _counts(db_engine)
    assert identities == 0 and grants == 0
    rows = _interaction(db_engine)["rows"]
    assert rows[0][3] == "IDENTITY_RESOLUTION_FAILED"


# ============================================================ H. 过期（G7）


def test_expired_arm_does_not_bootstrap(db_engine, tmp_path) -> None:
    admin, channel_id, _agent, connection_id = _seed(db_engine)
    phases, worker = _chain(db_engine, tmp_path / "outbox.jsonl")
    arm_id = _arm(db_engine, channel_id, connection_id, admin, ttl_seconds=-10)

    with pytest.raises(IdentityResolutionFailedError):
        worker.process(phases.create_pending(_inbound(channel_id)))

    _users, identities, grants = _counts(db_engine)
    assert identities == 0 and grants == 0
    with _uow(db_engine) as uow:
        arm = uow.bootstrap_arms.get_by_id(arm_id)
    assert arm is not None and arm.status == STATUS_ARMED      # 过期不写状态
    rows = _interaction(db_engine)["rows"]
    assert rows[0][3] == "IDENTITY_RESOLUTION_FAILED"          # 既有失败码
