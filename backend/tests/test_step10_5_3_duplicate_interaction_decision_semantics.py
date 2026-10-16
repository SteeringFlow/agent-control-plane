"""Step 10.5.3 —— Duplicate Interaction Decision Semantics（决策语义步，生产代码 0 行）。

问题已从「怎么判断重复」（10.5.2：UNIQUE(channel_id, external_interaction_id) 已解决）
推进到「判断重复之后，Control Plane 应该做什么」。本文件把决策语义钉成可回归断言：

A  决策结果是**内部裁决 token，不是 DB 状态**：DB 仍只有 processing 四值
   （pending/processing/succeeded/failed）× delivery 三值（pending/delivered/failed），
   无 DUPLICATE 值 / 列；token（DUPLICATE_TERMINAL 等）只活在契约文档里；
B  决策矩阵的输入状态全部真实可观察（成功 / 不可重试失败 / Case A / Case B）；
   **Case A（Agent 未成功：FAILED+delivery=PENDING+retryable=True）与
   Case B（Agent 已成功：SUCCEEDED+delivery=FAILED+retryable=True）在行上可区分**；
C  absent → NEW → execute（1 行 + 1 次副作用）；
D  终态重复（同 MsgId 再达）→ IntegrityError、无第二次副作用：
   suppress 的 DB 层效果已被身份约束实现，但「已处理」响应语义未编码；
   今天渠道重推既不能重新执行 Agent（D3/Case A），也绝不重跑 Agent（D4/Case B）；
E  PROCESSING 不是锁：schema 无所有权列；in-flight 行存在时同 MsgId 再达 →
   IntegrityError（无第二次执行）；卡死 PROCESSING 是该 MsgId 的永久阴影
   （per-identity，不阻塞其他 MsgId）——无租约，无恢复路径；
F  决策层零基础设施偷渡（无 lock/lease/queue/worker/幂等）；Port 面零变化；
   Delivery Retry 今天没有承载物（response_payload 零写入方、OutboundResponse 不回链）；
G  契约成文（决策矩阵四个 token + Delivery Retry ≠ Agent Retry 等）。

历史注记：10.5.1 G1 曾证明「同 MsgId in-flight 会再次执行」；10.5.2 身份物化后
同 MsgId 的第二次 create 在任何时刻（含 in-flight）都先撞 UNIQUE（本文件 E2/E3
真实探针），该事实随 10.5.2 已翻转，G1 因此改用不同 MsgId 隔离两个事实。

本步不设计 Redis / 锁 / 租约 / 队列 / 幂等服务，不为 §6 三个未决问题拍板。
"""

import ast
from dataclasses import fields
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SASession, sessionmaker

from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.errors import (
    AgentDispatchFailedError,
    ChannelDeliveryFailedError,
    IdentityResolutionFailedError,
)
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
from app.domain.interaction_record import (
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    InteractionRecord,
    PROCESSING_FAILED,
    PROCESSING_PENDING,
    PROCESSING_PROCESSING,
    PROCESSING_SUCCEEDED,
)
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.models.interaction import InteractionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.interaction_record import InteractionRecordRepository

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
APPLICATION = APP / "application"
REPO_ROOT = BACKEND.parent
DOC = REPO_ROOT / "docs" / "step-10.5.3-duplicate-interaction-decision-semantics.md"

# 决策结果 token（内部语义，非 DB 状态 —— 只允许出现在文档里，不允许进生产代码）
DECISION_TOKENS = ("DUPLICATE_TERMINAL", "DUPLICATE_IN_FLIGHT",
                   "RETRYABLE_FAILURE", "DELIVERY_FAILURE")


# ================================================================== 测试底座


class _CountingAgentAdapter:
    """成功 Agent：记录调用次数（副作用计数的直接证据）。"""

    def __init__(self) -> None:
        self.calls = 0

    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        self.calls += 1
        return AgentResponse(content=f"echo:{request.content}",
                             external_session_id="agent-session-1")


class _FailingAgentAdapter:
    """Agent transport 必失败（Case A 语义：Agent 未成功，delivery=PENDING）。"""

    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        raise AgentDispatchFailedError()


class _RecordingChannelAdapter:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


class _FailingChannelAdapter:
    """Delivery 必失败（Case B 语义：Agent 已成功 processing=SUCCEEDED）。"""

    def deliver(self, outbound: OutboundResponse) -> None:
        raise ChannelDeliveryFailedError(
            channel_id=outbound.channel_id,
            external_conversation_id=outbound.external_conversation_id)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine) -> dict[str, int]:
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status="active",
            config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
            credential_ref="cred-a"))
        # Implementation Package 2 / Step 2：显式授权该会话（default-deny 前提）
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="user-A",
            connection_id=path.id))
        uow.commit()
        return {"channel": channel.id, "agent": agent.id}


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


def _inbound(channel_id: int, interaction: str = "msg-1",
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


def _plant_processing_row(db_engine, channel_id: int, msg_id: str) -> int:
    """复刻 `_start` 的两事务，停在 PROCESSING（模拟执行中 / 进程死后的卡死行）。"""
    with _uow_factory(db_engine)() as uow:
        rec = uow.interactions.create(InteractionRecord(
            direction="inbound", channel_id=channel_id,
            external_interaction_id=msg_id,
            request_payload={"channel_id": channel_id,
                             "external_interaction_id": msg_id}))
        uow.commit()
    with _uow_factory(db_engine)() as uow:
        uow.interactions.claim_processing(interaction_id=rec.id)
        uow.commit()
    return rec.id


def _py_files(root: Path):
    return sorted(p for p in root.rglob("*.py"))


# ================================================================== A. 决策结果是内部语义，不是 DB 状态


def test_a1_db_status_values_are_only_4_plus_3() -> None:
    """DB 状态不增：processing 四值 × delivery 三值；无 DUPLICATE 值/列/字面量。"""
    assert {PROCESSING_PENDING, PROCESSING_PROCESSING,
            PROCESSING_SUCCEEDED, PROCESSING_FAILED} == {
        "pending", "processing", "succeeded", "failed"}
    assert {DELIVERY_PENDING, DELIVERY_DELIVERED, DELIVERY_FAILED} == {
        "pending", "delivered", "failed"}

    # ORM 无重复/决策类列：interactions 仍是生命周期记录，不是判定结果的存储
    cols = {c.name for c in inspect(InteractionModel).columns}
    forbidden_cols = {c for c in cols
                      if any(k in c.lower() for k in
                             ("duplicate", "dedup", "decision", "verdict"))}
    assert not forbidden_cols, f"出现决策/重复类列：{forbidden_cols}"

    # 生命周期写入方不得出现新状态字面量
    src = (APPLICATION / "interaction_recording.py").read_text(encoding="utf-8")
    for token in ("duplicate", "dedup", "decided", "suppressed", "queued",
                  "delivered_or", "enqueued"):
        assert token not in src.lower(), f"生命周期写入方出现新状态字面量：{token}"


def test_a2_decision_tokens_are_internal_and_not_persisted() -> None:
    """决策 token 只属于内部语义：全生产代码（app 树）不得出现这四个 token。"""
    for path in _py_files(APP):
        src = path.read_text(encoding="utf-8")
        for token in DECISION_TOKENS:
            assert token not in src, \
                f"{path.relative_to(APP)} 出现了决策 token {token}（只允许在文档）"


# ================================================================== B. 决策矩阵的输入状态空间（真实可观察）


def test_b1_success_terminal_row_shape(db_engine) -> None:
    """成功终态：processing=SUCCEEDED + delivery=DELIVERED + retryable=False。"""
    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(_inbound(g["channel"], "msg-1"))

    row = _interaction_rows(db_engine)[0]
    assert row.processing_status == PROCESSING_SUCCEEDED
    assert row.delivery_status == DELIVERY_DELIVERED
    assert row.retryable is False
    assert row.failure_stage is None and row.failure_code is None


def test_b2_non_retryable_failure_terminal_row_shape(db_engine) -> None:
    """不可重试失败终态：FAILED + delivery=PENDING + retryable=False
    （契约/身份类失败，重复请求无意义 → suppress 语义已定的那类行）。"""
    g = _seed(db_engine)
    with pytest.raises(IdentityResolutionFailedError):
        _wrapped_runtime(db_engine).execute(
            _inbound(g["channel"], "msg-1", identity="wx_unknown"))

    row = _interaction_rows(db_engine)[0]
    assert row.processing_status == PROCESSING_FAILED
    assert row.delivery_status == DELIVERY_PENDING
    assert row.retryable is False
    assert row.failure_stage == "identity_resolution"
    assert row.failure_code == "IDENTITY_RESOLUTION_FAILED"
    assert row.session_id is None


def test_b3_case_a_agent_failure_row_shape(db_engine) -> None:
    """Case A（Agent 未成功）：FAILED + delivery=PENDING + retryable=True。"""
    g = _seed(db_engine)
    with pytest.raises(AgentDispatchFailedError):
        _wrapped_runtime(db_engine, agent_adapter=_FailingAgentAdapter()).execute(
            _inbound(g["channel"], "msg-1"))

    row = _interaction_rows(db_engine)[0]
    assert row.processing_status == PROCESSING_FAILED
    assert row.delivery_status == DELIVERY_PENDING
    assert row.retryable is True
    assert row.failure_stage == "agent_dispatch"
    assert row.failure_code == "AGENT_DISPATCH_FAILED"


def test_b4_case_b_delivery_failure_row_shape(db_engine) -> None:
    """Case B（Agent 已成功）：processing=SUCCEEDED + delivery=FAILED +
    retryable=True（Delivery 失败 ≠ Processing 失败，10.4.2 冻结）。"""
    g = _seed(db_engine)
    with pytest.raises(ChannelDeliveryFailedError):
        _wrapped_runtime(db_engine,
                         channel_adapter=_FailingChannelAdapter()).execute(
            _inbound(g["channel"], "msg-1"))

    row = _interaction_rows(db_engine)[0]
    assert row.processing_status == PROCESSING_SUCCEEDED
    assert row.delivery_status == DELIVERY_FAILED
    assert row.retryable is True
    assert row.failure_stage == "delivery"
    assert row.failure_code == "CHANNEL_DELIVERY_FAILED"


def test_b5_case_a_and_b_are_distinguishable_by_processing_status(db_engine) -> None:
    """核心输入事实：Case A 与 Case B 差在 processing 一个维度即可区分——
    不需要新列，DB 已经回答「Agent 到底成没成功」。"""
    g = _seed(db_engine)
    with pytest.raises(AgentDispatchFailedError):
        _wrapped_runtime(db_engine, agent_adapter=_FailingAgentAdapter()).execute(
            _inbound(g["channel"], "msg-1"))
    with pytest.raises(ChannelDeliveryFailedError):
        _wrapped_runtime(db_engine,
                         channel_adapter=_FailingChannelAdapter()).execute(
            _inbound(g["channel"], "msg-2"))

    rows = _interaction_rows(db_engine)
    case_a, case_b = rows[0], rows[1]
    assert case_a.processing_status != case_b.processing_status
    assert case_a.retryable is True and case_b.retryable is True
    assert case_a.processing_status == PROCESSING_FAILED
    assert case_b.processing_status == PROCESSING_SUCCEEDED
    assert case_a.delivery_status == DELIVERY_PENDING
    assert case_b.delivery_status == DELIVERY_FAILED


# ================================================================== C. absent → execute


def test_c1_absent_interaction_executes_once(db_engine) -> None:
    """矩阵第一行：无既有 Interaction → NEW → execute（1 行 + 1 次副作用）。"""
    g = _seed(db_engine)
    agent = _CountingAgentAdapter()
    channel = _RecordingChannelAdapter()
    _wrapped_runtime(db_engine, agent_adapter=agent,
                     channel_adapter=channel).execute(
        _inbound(g["channel"], "msg-1"))

    rows = _interaction_rows(db_engine)
    assert len(rows) == 1
    assert rows[0].processing_status == PROCESSING_SUCCEEDED
    assert agent.calls == 1
    assert len(channel.deliveries) == 1


# ================================================================== D. 终态重复（suppress 语义的当前真实行为）


def test_d1_succeeded_terminal_duplicate_is_side_effect_free_conflict(db_engine) -> None:
    """成功终态 + 同 MsgId 再达 → IntegrityError，无第二次副作用。
    （suppress 的效果已被 DB 身份实现；「已处理」响应语义尚未编码。）"""
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
    assert len(rows) == 1
    assert rows[0].processing_status == PROCESSING_SUCCEEDED
    assert agent.calls == 1                      # suppress：无第二次 Agent 副作用
    assert len(channel.deliveries) == 1


def test_d2_non_retryable_failure_duplicate_conflicts(db_engine) -> None:
    """不可重试失败终态 + 同 MsgId 再达 → IntegrityError（重推无意义：契约/身份类）。"""
    g = _seed(db_engine)
    runtime = _wrapped_runtime(db_engine)
    inbound = _inbound(g["channel"], "msg-1", identity="wx_unknown")
    with pytest.raises(IdentityResolutionFailedError):
        runtime.execute(inbound)

    with pytest.raises(IntegrityError):
        runtime.execute(inbound)

    rows = _interaction_rows(db_engine)
    assert len(rows) == 1
    assert rows[0].processing_status == PROCESSING_FAILED
    assert rows[0].retryable is False


def test_d3_case_a_duplicate_cannot_reenroll_today(db_engine) -> None:
    """Case A 终态 + 同 MsgId 再达 → IntegrityError：渠道重推今天**无法**重新执行
    Agent（行保留原失败）——「重推 = 重新执行的机会」是未决产品语义，未编码。"""
    g = _seed(db_engine)
    agent = _FailingAgentAdapter()
    runtime = _wrapped_runtime(db_engine, agent_adapter=agent)
    inbound = _inbound(g["channel"], "msg-1")
    with pytest.raises(AgentDispatchFailedError):
        runtime.execute(inbound)

    with pytest.raises(IntegrityError):
        runtime.execute(inbound)

    rows = _interaction_rows(db_engine)
    assert len(rows) == 1
    assert rows[0].processing_status == PROCESSING_FAILED
    assert rows[0].failure_code == "AGENT_DISPATCH_FAILED"
    assert rows[0].retryable is True
    # 重推没有把 Agent 执行次数从 1 变成 2（副作用统计仍只有首次尝试）


def test_d4_case_b_duplicate_never_reruns_agent(db_engine) -> None:
    """Case B 终态 + 同 MsgId 再达 → IntegrityError：Agent 绝不重跑。
    重跑会重复业务副作用——任何「因为 Delivery 失败而重跑 Agent」都是语义错误。"""
    g = _seed(db_engine)
    agent = _CountingAgentAdapter()
    runtime = _wrapped_runtime(db_engine, agent_adapter=agent,
                               channel_adapter=_FailingChannelAdapter())
    inbound = _inbound(g["channel"], "msg-1")
    with pytest.raises(ChannelDeliveryFailedError):
        runtime.execute(inbound)
    assert agent.calls == 1

    with pytest.raises(IntegrityError):
        runtime.execute(inbound)

    rows = _interaction_rows(db_engine)
    assert len(rows) == 1
    assert rows[0].processing_status == PROCESSING_SUCCEEDED   # Agent 结果原样保留
    assert rows[0].delivery_status == DELIVERY_FAILED
    assert rows[0].retryable is True
    assert agent.calls == 1                                     # 绝不第二次调用 Agent


# ================================================================== E. PROCESSING 不是锁


def test_e1_processing_row_has_no_ownership_column(db_engine) -> None:
    """PROCESSING 不是锁的 schema 证据：interactions 行没有任何 owner/lease/claim。"""
    cols = {c.name for c in inspect(InteractionModel).columns}
    for token in ("owner", "lease", "claim", "lock", "held_by", "expires_at"):
        assert not any(token in c.lower() for c in cols), \
            f"出现所有权/租约类列：{token}"
    for rel in ("domain/interaction_record.py",
                "persistence/models/interaction.py"):
        src = (APP / rel).read_text(encoding="utf-8")
        for token in ("lease", "claim", "owner", "lock"):
            assert token not in src.lower(), f"{rel} 出现 {token}"


def test_e2_inflight_row_conflicts_same_msgid_no_second_execution(db_engine) -> None:
    """in-flight（PROCESSING）行存在时，同 MsgId 再达 → IntegrityError，
    无第二次执行（10.5.2 后 UNIQUE 先于 Runtime 拦截，含 in-flight 窗口）。"""
    g = _seed(db_engine)
    agent = _CountingAgentAdapter()
    runtime = _wrapped_runtime(db_engine, agent_adapter=agent)
    _plant_processing_row(db_engine, g["channel"], "msg-1")

    assert _interaction_rows(db_engine)[0].processing_status == "processing"
    with pytest.raises(IntegrityError):
        runtime.execute(_inbound(g["channel"], "msg-1"))

    rows = _interaction_rows(db_engine)
    assert len(rows) == 1
    assert rows[0].processing_status == "processing"   # 行仍停在 PROCESSING
    assert agent.calls == 0                            # 无第二次执行


def test_e3_stuck_processing_row_never_recovers(db_engine) -> None:
    """卡死的 PROCESSING 行是该 MsgId 的永久阴影：
    - 同 MsgId 每次再达都 IntegrityError（永远冲突，无租约回收路径）；
    - 不同 MsgId 完全不受影响（阴影是 per-identity 的，不阻塞整个 Channel）。
    """
    g = _seed(db_engine)
    agent = _CountingAgentAdapter()
    runtime = _wrapped_runtime(db_engine, agent_adapter=agent)
    _plant_processing_row(db_engine, g["channel"], "msg-1")

    for _ in range(2):                                 # 多次重推，每次都冲突
        with pytest.raises(IntegrityError):
            runtime.execute(_inbound(g["channel"], "msg-1"))

    runtime.execute(_inbound(g["channel"], "msg-2"))   # 不同 MsgId 正常执行
    rows = _interaction_rows(db_engine)
    assert len(rows) == 2
    assert rows[0].processing_status == "processing"   # 阴影永远停在 PROCESSING
    assert rows[1].processing_status == PROCESSING_SUCCEEDED
    assert agent.calls == 1                            # 只有 msg-2 触发过一次 Agent


# ================================================================== F. 无基础设施偷渡 / Port 面 / Delivery Retry 承载物


def test_f1_no_lock_lease_queue_infrastructure_in_decision_layer() -> None:
    """决策层零基础设施偷渡：application / repositories 无 lock/lease/queue/worker/
    mutex/semaphore/幂等符号，无 redis/celery/threading/multiprocessing/queue 导入。"""
    for root in (APPLICATION, APP / "repositories"):
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            roots: set[str] = set()
            names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots.add(node.module.split(".")[0])
                elif isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                       ast.AsyncFunctionDef)):
                    names.add(node.name)
            # 12.9.4.16：application/driver.py 是全仓**唯一**的后台 ticker，它跑的是
            # 阻塞的 Worker.process（同步 DB / HTTP），只能由线程承载——放进 asyncio
            # loop 会阻塞整个服务。threading 因此**只豁免这一个文件**，其余模块仍禁。
            banned = {"redis", "celery", "queue", "multiprocessing"}
            if path.name != "driver.py":
                banned.add("threading")
            assert roots.isdisjoint(banned), f"{path.name}: {roots}"
            for name in names:
                lower = name.lower()
                for token in ("lock", "lease", "queue", "mutex",
                              "semaphore", "dedup", "idempot"):
                    assert token not in lower, f"{path.name} 出现 {name}"
                # 12.9.4.5：`Worker`（application/worker.py）是经审计引入的**单次
                # 执行 primitive**，不是后台 worker 基础设施（无 loop / 无 poll /
                # 无 supervisor）。它只许出现在 worker.py，其余模块仍禁 worker 符号。
                if path.name != "worker.py":
                    assert "worker" not in lower, f"{path.name} 出现 {name}"


def test_f2_port_surface_unchanged_no_decision_methods() -> None:
    """10.5.3 零生产改动：InteractionRecordRepository Port 面仍是五个生命周期方法
    （12.9.4.4 + find_pending：Worker pickup 的有界纯读，不做任何幂等/去重决策）；
    写入边界无 lookup→decide、无 IntegrityError 处理、无 suppress/retry 入口。"""
    port_src = (APP / "repositories" / "interaction_record.py").read_text(
        encoding="utf-8")
    tree = ast.parse(port_src, filename="interaction_record.py")
    methods = {node.name for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef)}
    assert methods == {
        "create", "claim_processing", "record_outcome", "get_by_id",
        "find_by_channel_and_external_id", "find_pending",
        # 12.9.4.12 attempt fence：PROCESSING 孤儿回收（只回收，不做幂等决策）
        "reclaim_stale_processing",
        # 13.1 Observability：有界陈列读（纯读，不做任何幂等/去重决策）
        "list_recent",
    }
    assert "def create_or_get" not in port_src
    assert "def upsert" not in port_src

    recording = (APPLICATION / "interaction_recording.py").read_text(
        encoding="utf-8")
    assert "IntegrityError" not in recording          # 不把 conflict 翻译成决策
    assert "interactions.find_by_channel_and_external_id" not in recording
    for forbidden in ("def retry(", "def replay(", "def suppress(",
                      "def decide("):
        assert forbidden not in recording, f"出现执行入口：{forbidden}"
    # Port 形状自检仍在（协议未漂移）
    assert InteractionRecordRepository is not None


def test_f3_delivery_retry_carrier_exists_but_no_retry(db_engine) -> None:
    """（10.8.4 翻转）response_payload 已成为写入事实：
    - 完整成功路径（succeeded × delivered）落响应快照 = 承载物出现；
    - **Case B（succeeded × failed）仍为 NULL**——失败走异常通道，Result 不存在，
      承载物对 Case B 尚未成立（10.7.2 B2 矩阵的诚实修订，未来接线裁决）；
    - 有承载数据 ≠ 现在实现 Retry：Retry 机制仍不存在（本步不引入）。"""
    recording = (APPLICATION / "interaction_recording.py").read_text(
        encoding="utf-8")
    assert "response_payload" in recording
    hits = {p.relative_to(APP).as_posix()
            for p in _py_files(APP)
            if "response_payload" in p.read_text(encoding="utf-8")}
    assert hits >= {
        "domain/interaction_record.py",
        "persistence/models/interaction.py",
        "persistence/mappers/interaction_record.py",
        "application/interaction_recording.py",
    }

    # OutboundResponse 仍不含 external_interaction_id（8.6 冻结，出站不回链）
    names = {f.name for f in fields(OutboundResponse)}
    assert "external_interaction_id" not in names

    # 承载数据存在，但没有任何 retry / replay / outbox 执行入口（符号级扫描，
    # docstring 里的「不做 Xxx」说明文字不算偷渡）
    for path in _py_files(APP):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                lower = node.name.lower()
                for token in ("retry", "replay", "redeliver", "outbox"):
                    assert token not in lower, \
                        f"{path.name} 出现 Retry 基础设施符号：{node.name}"
    names = {f.name for f in fields(OutboundResponse)}
    assert names == {"channel_id", "external_conversation_id", "content",
                     "metadata"}
    assert "external_interaction_id" not in names


# ================================================================== G. 契约成文


def test_g1_contract_is_written_down() -> None:
    """决策矩阵成文（防漂移）：四个决策 token + 核心语义 token 都在文档里。"""
    assert DOC.exists(), f"缺少决策语义文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in DECISION_TOKENS + (
        "(channel_id, external_interaction_id)",
        "Delivery Retry ≠ Agent Retry",
        "PROCESSING 不是锁",
        "不可重试失败",
        "三个未决问题",
        "不引入 Redis",
    ):
        assert token in text, f"决策文档缺少 token：{token}"
