"""Step 10.5.1 —— Interaction Identity & Idempotency Boundary Audit（审计步，生产代码 0 行改动）。

把审计结论固化为可回归断言（审计步的唯一交付物 = 文档 + 本快照测试）：

A  external_interaction_id 的完整生命周期（七跳全部真实存在；结果侧空白）；
B  DB 约束现状（**10.5.2 物化后翻转**：UNIQUE(channel_id, external_interaction_id)
   已在 schema；项目早有 channel 作用域复合唯一纪律）；
C  Interaction 创建边界（唯一构造点 = Runtime 调用边界；协议失败不产生行、语义失败产生行）；
D  duplicate create（**10.5.2 后翻转**：同 Channel 同 MsgId → DB conflict，
   不产生第二行、不产生第二次副作用）；
E  四状态语义（终态不可逆 / 后续执行不改写既有行 / retryable 非控制字段）；
F  同一 external id 跨 Channel 是可能的（10.5.2 后成为身份纪律的核心）；
G  Agent 执行中再进事件：PROCESSING 可观察但无任何 in-flight 保护；
H  无幂等基础设施偷渡；Runtime 边界回归；契约文档成文。

历史注记：B1/B3/D1/E1/E2/G1 原为 10.5.1 时的「缺口存在」快照；10.5.2
Interaction Identity Materialization 落地后按约定就地翻转（10.4.1→10.4.2
同一模式），原始状态见 docs/step-10.5.1。

本步不设计 Redis / 锁 / 队列 / 幂等服务 —— 只让真实状态语义先出来。
"""

import ast
from dataclasses import fields
from pathlib import Path

import pytest
from sqlalchemy import UniqueConstraint, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SASession, sessionmaker

from app.adapters.agents.acp import ACPAdapter
from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.channel_inbound import ChannelInboundService
from app.application.delivery import DeliveryExecutor
from app.application.errors import AgentDispatchFailedError, IdentityResolutionFailedError
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import PersistingCoordinator
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.agent import Agent
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.models.interaction import InteractionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.channel_inbound import InboundFormatError

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
APPLICATION = APP / "application"
REPO_ROOT = BACKEND.parent
DOC = REPO_ROOT / "docs" / "step-10.5.1-interaction-identity-idempotency-audit.md"

DEAD_ENDPOINT = "http://127.0.0.1:1"  # 无监听端口 → transport 失败


# ================================================================== 测试底座


class _CountingAgentAdapter:
    """记录 Agent 被调用次数（副作用计数的直接证据）。"""

    def __init__(self) -> None:
        self.calls = 0

    def send(self, request, connection) -> AgentResponse:
        self.calls += 1
        return AgentResponse(content=f"echo:{request.content}",
                             external_session_id="agent-session-1")


class _RecordingChannelAdapter:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


class _ReentrantAgentAdapter:
    """模拟「Agent 执行中又有事件进入」：首次 send 内部再触发一次完整 Runtime 执行。

    `AgentDispatchExecutor` 在退出 UoW 之后才调用 `adapter.send`（外部 I/O 不占
    事务），因此这里重入一次完整执行是安全的。10.5.2 后同 MsgId 会在 DB 层
    conflict（身份物化），故重入事件使用**不同 MsgId**——隔离
    「in-flight 无保护」与「身份 conflict」两个事实。
    """

    def __init__(self, holder: dict, channel_id: int,
                 first_id: str, reentry_id: str, rows_fn) -> None:
        self._holder = holder
        self._channel_id = channel_id
        self._first_id = first_id
        self._reentry_id = reentry_id
        self._rows_fn = rows_fn
        self.calls = 0
        self.status_during_retry: str | None = None
        self._reentered = False

    def send(self, request, connection) -> AgentResponse:
        self.calls += 1
        if not self._reentered:
            self._reentered = True
            self.status_during_retry = self._rows_fn()[-1].processing_status
            self._holder["runtime"].execute(
                _inbound(self._channel_id, self._reentry_id))
        return AgentResponse(content="echo", external_session_id="agent-session-1")


class _RaisingInboundAdapter:
    """入站协议失败替身（parse_message 抛错）。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def verify_handshake(self, channel, **kwargs) -> str:  # noqa: ARG002
        return "echostr"

    def parse_message(self, channel, **kwargs):  # noqa: ARG002
        raise self._exc


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine, endpoint: str | None = None) -> dict[str, int]:
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status="active",
            config={"endpoint": endpoint or "http://agent.internal",
                    "timeout_s": 5.0},
            credential_ref="cred-a"))
        # Implementation Package 2 / Step 2：显式授权该会话（default-deny 前提）
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="user-A",
            connection_id=path.id))
        uow.commit()
        return {"channel": channel.id, "agent": agent.id}


def _seed_two_channels(db_engine) -> dict[str, int]:
    """两个 Channel 各有独立 Identity / Connection（Routing 单 active 前提成立）。"""
    with _uow_factory(db_engine)() as uow:
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        out: dict[str, int] = {}
        for key, ch_name, ext, user_name, conversation in (
            ("channel_a", "ch-a", "wx_a", "alice", "conv-a"),
            ("channel_b", "ch-b", "wx_b", "bob", "conv-b"),
        ):
            user = uow.users.create(User(name=user_name))
            channel = uow.channels.create(Channel(name=ch_name, type="wecom"))
            uow.identities.create(Identity(
                user_id=user.id, channel_id=channel.id, external_id=ext))
            path = uow.connections.create(Connection(
                channel_id=channel.id, agent_id=agent.id, name=f"path-{key}",
                status="active",
                config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
                credential_ref="cred"))
            # Implementation Package 2 / Step 2：显式授权该会话（default-deny 前提）
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=channel.id, external_conversation_id=conversation,
                connection_id=path.id))
            out[key] = channel.id
        uow.commit()
        return out


def _wrapped_runtime(db_engine, agent_adapter=None, channel_adapter=None):
    uow = _uow_factory(db_engine)
    inner = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=agent_adapter or _CountingAgentAdapter(),
            uow_factory=uow),
        delivery=DeliveryExecutor(
            channel_adapter=channel_adapter or _RecordingChannelAdapter()),
    )
    return PersistingCoordinator(inner=inner, uow_factory=_uow_factory(db_engine))


def _inbound(channel_id: int, interaction: str = "i-1",
             conversation: str = "user-A", identity: str = "wx_a",
             content: str = "hello") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id=interaction,
        external_conversation_id=conversation,
        external_identity_id=identity, content=content)


def _interaction_rows(db_engine) -> list[InteractionModel]:
    with SASession(db_engine) as s:
        return list(s.execute(select(InteractionModel)
                              .order_by(InteractionModel.id)).scalars().all())


def _py_files(root: Path):
    return sorted(p for p in root.rglob("*.py"))


# ================================================================== A. 身份生命周期


def test_a1_external_interaction_id_lifecycle_hops_are_real() -> None:
    """七跳全部真实存在：wire MsgId → 契约字段 → 落库列（+ payload 双写）。"""
    wecom = (APP / "adapters" / "channels" / "wecom.py").read_text(encoding="utf-8")
    assert "MsgId" in wecom
    assert "external_interaction_id=msg_id" in wecom

    domain = (APP / "domain" / "inbound_interaction.py").read_text(encoding="utf-8")
    assert "external_interaction_id: str" in domain

    recording = (APPLICATION / "interaction_recording.py").read_text(encoding="utf-8")
    assert "external_interaction_id=inbound.external_interaction_id" in recording
    # 双写：列 + request_payload 快照（12.9.4.3 起快照形状收口到
    # app.domain.interaction_snapshot 单一事实源，故字面量在该模块中）
    snapshot = (APP / "domain" / "interaction_snapshot.py").read_text(encoding="utf-8")
    assert '"external_interaction_id": inbound.external_interaction_id' in snapshot

    mapper = (APP / "persistence" / "mappers"
              / "interaction_record.py").read_text(encoding="utf-8")
    assert "external_interaction_id=row.external_interaction_id" in mapper


def test_a2_inbound_interaction_is_not_a_persisted_identity() -> None:
    """8.1 回归：InboundInteraction 无 id / status / created_at（不落库的值对象）。"""
    names = {f.name for f in fields(InboundInteraction)}
    assert names == {
        "channel_id", "external_interaction_id", "external_conversation_id",
        "external_identity_id", "content", "metadata",
    }


def test_a3_outbound_response_carries_no_interaction_identity() -> None:
    """出站侧不含 external_interaction_id —— 投递出去的消息无法回链来源事件。"""
    names = {f.name for f in fields(OutboundResponse)}
    assert names == {"channel_id", "external_conversation_id", "content",
                     "metadata", "artifacts"}
    assert "external_interaction_id" not in names


def test_a4_response_payload_writer_appears_with_10_8_4() -> None:
    """（10.8.4 翻转）结果侧空白已由 Result 接线填补：wrapper 成为写入方。
    原断言（全树仅 定义/映射 三处、无写入方）关闭——缺口已修。"""
    recording = (APPLICATION / "interaction_recording.py").read_text(encoding="utf-8")
    assert "response_payload" in recording           # 原「零写入方」缺口已关闭
    hits = {p.relative_to(APP).as_posix()
            for p in _py_files(APP)
            if "response_payload" in p.read_text(encoding="utf-8")}
    assert hits == {
        "domain/interaction_record.py",
        "persistence/models/interaction.py",
        "persistence/mappers/interaction_record.py",
        "application/interaction_recording.py",       # 10.8.4 写入方
        "repositories/interaction_record.py",         # 10.8.4 Port 参数
        "persistence/repositories/interaction_record.py",  # 10.8.4 Adapter 落列
        "api/management/interactions.py",             # 13.1 只读回显（docstring）
        "schemas/interactions.py",                    # 13.1 只读回显（响应字段）
        # P2：投递恢复从**已落库**的 response_payload 重建出站信封（只读引用）
        "application/delivery.py",
        "application/delivery_recovery.py",
    }


# ================================================================== B. DB 约束现状


def test_b1_interactions_identity_unique_constraint_is_materialized() -> None:
    """（10.5.2 翻转）UNIQUE(channel_id, external_interaction_id) 已物化。"""
    uniques = [c for c in InteractionModel.__table__.constraints
               if isinstance(c, UniqueConstraint)]
    assert len(uniques) == 1
    assert uniques[0].name == "uq_interactions_tenant_channel_external_id"
    assert {c.name for c in uniques[0].columns} == {
        "tenant_id", "channel_id", "external_interaction_id"}
    cols = {c.name: c for c in inspect(InteractionModel).columns}
    # 身份一半仍可空：无外部 ID 的行不参与去重（NULL 不受唯一约束）
    assert cols["external_interaction_id"].nullable is True
    assert not cols["external_interaction_id"].index
    # scope 列 NOT NULL；不为 channel 单独建索引（复合唯一索引前导列即 channel）


def test_b2_project_already_has_channel_scoped_unique_discipline() -> None:
    """对照纪律：identities / connections 的外部 ID 唯一键都带 channel 作用域。"""
    from app.persistence.models.connection import ConnectionModel
    from app.persistence.models.identity import IdentityModel

    for model, expected in (
        (IdentityModel, {"tenant_id", "channel_id", "external_id"}),
        (ConnectionModel, {"tenant_id", "channel_id", "agent_id"}),
    ):
        uniques = [c for c in model.__table__.constraints
                   if isinstance(c, UniqueConstraint)]
        assert uniques, f"{model.__tablename__} 缺少唯一约束"
        assert any({col.name for col in u.columns} == expected for u in uniques), \
            f"{model.__tablename__} 唯一键不是 {expected}"


def test_b3_channel_id_is_a_first_class_identity_column() -> None:
    """（10.5.2 翻转，原缺口 G1 关闭）channel_id 是一等 FK 身份列。"""
    cols = {c.name for c in inspect(InteractionModel).columns}
    assert "channel_id" in cols
    assert not inspect(InteractionModel).columns["channel_id"].nullable
    fk = list(InteractionModel.__table__.columns["channel_id"].foreign_keys)[0]
    assert fk.target_fullname == "channels.id"


# ================================================================== C. 创建边界


def test_c1_interaction_row_is_created_at_exactly_one_boundary() -> None:
    """建行边界唯一 = Runtime 调用边界（PersistingCoordinator），无第二处。

    区分两种 `InteractionRecord(...)` 构造：
    - `application/interaction_recording.py` = **写路径**（交给 `interactions.create`）；
    - `persistence/mappers/interaction_record.py` = **读路径映射**（`to_domain`，
      把 ORM 行翻译成领域对象，不产生新行）。
    """
    create_hits: set[str] = set()
    construct_hits: set[str] = set()
    for path in _py_files(APP):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(APP).as_posix()
        src = path.read_text(encoding="utf-8")
        if "interactions.create(" in src:
            create_hits.add(rel)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "InteractionRecord"):
                construct_hits.add(rel)
    assert create_hits == {"application/interaction_recording.py"}
    assert construct_hits == {"application/interaction_recording.py",
                              "persistence/mappers/interaction_record.py"}


def test_c2_inbound_protocol_failure_produces_no_row(db_engine) -> None:
    """入站协议失败（尚未进入创建边界）→ 不产生 Interaction 行。"""
    g = _seed(db_engine)
    service = ChannelInboundService(
        uow_factory=_uow_factory(db_engine),
        intake=_wrapped_runtime(db_engine),     # 12.9.4.6：intake 只入队
        inbound_adapter=_RaisingInboundAdapter(InboundFormatError("bad")),
    )
    with pytest.raises(InboundFormatError):
        service.handle_message(
            g["channel"], signature="s", timestamp="t", nonce="n", body="b")
    assert _interaction_rows(db_engine) == []


def test_c3_runtime_semantic_failure_still_records_a_row(db_engine) -> None:
    """Runtime 语义失败（已过创建边界）→ 仍产生终态记录行。"""
    g = _seed(db_engine)
    with pytest.raises(IdentityResolutionFailedError):
        _wrapped_runtime(db_engine).execute(
            _inbound(g["channel"], identity="wx_unknown"))
    rows = _interaction_rows(db_engine)
    assert len(rows) == 1
    assert rows[0].processing_status == "failed"
    assert rows[0].failure_stage == "identity_resolution"


# ================================================================== D. duplicate create


def test_d1_duplicate_create_conflicts_at_db_and_produces_one_row(db_engine) -> None:
    """（10.5.2 翻转）同 Channel + 同 MsgId 第二次 create → DB conflict。

    本步只物化身份：conflict 以 IntegrityError 形态暴露，不映射、不吞、不跳过——
    「命中后怎么办」属 10.5.3+ 的 Execution semantics 裁决（Idempotency ≠ Retry）。
    """
    g = _seed(db_engine)
    agent = _CountingAgentAdapter()
    channel = _RecordingChannelAdapter()
    runtime = _wrapped_runtime(db_engine, agent_adapter=agent,
                               channel_adapter=channel)
    inbound = _inbound(g["channel"], "msg-1")
    runtime.execute(inbound)

    with pytest.raises(IntegrityError):
        runtime.execute(inbound)

    rows = _interaction_rows(db_engine)
    assert len(rows) == 1                                # 只有一条
    assert rows[0].external_interaction_id == "msg-1"
    assert rows[0].processing_status == "succeeded"
    assert agent.calls == 1                              # 没有第二次副作用
    assert len(channel.deliveries) == 1


# ================================================================== E. 状态语义


def test_e1_failed_row_is_never_updated_by_a_later_request(db_engine) -> None:
    """终态不可逆：后续执行新建行，旧失败行原样保留。

    10.5.2 后同 MsgId 的渠道重推在 DB 层 conflict（见 D1），故此处用同一
    Channel 内**不同 MsgId** 表达同一不变量：重试 = 新行，旧行永不更新。
    """
    g = _seed(db_engine, endpoint=DEAD_ENDPOINT)
    runtime = _wrapped_runtime(db_engine, agent_adapter=ACPAdapter())
    for msg_id in ("msg-1", "msg-2"):
        with pytest.raises(AgentDispatchFailedError):
            runtime.execute(_inbound(g["channel"], msg_id))

    rows = _interaction_rows(db_engine)
    assert len(rows) == 2
    assert rows[0].processing_status == "failed"
    assert rows[0].failure_code == "AGENT_DISPATCH_FAILED"
    assert rows[0].retryable is True
    assert rows[1].processing_status == "failed"      # 旧行未被改写为新结果


def test_e2_succeeded_row_is_never_updated_by_a_later_request(db_engine) -> None:
    """成功行同样不可逆：后续执行新建行，首行状态 / session 引用不变。"""
    g = _seed(db_engine)
    runtime = _wrapped_runtime(db_engine)
    runtime.execute(_inbound(g["channel"], "msg-1"))
    first = _interaction_rows(db_engine)[0]
    runtime.execute(_inbound(g["channel"], "msg-2"))

    rows = _interaction_rows(db_engine)
    assert len(rows) == 2
    assert rows[0].id == first.id
    assert rows[0].processing_status == "succeeded"
    assert rows[0].delivery_status == "delivered"
    assert rows[1].session_id == rows[0].session_id  # 后续 REUSE 同一 Session


def test_e3_retryable_is_a_record_field_not_a_control_field() -> None:
    """retryable 没有控制流消费者；也不存在 retry/replay/redeliver 执行入口。"""
    for path in _py_files(APP):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                names = {n.id for n in ast.walk(node.test)
                         if isinstance(n, ast.Name)}
                assert "retryable" not in names, f"{path.name} 用 retryable 做分支"
    for path in _py_files(APP):
        src = path.read_text(encoding="utf-8")
        for forbidden in ("def retry(", "def replay(", "def redeliver("):
            assert forbidden not in src, f"{path.name} 出现 {forbidden}"


# ================================================================== F. 跨 Channel


def test_f1_same_external_id_across_channels_is_two_interactions(db_engine) -> None:
    """同一 external_interaction_id 在不同 Channel 下是两次不同交互
    （CP 不能假设渠道 ID 全局唯一）。"""
    ids = _seed_two_channels(db_engine)
    runtime = _wrapped_runtime(db_engine)
    runtime.execute(_inbound(ids["channel_a"], "shared-1",
                             conversation="conv-a", identity="wx_a"))
    runtime.execute(_inbound(ids["channel_b"], "shared-1",
                             conversation="conv-b", identity="wx_b"))

    rows = _interaction_rows(db_engine)
    assert len(rows) == 2
    assert {r.external_interaction_id for r in rows} == {"shared-1"}
    assert rows[0].session_id != rows[1].session_id   # 分属两个会话


# ================================================================== G. in-flight


def test_g1_inflight_execution_has_no_protection(db_engine) -> None:
    """Agent 执行中再进事件：PROCESSING 事实可观察，但无任何 in-flight 保护。

    （10.5.2 后用不同 MsgId 隔离 in-flight 与身份 conflict，见 D1。）
    """
    g = _seed(db_engine)
    holder: dict = {}
    agent = _ReentrantAgentAdapter(
        holder, g["channel"], "msg-1", "msg-2",
        lambda: _interaction_rows(db_engine))
    runtime = _wrapped_runtime(db_engine, agent_adapter=agent,
                               channel_adapter=_RecordingChannelAdapter())
    holder["runtime"] = runtime
    runtime.execute(_inbound(g["channel"], "msg-1"))

    assert agent.status_during_retry == "processing"  # in-flight 事实真实持久化
    rows = _interaction_rows(db_engine)
    assert len(rows) == 2                             # 没有任何拦截
    assert agent.calls == 2                           # Agent 被并发调用两次
    assert all(r.processing_status == "succeeded" for r in rows)


# ================================================================== H. 边界 / 契约


def test_h1_no_idempotency_or_dedup_infrastructure_exists() -> None:
    """无幂等基础设施偷渡：无 Idempot*/Dedup* 符号，无 Interaction 维度的
    Duplicate 语义，无 redis / celery 依赖。

    对照（不计入偷渡）：management 层的 `DuplicateAgentError` /
    `DuplicateChannelError` / `DuplicateIdentityError` 是 **6.2/6.3 的同名去重**
    语义，回答「这个名字在本平台是否已存在」，与 Interaction 入站幂等（「这条渠道
    消息是否已处理过」）是两件事。
    """
    symbols: set[str] = set()
    for path in _py_files(APP):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                symbols.add(node.name.lower())
    hits = sorted(s for s in symbols if "idempot" in s or "dedup" in s)
    assert not hits, f"出现幂等基础设施符号：{hits}"
    interaction_dupes = sorted(s for s in symbols
                               if "duplicate" in s and "interaction" in s)
    assert not interaction_dupes, f"出现 Interaction 去重语义：{interaction_dupes}"

    for path in _py_files(APP):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        assert roots.isdisjoint({"redis", "celery"}), f"{path.name}: {roots}"


def test_h2_runtime_boundary_still_has_zero_persistence_contact() -> None:
    """回归 10.4.1 c1：Runtime 全链仍零 persistence 接触（创建边界只在装饰器）。"""
    guarded = ("identity_resolution.py", "routing.py", "session_resolution.py",
               "session_creation.py", "agent_dispatch.py", "delivery.py",
               "coordinator.py", "runtime.py", "channel_inbound.py")
    forbidden = ("app.persistence", "sqlalchemy")
    for name in guarded:
        tree = ast.parse((APPLICATION / name).read_text(encoding="utf-8"),
                         filename=name)
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module)
        hit = [r for r in roots
               if any(r == f or r.startswith(f + ".") for f in forbidden)]
        assert not hit, f"application/{name} 触碰 persistence：{hit}"


def test_h3_audit_contract_is_written_down() -> None:
    """契约成文（防漂移）：关键裁决 token 必须出现在审计文档中。"""
    assert DOC.exists(), f"缺少审计文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in ("(channel_id, external_interaction_id)",
                  "Runtime 调用边界",
                  "不引入独立 idempotency key",
                  "不新增 deduplication state",
                  "终态不可逆",
                  "副作用抑制",
                  "PROCESSING"):
        assert token in text, f"审计文档缺少裁决 token：{token}"
