"""Step 10.5.2 —— Interaction Identity Materialization。

把 Channel + External Interaction ID 从 JSON 内的事实提升成 Interaction 的
一等持久化身份：

    Interaction
    ├── id
    ├── channel_id                    ← 本步物化（FK → channels.id, NOT NULL）
    ├── session_id
    ├── external_interaction_id
    └── request_payload               ← 仍保留原始事件（含 channel_id 快照）

并建立 `UNIQUE(channel_id, external_interaction_id)`。

统一身份纪律（本步的设计结果）：

    Identity   : channel_id + external_id
    Interaction: channel_id + external_interaction_id
    Connection : channel_id + agent_id

    所有外部世界产生的 ID，都必须在自己的 Channel Scope 内解释。

**本步明确不做 Duplicate Handling**（Idempotency ≠ Retry）：
UNIQUE 只意味着「第一次 create 成功、第二次 create → DB conflict」；
conflict 后怎么办（抑制 / 重新执行 / FAILED+retryable 的自愈通道）
属 10.5.3+ 的 Execution semantics 裁决，本步不猜。

验收（14 条 → 本文件断言 + A/B/C/D/E/F/G 六段）。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import UniqueConstraint, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SASession, sessionmaker

from app.adapters.agents.acp import ACPAdapter
from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.errors import IdentityResolutionFailedError
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
from app.domain.interaction_record import InteractionRecord
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.models.interaction import InteractionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
APPLICATION = APP / "application"
REPO_ROOT = BACKEND.parent
DOC = REPO_ROOT / "docs" / "step-10.5.2-interaction-identity-materialization.md"


# ================================================================== 测试底座


class _CountingAgentAdapter:
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


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine) -> dict[str, int]:
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        bob = uow.users.create(User(name="bob"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        channel_a = uow.channels.create(Channel(name="ch-a", type="wecom"))
        channel_b = uow.channels.create(Channel(name="ch-b", type="wecom"))
        uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel_a.id, external_id="wx_a"))
        uow.identities.create(Identity(
            user_id=bob.id, channel_id=channel_b.id, external_id="wx_b"))
        for ch, conversation in ((channel_a, "conv-a"), (channel_b, "conv-b")):
            path = uow.connections.create(Connection(
                channel_id=ch.id, agent_id=agent.id, name=f"path-{ch.id}",
                status="active",
                config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
                credential_ref="cred"))
            # Implementation Package 2 / Step 2：显式授权该会话（default-deny 前提）
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=ch.id, external_conversation_id=conversation,
                connection_id=path.id))
        uow.commit()
        return {"channel_a": channel_a.id, "channel_b": channel_b.id,
                "agent": agent.id}


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
             conversation: str = "conv-a", identity: str = "wx_a",
             content: str = "hello") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id=interaction,
        external_conversation_id=conversation,
        external_identity_id=identity, content=content)


def _interaction_rows(db_engine) -> list[InteractionModel]:
    with SASession(db_engine) as s:
        return list(s.execute(select(InteractionModel)
                              .order_by(InteractionModel.id)).scalars().all())


# ================================================================== A. Schema 物化


def test_a1_channel_id_is_first_class_in_domain_and_orm() -> None:
    """channel_id 成为一等字段：domain 记录镜像 + ORM 列都在，且不可空。"""
    from dataclasses import fields

    domain_fields = {f.name for f in fields(InteractionRecord)}
    assert "channel_id" in domain_fields
    cols = {c.name: c for c in inspect(InteractionModel).columns}
    assert cols["channel_id"].nullable is False
    assert cols["external_interaction_id"].nullable is True  # 身份另一半保持可空


def test_a2_channel_id_is_fk_to_channels() -> None:
    """FK 正确：interactions.channel_id → channels.id（与 sessions 同表纪律）。"""
    fk = list(InteractionModel.__table__.columns["channel_id"].foreign_keys)[0]
    assert fk.target_fullname == "channels.id"


def test_a3_identity_unique_constraint_materialized() -> None:
    """UNIQUE(channel_id, external_interaction_id) 在 schema 上，命名对齐纪律。"""
    uniques = [c for c in InteractionModel.__table__.constraints
               if isinstance(c, UniqueConstraint)]
    assert len(uniques) == 1
    assert uniques[0].name == "uq_interactions_tenant_channel_external_id"
    assert {c.name for c in uniques[0].columns} == {
        "tenant_id", "channel_id", "external_interaction_id"}


def test_a4_no_separate_channel_index_needed() -> None:
    """索引裁决：复合唯一索引以 channel_id 为前导列，天然服务作用域访问，
    不再为 channel_id 单独建索引（identities / connections 同纪律）。"""
    cols = {c.name: c for c in inspect(InteractionModel).columns}
    assert not cols["channel_id"].index
    indexes = [ix for ix in InteractionModel.__table__.indexes
               if ix.columns[0].name == "channel_id"
               and len(ix.columns) == 1]
    assert not indexes


def test_a5_request_payload_still_keeps_raw_event(db_engine) -> None:
    """request_payload 仍保留原始事件（channel_id 快照 + 全部外部 ID + content）。"""
    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(
        _inbound(g["channel_a"], "msg-1", content="hello"))

    row = _interaction_rows(db_engine)[0]
    assert row.channel_id == g["channel_a"]
    assert row.request_payload == {
        "channel_id": g["channel_a"],
        "external_interaction_id": "msg-1",
        "external_conversation_id": "conv-a",
        "external_identity_id": "wx_a",
        "content": "hello",
        "metadata": None,   # 12.9.4.3：完整入站快照（该入站未携带 metadata）
    }


# ================================================================== B. Identity 三部曲（核心）


def test_b1_channel_a_msgid_100_is_allowed(db_engine) -> None:
    """核心①：Channel A + MsgId 100 → allowed（创建成功，身份完整）。"""
    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(_inbound(g["channel_a"], "100"))

    rows = _interaction_rows(db_engine)
    assert len(rows) == 1
    assert rows[0].channel_id == g["channel_a"]
    assert rows[0].external_interaction_id == "100"
    assert rows[0].processing_status == "succeeded"


def test_b2_channel_b_msgid_100_coexists(db_engine) -> None:
    """核心②：Channel B + MsgId 100 → allowed（同 MsgId 跨 Channel 共存）。"""
    g = _seed(db_engine)
    runtime = _wrapped_runtime(db_engine)
    runtime.execute(_inbound(g["channel_a"], "100",
                             conversation="conv-a", identity="wx_a"))
    runtime.execute(_inbound(g["channel_b"], "100",
                             conversation="conv-b", identity="wx_b"))

    rows = _interaction_rows(db_engine)
    assert len(rows) == 2
    assert {r.channel_id for r in rows} == {g["channel_a"], g["channel_b"]}
    assert {r.external_interaction_id for r in rows} == {"100"}
    assert all(r.processing_status == "succeeded" for r in rows)


def test_b3_channel_a_msgid_100_again_conflicts(db_engine) -> None:
    """核心③：Channel A + MsgId 100 再次 create → DB conflict，无第二行。"""
    g = _seed(db_engine)
    agent = _CountingAgentAdapter()
    channel = _RecordingChannelAdapter()
    runtime = _wrapped_runtime(db_engine, agent_adapter=agent,
                               channel_adapter=channel)
    runtime.execute(_inbound(g["channel_a"], "100"))

    with pytest.raises(IntegrityError):
        runtime.execute(_inbound(g["channel_a"], "100"))

    rows = _interaction_rows(db_engine)
    assert len(rows) == 1                       # UNIQUE 阻止了第二条
    assert agent.calls == 1                     # 且没有第二次副作用
    assert len(channel.deliveries) == 1


def test_b4_conflict_exposes_raw_integrityerror_no_translation(db_engine) -> None:
    """conflict 以 IntegrityError 原样暴露：不映射、不吞、不转换、不跳过。"""
    g = _seed(db_engine)
    runtime = _wrapped_runtime(db_engine)
    runtime.execute(_inbound(g["channel_a"], "100"))
    with pytest.raises(IntegrityError):
        runtime.execute(_inbound(g["channel_a"], "100"))
    # 第二次失败的 UoW 已回滚：库里仍是干净的一条
    assert len(_interaction_rows(db_engine)) == 1


# ================================================================== C. Repository 精确查询


def test_c1_repository_finds_interaction_by_identity(db_engine) -> None:
    """按 (channel_id, external_interaction_id) 精确读回（Port + Adapter）。"""
    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(
        _inbound(g["channel_a"], "100", content="hello"))

    with _uow_factory(db_engine)() as uow:
        found = uow.interactions.find_by_channel_and_external_id(
            g["channel_a"], "100")
        assert found is not None
        assert found.channel_id == g["channel_a"]
        assert found.external_interaction_id == "100"
        assert found.request_payload["content"] == "hello"
        # 按主键读回的同一行
        by_id = uow.interactions.get_by_id(found.id)
        assert by_id is not None and by_id.id == found.id


def test_c2_wrong_channel_or_unknown_id_returns_none(db_engine) -> None:
    """跨 Channel 的同名 external id 查不到（scope 正确）；未知 id 返回 None。"""
    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(_inbound(g["channel_a"], "100"))

    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.find_by_channel_and_external_id(
            g["channel_b"], "100") is None          # scope 隔离
        assert uow.interactions.find_by_channel_and_external_id(
            g["channel_a"], "999") is None          # 不存在


# ================================================================== D. Runtime / Adapter 零改动


def test_d1_runtime_core_and_executors_untouched() -> None:
    """Runtime Core（coordinator / runtime Port / 六 executor / channel_inbound）
    零改动：不 import persistence、不构造 InteractionRecord、不出现 channel_id
    落库逻辑（身份写入只发生在调用边界的 wrapper）。"""
    guarded = ("coordinator.py", "runtime.py", "identity_resolution.py",
               "routing.py", "session_resolution.py", "session_creation.py",
               "agent_dispatch.py", "delivery.py", "channel_inbound.py")
    forbidden_roots = ("app.persistence", "sqlalchemy",
                       "app.repositories.interaction_record")
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
               if any(r == f or r.startswith(f + ".") for f in forbidden_roots)]
        assert not hit, f"application/{name} 触碰持久化边界：{hit}"
        src = (APPLICATION / name).read_text(encoding="utf-8")
        assert "InteractionRecord" not in src, f"application/{name}"


def test_d2_channel_adapters_untouched() -> None:
    """Adapter 零改动：wecom / acp 不 import persistence、不认识
    InteractionRecord——身份物化对 wire 层完全不可见。"""
    for rel in ("adapters/channels/wecom.py", "adapters/agents/acp.py"):
        tree = ast.parse((APP / rel).read_text(encoding="utf-8"), filename=rel)
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module)
        forbidden = ("app.persistence", "app.repositories.interaction_record",
                     "app.domain.interaction_record")
        hit = [r for r in roots
               if any(r == f or r.startswith(f + ".") for f in forbidden)]
        assert not hit, f"{rel} 触碰身份物化：{hit}"


def test_d3_uow_needs_no_new_repository() -> None:
    """UoW 无需扩边：interactions 仓库 10.4.2 起已暴露，本步只在 Port 上加读路径。"""
    port_src = (APP / "repositories" / "unit_of_work.py").read_text(encoding="utf-8")
    impl_src = (APP / "persistence" / "unit_of_work.py").read_text(encoding="utf-8")
    assert "interactions" in port_src and "interactions" in impl_src
    # 没有为本步新增任何仓库（无 Outbox / Idempotency 仓库偷渡）
    assert "Outbox" not in port_src and "Idempotency" not in port_src


# ================================================================== E. 不引入幂等决策


def test_e1_no_idempotency_decision_in_write_boundary() -> None:
    """写入边界不出现「命中已存在即跳过/返回」类幂等决策：
    - wrapper 不做 try/except IntegrityError；
    - wrapper 不查询 Interaction 身份（`uow.interactions.find_by_...`）——
      注意 wrapper 里同名方法是 `uow.identities.find_by_channel_and_external_id`
      （Identity 解析的既有读路径，与本仓库无关）；
    - Repository 不提供 create-or-get / upsert 语义。"""
    src = (APPLICATION / "interaction_recording.py").read_text(encoding="utf-8")
    assert "IntegrityError" not in src
    assert "interactions.find_by_channel_and_external_id" not in src

    repo = (APP / "persistence" / "repositories"
            / "interaction_record.py").read_text(encoding="utf-8")
    for forbidden in ("def create_or_get", "def upsert", "def get_or_create",
                      "def find_or_create"):
        assert forbidden not in repo, f"Repository 出现幂等决策形状 {forbidden}"


def test_e2_no_retry_no_duplicate_status_no_new_key() -> None:
    """不引入 Retry / Duplicate 状态 / 新 idempotency_key：
    - 无 retry / replay 执行入口（全 app 树）；
    - 无 Idempot*/Dedup* 符号、无 Interaction 维度的 Duplicate 语义
      （management 层的 DuplicateAgentError 等是 6.2/6.3 同名去重，不相关）；
    - Interaction 路径四个文件中不出现 DUPLICATE / IDEMPOTENCY_KEY 字样。"""
    symbols: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                symbols.add(node.name)
    assert not [s for s in symbols
                if "idempot" in s.lower() or "dedup" in s.lower()]
    assert not [s for s in symbols
                if "duplicate" in s.lower() and "interaction" in s.lower()]

    interaction_path = ("domain/interaction_record.py",
                        "persistence/models/interaction.py",
                        "persistence/repositories/interaction_record.py",
                        "application/interaction_recording.py")
    for rel in interaction_path:
        src = (APP / rel).read_text(encoding="utf-8")
        for forbidden in ("DUPLICATE", "IDEMPOTENCY_KEY"):
            assert forbidden not in src, f"{rel} 出现 {forbidden}"
    for path in sorted(APP.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        for forbidden in ("def retry(", "def replay(", "def redeliver("):
            assert forbidden not in src, f"{path.name} 出现 {forbidden}"


# ================================================================== F. 失败路径语义回归


def test_f1_pre_session_failure_still_records_with_channel(db_engine) -> None:
    """10.4.2 语义回归：Identity 失败仍被记录，且现在带 channel 身份。"""
    g = _seed(db_engine)
    with pytest.raises(IdentityResolutionFailedError):
        _wrapped_runtime(db_engine).execute(
            _inbound(g["channel_a"], "msg-1", identity="wx_unknown"))

    row = _interaction_rows(db_engine)[0]
    assert row.processing_status == "failed"
    assert row.failure_stage == "identity_resolution"
    assert row.session_id is None                       # Session 建立前失败
    assert row.channel_id == g["channel_a"]             # 身份完整


# ================================================================== G. 契约成文


def test_g1_contract_is_written_down() -> None:
    """契约文档成文（防漂移）：身份三部曲 + UNIQUE + 不做 Duplicate Handling。"""
    assert DOC.exists(), f"缺少契约文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in ("UNIQUE(channel_id, external_interaction_id)",
                  "channel_id + external_interaction_id",
                  "所有外部世界产生的 ID",
                  "Idempotency ≠ Retry",
                  "DB conflict",
                  "不做 Duplicate Handling",
                  "find_by_channel_and_external_id"):
        assert token in text, f"契约文档缺少 token：{token}"
