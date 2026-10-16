"""Step 12.9.4.5 —— Worker Skeleton（单次执行 primitive）。

锁定 Worker.process(interaction_id) 的链路与三个核心 Guard：

    A  基本链路：claim → reconstruct → Runtime.execute → record outcome
    B  G1：claim 失败**绝不执行** Agent（整个异步架构最重要的安全阀）
    C  G2：Worker 是唯一 Runtime execution owner（ChannelInboundService 无具体
       RuntimeCoordinator，切换 = 纯装配变更）
    D  G3：Outcome 覆盖 Runtime 全部结果，且 Worker **不重新解释**状态语义

刻意不测（全部后置）：daemon loop / poll / sleep / supervisor / graceful shutdown /
queue / multiple workers / lease / heartbeat / stale recovery。
"""

import ast
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.errors import (
    ChannelDeliveryFailedError,
    IdentityResolutionFailedError,
)
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import PersistingCoordinator
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.application.worker import Worker
from app.domain.agent import Agent
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session import Session as SessionEntity
from app.domain.session_resolution import REUSE_ACTIVE
from app.domain.user import User
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]


# ================================================================== 测试底座


class _OkRuntime:
    """成功 Runtime：记录调用次数与收到的 inbound。"""

    def __init__(self, result: RuntimeExecutionResult) -> None:
        self.result = result
        self.calls = 0
        self.inbounds: list[InboundInteraction] = []

    def execute(self, inbound: InboundInteraction) -> RuntimeExecutionResult:
        self.calls += 1
        self.inbounds.append(inbound)
        return self.result


class _RaisingRuntime:
    """失败 Runtime：抛出给定异常（模拟 Runtime typed error）。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def execute(self, inbound: InboundInteraction) -> RuntimeExecutionResult:
        self.calls += 1
        raise self.exc


class _EchoAgentAdapter:
    def send(self, request, connection) -> AgentResponse:
        return AgentResponse(content=f"echo:{request.content}",
                             external_session_id="agent-session-1")


class _RecordingChannelAdapter:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


def _uow_factory(db_engine) -> Callable[[], SqlAlchemyUnitOfWork]:
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine, with_session: bool = False) -> dict:
    """最小可用链路种子：user / channel / agent / identity / connection。"""
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        identity = uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status="active",
            config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
            credential_ref="cred-a"))
        # Implementation Package 2 / Step 2：显式授权该会话（default-deny 前提）
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="conv-1",
            connection_id=path.id))
        session_id = None
        if with_session:
            session_id = uow.sessions.create(SessionEntity(
                identity_id=identity.id, channel_id=channel.id,
                external_conversation_id="conv-1", status="active")).id
        uow.commit()
        return {"channel": channel.id, "agent": agent.id, "session": session_id}


def _inbound(channel_id: int, msg_id: str = "msg-1") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id=msg_id,
        external_conversation_id="conv-1", external_identity_id="wx_a",
        content="你好")


def _create_pending(db_engine, channel_id: int, msg_id: str = "msg-1") -> int:
    """走 intake phase 落一条 PENDING（Worker 的输入）。"""
    phases = PersistingCoordinator(inner=_OkRuntime(None),  # type: ignore[arg-type]
                                   uow_factory=_uow_factory(db_engine))
    return phases.create_pending(_inbound(channel_id, msg_id))


def _worker(db_engine, runtime) -> Worker:
    return Worker(runtime=runtime,
                  phases=PersistingCoordinator(
                      inner=runtime, uow_factory=_uow_factory(db_engine)),
                  uow_factory=_uow_factory(db_engine))


def _ok_result(session_id: int) -> RuntimeExecutionResult:
    return RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=session_id,
        agent_response=AgentResponse(content="ok"))


def _row(db_engine, interaction_id: int):
    with _uow_factory(db_engine)() as uow:
        return uow.interactions.get_by_id(interaction_id)


def _real_runtime(db_engine, channel_adapter) -> RuntimeCoordinator:
    """真实 RuntimeCoordinator（六段，零改动）。"""
    uow = _uow_factory(db_engine)
    return RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=_EchoAgentAdapter(), uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )


# ================================================================== A. 基本链路


def test_a1_process_claims_executes_and_records(db_engine) -> None:
    """PENDING → process：claim 成功 → 执行 → 成功终态落定。"""
    g = _seed(db_engine, with_session=True)
    interaction_id = _create_pending(db_engine, g["channel"])
    runtime = _OkRuntime(_ok_result(g["session"]))

    assert _worker(db_engine, runtime).process(interaction_id) is True
    assert runtime.calls == 1
    row = _row(db_engine, interaction_id)
    assert row.processing_status == "succeeded"
    assert row.delivery_status == "delivered"
    assert row.response_payload["content"] == "ok"


def test_a2_runtime_receives_the_reconstructed_inbound(db_engine) -> None:
    """交给 Runtime 的是**重建**出的 inbound（12.9.4.3 闭环），字段与原始一致。"""
    g = _seed(db_engine, with_session=True)
    interaction_id = _create_pending(db_engine, g["channel"])
    runtime = _OkRuntime(_ok_result(g["session"]))

    assert _worker(db_engine, runtime).process(interaction_id) is True
    assert runtime.inbounds == [_inbound(g["channel"])]


def test_a3_worker_drives_the_real_runtime_coordinator(db_engine) -> None:
    """端到端：Worker → 真实 RuntimeCoordinator（六段）→ Delivery 真的发生。"""
    g = _seed(db_engine)
    channel_adapter = _RecordingChannelAdapter()
    runtime = _real_runtime(db_engine, channel_adapter)
    phases = PersistingCoordinator(inner=runtime,
                                   uow_factory=_uow_factory(db_engine))
    interaction_id = phases.create_pending(_inbound(g["channel"]))

    worker = Worker(runtime=runtime, phases=phases,
                    uow_factory=_uow_factory(db_engine))
    assert worker.process(interaction_id) is True

    assert len(channel_adapter.deliveries) == 1      # 六段真的跑完
    assert channel_adapter.deliveries[0].content == "echo:你好"
    row = _row(db_engine, interaction_id)
    assert row.processing_status == "succeeded"
    assert row.delivery_status == "delivered"


# ================================================================== B. G1：claim 失败绝不执行


def test_b1_already_claimed_row_is_not_executed(db_engine) -> None:
    """他人已 claim → 返回 False，Runtime 调用 0 次，状态不被改写。"""
    g = _seed(db_engine, with_session=True)
    interaction_id = _create_pending(db_engine, g["channel"])
    runtime = _OkRuntime(_ok_result(g["session"]))
    with _uow_factory(db_engine)() as uow:                # 他人先抢到
        assert uow.interactions.claim_processing(interaction_id) is True
        uow.commit()

    assert _worker(db_engine, runtime).process(interaction_id) is False
    assert runtime.calls == 0                              # ← G1
    assert _row(db_engine, interaction_id).processing_status == "processing"


def test_b2_terminal_row_is_not_executed(db_engine) -> None:
    """已是终态的行不可被重新执行。"""
    g = _seed(db_engine, with_session=True)
    interaction_id = _create_pending(db_engine, g["channel"])
    with _uow_factory(db_engine)() as uow:
        uow.interactions.record_outcome(
            interaction_id, processing_status="succeeded",
            delivery_status="delivered")
        uow.commit()
    runtime = _OkRuntime(_ok_result(g["session"]))

    assert _worker(db_engine, runtime).process(interaction_id) is False
    assert runtime.calls == 0
    assert _row(db_engine, interaction_id).processing_status == "succeeded"


def test_b3_missing_row_is_not_executed(db_engine) -> None:
    g = _seed(db_engine, with_session=True)
    runtime = _OkRuntime(_ok_result(g["session"]))

    assert _worker(db_engine, runtime).process(9999) is False
    assert runtime.calls == 0


def test_b4_two_workers_race_only_one_executes(db_engine) -> None:
    """并发语义：两个 Worker 竞争同一行 → 恰好一个执行（Runtime 调用 1 次）。"""
    g = _seed(db_engine, with_session=True)
    interaction_id = _create_pending(db_engine, g["channel"])
    runtime = _OkRuntime(_ok_result(g["session"]))
    first, second = _worker(db_engine, runtime), _worker(db_engine, runtime)

    assert first.process(interaction_id) is True
    assert second.process(interaction_id) is False
    assert runtime.calls == 1                              # 无双执行


# ================================================================== C. G2：唯一执行 owner


def test_c1_worker_owns_the_runtime_execution_callsite() -> None:
    """Worker 直接持有 runtime 并在 process 中调用 execute。"""
    src = (BACKEND / "app" / "application" / "worker.py").read_text(
        encoding="utf-8")
    assert "self._runtime.execute(" in src


def test_c2_channel_inbound_service_has_no_runtime_capability() -> None:
    """G2（12.9.4.6 迁移后）：ChannelInboundService **彻底失去执行能力**。

    它不再 import 任何 Runtime 执行契约——既不依赖具体 RuntimeCoordinator，
    也不再依赖 RuntimeExecutionCoordinator Protocol，且不出现任何执行调用；
    只持有 intake（PENDING 入队）。执行权唯一归 Worker。
    """
    src = (BACKEND / "app" / "application" / "channel_inbound.py").read_text(
        encoding="utf-8")
    # 以 import 判定依赖（docstring 提及类名不算依赖）
    assert "from app.application.runtime" not in src       # 不再依赖执行 Protocol
    assert "from app.application.coordinator" not in src   # 不 import 具体类
    assert ".execute(" not in src                          # 无任何执行调用
    assert "intake" in src and "create_pending" in src     # 只剩入队


def test_c3_worker_does_not_instantiate_the_runtime() -> None:
    """Worker 只接收注入；RuntimeCoordinator 的**实例化**仍唯一（组合根 main.py）。

    用 AST 判定真实调用——`class RuntimeCoordinator(RuntimeExecutionCoordinator)`
    是类定义，不是实例化，朴素子串匹配会误命中。
    """
    src = (BACKEND / "app" / "application" / "worker.py").read_text(
        encoding="utf-8")
    # 不 import 具体类（只依赖 Protocol）——docstring 提及类名不算依赖
    assert "from app.application.coordinator" not in src

    hits = []
    for path in (BACKEND / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "RuntimeCoordinator"):
                hits.append(path.name)
    assert hits == ["main.py"], \
        f"RuntimeCoordinator 实例化点应唯一（main.py），实际：{hits}"


# ================================================================== D. G3：Outcome 覆盖全部结果


def test_d1_success_maps_to_succeeded_delivered(db_engine) -> None:
    g = _seed(db_engine, with_session=True)
    interaction_id = _create_pending(db_engine, g["channel"])
    worker = _worker(db_engine, _OkRuntime(_ok_result(g["session"])))

    assert worker.process(interaction_id) is True
    row = _row(db_engine, interaction_id)
    assert (row.processing_status, row.delivery_status) == ("succeeded",
                                                            "delivered")


def test_d2_delivery_failure_maps_to_succeeded_failed(db_engine) -> None:
    """ChannelDeliveryFailedError → processing=SUCCEEDED × delivery=FAILED。"""
    g = _seed(db_engine, with_session=True)
    interaction_id = _create_pending(db_engine, g["channel"])
    worker = _worker(db_engine, _RaisingRuntime(ChannelDeliveryFailedError(
        channel_id=g["channel"], external_conversation_id="conv-1")))

    with pytest.raises(ChannelDeliveryFailedError):        # 异常原样传播
        worker.process(interaction_id)

    row = _row(db_engine, interaction_id)
    assert (row.processing_status, row.delivery_status) == ("succeeded",
                                                            "failed")


def test_d3_other_failure_maps_to_failed_pending(db_engine) -> None:
    """其他 Runtime 失败 → processing=FAILED × delivery=PENDING。"""
    g = _seed(db_engine, with_session=True)
    interaction_id = _create_pending(db_engine, g["channel"])
    worker = _worker(db_engine, _RaisingRuntime(IdentityResolutionFailedError(
        channel_id=g["channel"], external_identity_id="wx_a")))

    with pytest.raises(IdentityResolutionFailedError):
        worker.process(interaction_id)

    row = _row(db_engine, interaction_id)
    assert (row.processing_status, row.delivery_status) == ("failed", "pending")
    assert row.failure_stage == "identity_resolution"


def test_d4_worker_does_not_reinterpret_state_semantics() -> None:
    """G3：状态语义只在 phase API 一处——Worker 源码不含任何状态字面量。"""
    src = (BACKEND / "app" / "application" / "worker.py").read_text(
        encoding="utf-8")
    for token in ("succeeded", "delivered", "PROCESSING_SUCCEEDED",
                  "PROCESSING_FAILED", "DELIVERY_FAILED", "DELIVERY_DELIVERED",
                  "processing_status", "delivery_status", "failure_stage",
                  "failure_code"):
        assert token not in src, f"Worker 重新解释了状态语义：{token}"
