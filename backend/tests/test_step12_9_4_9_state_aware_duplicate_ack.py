"""Step 12.9.4.9 —— State-Aware Duplicate Inbound ACK。

渠道重推（同一 `(channel_id, external_interaction_id)` 再次入站）不再走到
「create → UNIQUE 冲突 → IntegrityError → HTTP 500」，而是：

    不存在 → create_pending → ACK 200
    已存在 → 返回既有 id    → ACK 200 + STOP

锁定五状态冻结表（**含 retryable=True 仍 STOP**）：

    PENDING × PENDING                   → ACK 200，STOP
    PROCESSING × PENDING                → ACK 200，STOP
    SUCCEEDED × DELIVERED               → ACK 200，STOP
    FAILED × PENDING / retryable=False  → ACK 200，STOP
    FAILED × PENDING / retryable=True   → ACK 200，仍 STOP

刻意不测（本次不做）：retry / re-execution / replay / outbox / orphan reclaim。
"""

import inspect
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api.runtime.channels import build_channel_inbound_api
from app.application.channel_inbound import ChannelInboundService
from app.application.interaction_recording import PersistingCoordinator
from app.application.worker import Worker
from app.domain.agent import Agent
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.interaction_record import (
    DELIVERY_DELIVERED,
    DELIVERY_PENDING,
    InteractionRecord,
    PROCESSING_FAILED,
    PROCESSING_SUCCEEDED,
)
from app.domain.interaction_snapshot import inbound_to_snapshot
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session import Session as SessionEntity
from app.domain.session_resolution import REUSE_ACTIVE
from app.domain.user import User
from app.persistence.models.interaction import InteractionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]

STATE_PENDING = "pending"
STATE_PROCESSING = "processing"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED_NON_RETRYABLE = "failed_non_retryable"
STATE_FAILED_RETRYABLE = "failed_retryable"

ALL_STATES = (
    STATE_PENDING, STATE_PROCESSING, STATE_SUCCEEDED,
    STATE_FAILED_NON_RETRYABLE, STATE_FAILED_RETRYABLE,
)


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


class _ScriptedInboundAdapter:
    """协议无关 Fake Adapter：返回固定 InboundInteraction。"""

    def __init__(self, result: InboundInteraction | None) -> None:
        self.result = result

    def verify_handshake(self, channel, **kwargs) -> str:  # pragma: no cover
        return "echo"

    def parse_message(self, channel, **kwargs):
        return self.result


class _CountingIntake:
    """包裹真实 intake，统计 create_pending 调用次数。

    重复入站若仍走 create，计数就会 +1 —— 这是「STOP」的机器化判据。
    """

    def __init__(self, real: PersistingCoordinator) -> None:
        self._real = real
        self.calls = 0

    def create_pending(self, inbound: InboundInteraction) -> int:
        self.calls += 1
        return self._real.create_pending(inbound)


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
        uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status="active",
            config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
            credential_ref="cred-a"))
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


def _rows(db_engine) -> list:
    with _uow_factory(db_engine)() as uow:
        return list(uow.session.execute(
            select(InteractionModel).order_by(InteractionModel.id)
        ).scalars().all())


def _snapshot(db_engine, row_id: int) -> tuple:
    """既有三态快照：用于证明接纳过程**不改写**状态。"""
    with _uow_factory(db_engine)() as uow:
        row = uow.session.get(InteractionModel, row_id)
        return (row.processing_status, row.delivery_status, row.retryable)


def _plant(db_engine, channel_id: int, msg_id: str, state: str) -> int:
    """按给定状态播种一条既有行（生命周期走真实 Port 跃迁）。"""
    uowf = _uow_factory(db_engine)
    with uowf() as uow:
        row = uow.interactions.create(InteractionRecord(
            direction="inbound", channel_id=channel_id,
            external_interaction_id=msg_id,
            # 真实行必然携带入站快照（12.9.4.3）——否则 Worker 无法重建
            request_payload=inbound_to_snapshot(_inbound(channel_id, msg_id))))
        row_id = row.id
        uow.commit()
    if state == STATE_PENDING:
        return row_id
    with uowf() as uow:
        uow.interactions.claim_processing(row_id)
        uow.commit()
    if state == STATE_PROCESSING:
        return row_id
    with uowf() as uow:
        if state == STATE_SUCCEEDED:
            uow.interactions.record_outcome(
                row_id, processing_status=PROCESSING_SUCCEEDED,
                delivery_status=DELIVERY_DELIVERED)
        elif state == STATE_FAILED_NON_RETRYABLE:
            uow.interactions.record_outcome(
                row_id, processing_status=PROCESSING_FAILED,
                delivery_status=DELIVERY_PENDING,
                failure_stage="identity_resolution",
                failure_code="IDENTITY_RESOLUTION_FAILED", retryable=False)
        elif state == STATE_FAILED_RETRYABLE:
            uow.interactions.record_outcome(
                row_id, processing_status=PROCESSING_FAILED,
                delivery_status=DELIVERY_PENDING,
                failure_stage="agent_dispatch",
                failure_code="AGENT_DISPATCH_FAILED", retryable=True)
        uow.commit()
    return row_id


def _service(db_engine, adapter, intake) -> ChannelInboundService:
    return ChannelInboundService(
        uow_factory=_uow_factory(db_engine), intake=intake,
        inbound_adapter=adapter)


def _ack(service, channel_id: int) -> int | None:
    return service.handle_message(
        channel_id, signature="s", timestamp="t", nonce="n", body="body")


# ================================================================== A. 首达仍 create


def test_a1_first_arrival_creates_pending(db_engine) -> None:
    """无既有行 → 走 create_pending，落一条 PENDING。"""
    g = _seed(db_engine)
    real = PersistingCoordinator(inner=None, uow_factory=_uow_factory(db_engine))
    intake = _CountingIntake(real)
    service = _service(db_engine, _ScriptedInboundAdapter(
        _inbound(g["channel"], "msg-1")), intake)

    interaction_id = _ack(service, g["channel"])

    assert intake.calls == 1
    assert interaction_id is not None
    rows = _rows(db_engine)
    assert len(rows) == 1
    assert rows[0].id == interaction_id
    assert rows[0].processing_status == "pending"


def test_a2_different_external_id_creates_separately(db_engine) -> None:
    """不同 external_interaction_id = 不同交互，各自 create（不被误判为重推）。"""
    g = _seed(db_engine)
    real = PersistingCoordinator(inner=None, uow_factory=_uow_factory(db_engine))
    first = _service(db_engine, _ScriptedInboundAdapter(
        _inbound(g["channel"], "msg-1")), _CountingIntake(real))
    second = _service(db_engine, _ScriptedInboundAdapter(
        _inbound(g["channel"], "msg-2")), _CountingIntake(real))

    id_a = _ack(first, g["channel"])
    id_b = _ack(second, g["channel"])

    assert id_a != id_b
    assert len(_rows(db_engine)) == 2


def test_a3_same_external_id_different_channel_is_not_duplicate(db_engine) -> None:
    """身份 scope = Channel：不同渠道的同名外部 ID 是两次不同交互。"""
    g = _seed(db_engine)
    real = PersistingCoordinator(inner=None, uow_factory=_uow_factory(db_engine))
    with _uow_factory(db_engine)() as uow:
        other = uow.channels.create(Channel(name="wecom-2", type="wecom")).id
        uow.commit()

    id_a = _ack(_service(db_engine, _ScriptedInboundAdapter(
        _inbound(g["channel"], "msg-1")), _CountingIntake(real)), g["channel"])
    id_b = _ack(_service(db_engine, _ScriptedInboundAdapter(
        _inbound(other, "msg-1")), _CountingIntake(real)), other)

    assert id_a != id_b
    assert len(_rows(db_engine)) == 2


# ================================================================== B. 五状态重复 → ACK + STOP


@pytest.mark.parametrize("state", ALL_STATES)
def test_b_five_states_duplicate_returns_existing_and_stops(
        db_engine, state: str) -> None:
    """五状态冻结表：重复入站 → 返回既有 id + 不 create + 不改写状态。"""
    g = _seed(db_engine)
    planted = _plant(db_engine, g["channel"], "msg-1", state)
    before = _snapshot(db_engine, planted)

    real = PersistingCoordinator(inner=None, uow_factory=_uow_factory(db_engine))
    intake = _CountingIntake(real)
    service = _service(db_engine, _ScriptedInboundAdapter(
        _inbound(g["channel"], "msg-1")), intake)

    got = _ack(service, g["channel"])

    assert got == planted            # ACK 的是**既有**那一条
    assert intake.calls == 0         # STOP：不再 create
    assert len(_rows(db_engine)) == 1
    assert _snapshot(db_engine, planted) == before      # 状态未被改写


def test_b_terminal_duplicate_never_reaches_intake(db_engine) -> None:
    """终态重复：intake 完全不被触达（不是「create 后被 UNIQUE 拒绝」）。"""
    g = _seed(db_engine)
    _plant(db_engine, g["channel"], "msg-1", STATE_SUCCEEDED)
    real = PersistingCoordinator(inner=None, uow_factory=_uow_factory(db_engine))
    intake = _CountingIntake(real)
    service = _service(db_engine, _ScriptedInboundAdapter(
        _inbound(g["channel"], "msg-1")), intake)

    _ack(service, g["channel"])
    _ack(service, g["channel"])

    assert intake.calls == 0         # 两次重推都未落库


# ================================================================== C. retryable=True 仍 STOP


def test_c1_retryable_failure_duplicate_does_not_reenroll_worker(db_engine) -> None:
    """**本步最关键一条**：retryable=True 的失败行，重推**不**重新进入 Worker。

    重试 / 重执行是独立裁决；接纳层不得借 retryable 偷偷把重推变成重试。
    """
    g = _seed(db_engine, with_session=True)
    planted = _plant(db_engine, g["channel"], "msg-1", STATE_FAILED_RETRYABLE)

    runtime = _OkRuntime(RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=g["session"],
        agent_response=AgentResponse(content="hi")))
    uowf = _uow_factory(db_engine)
    phases = PersistingCoordinator(inner=None, uow_factory=uowf)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=uowf)

    real = PersistingCoordinator(inner=None, uow_factory=uowf)
    service = _service(db_engine, _ScriptedInboundAdapter(
        _inbound(g["channel"], "msg-1")), _CountingIntake(real))

    got = _ack(service, g["channel"])

    assert got == planted
    assert worker.process(got) is False       # 未被 claim
    assert runtime.calls == 0                 # ← 一次都没执行
    assert _snapshot(db_engine, planted) == ("failed", "pending", True)


@pytest.mark.parametrize("state", [STATE_PROCESSING, STATE_SUCCEEDED,
                                   STATE_FAILED_NON_RETRYABLE])
def test_c2_other_states_also_do_not_reenroll_worker(db_engine, state: str) -> None:
    """PROCESSING / 成功终态 / 不可重试失败：重推同样不重新执行。"""
    g = _seed(db_engine, with_session=True)
    planted = _plant(db_engine, g["channel"], "msg-1", state)

    runtime = _OkRuntime(RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=g["session"],
        agent_response=AgentResponse(content="hi")))
    uowf = _uow_factory(db_engine)
    worker = Worker(runtime=runtime,
                    phases=PersistingCoordinator(inner=None, uow_factory=uowf),
                    uow_factory=uowf)
    service = _service(db_engine, _ScriptedInboundAdapter(
        _inbound(g["channel"], "msg-1")),
        _CountingIntake(PersistingCoordinator(inner=None, uow_factory=uowf)))

    got = _ack(service, g["channel"])

    assert got == planted
    assert worker.process(got) is False
    assert runtime.calls == 0


def test_c3_pending_duplicate_still_executes_exactly_once(db_engine) -> None:
    """PENDING 重复：HTTP 会给同一 id 挂两次 Worker 任务 → CAS 保证只执行一次。

    「STOP」指不 create 第二条；已存在的 PENDING 仍会被正常消费，且**恰好一次**。
    """
    g = _seed(db_engine, with_session=True)
    planted = _plant(db_engine, g["channel"], "msg-1", STATE_PENDING)

    runtime = _OkRuntime(RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=g["session"],
        agent_response=AgentResponse(content="hi")))
    uowf = _uow_factory(db_engine)
    worker = Worker(runtime=runtime,
                    phases=PersistingCoordinator(inner=None, uow_factory=uowf),
                    uow_factory=uowf)
    service = _service(db_engine, _ScriptedInboundAdapter(
        _inbound(g["channel"], "msg-1")),
        _CountingIntake(PersistingCoordinator(inner=None, uow_factory=uowf)))

    got = _ack(service, g["channel"])
    assert got == planted

    first = worker.process(got)       # 模拟 BackgroundTasks
    second = worker.process(got)      # 模拟重推又挂了一次任务

    assert (first, second) == (True, False)
    assert runtime.calls == 1         # ← 恰好一次，绝不双执行


# ================================================================== D. HTTP 层：两次都 200


def test_d1_duplicate_inbound_returns_200_not_500(db_engine) -> None:
    """生产缺陷的机器化证明：同一条消息 POST 两次 → **两次都是 200**，不是 500。"""
    g = _seed(db_engine, with_session=True)
    uowf = _uow_factory(db_engine)
    runtime = _OkRuntime(RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=g["session"],
        agent_response=AgentResponse(content="hi")))
    phases = PersistingCoordinator(inner=None, uow_factory=uowf)
    service = _service(db_engine, _ScriptedInboundAdapter(
        _inbound(g["channel"], "msg-1")),
        _CountingIntake(PersistingCoordinator(inner=None, uow_factory=uowf)))

    app = FastAPI()
    app.include_router(build_channel_inbound_api(
        service=service,
        worker=Worker(runtime=runtime, phases=phases, uow_factory=uowf)))
    client = TestClient(app)
    url = f"/api/channels/{g['channel']}/wecom/webhook"

    first = client.post(f"{url}?msg_signature=s&timestamp=t&nonce=n", content="b")
    second = client.post(f"{url}?msg_signature=s&timestamp=t&nonce=n", content="b")

    assert first.status_code == 200
    assert second.status_code == 200      # ← 修复前这里是 500
    assert len(_rows(db_engine)) == 1     # 未产生第二条
    assert runtime.calls == 1             # BackgroundTasks 两次 → 仍只执行一次


# ================================================================== E. 层级守卫


def test_e1_admission_layer_does_not_interpret_state() -> None:
    """接纳层只回答「是否已有」，**不解释**状态——源码级锁定。

    状态判定一旦进入接纳层，「重推 = 重试机会」这个未决语义就会被偷偷编码。
    """
    src = inspect.getsource(ChannelInboundService._existing_row_id)
    for token in ("succeeded", "failed", "delivered", "retryable",
                  "processing_status", "delivery_status"):
        assert token not in src.lower(), f"接纳层开始解释状态：{token}"


def test_e2_persistence_phase_api_still_makes_no_decision() -> None:
    """10.5.3 冻结保持：持久化写入边界仍无 lookup→decide、无 IntegrityError。"""
    src = (BACKEND / "app" / "application" / "interaction_recording.py"
           ).read_text(encoding="utf-8")
    # 精确形式：禁的是 interactions 读回（`identities.` 的同名方法是既有合法调用）
    assert "interactions.find_by_channel_and_external_id" not in src
    assert "IntegrityError" not in src


def test_e3_channel_inbound_imports_unchanged() -> None:
    """接纳逻辑不新增 import：复用既有 UoW 读路径，不触碰持久化 Port。"""
    src = (BACKEND / "app" / "application" / "channel_inbound.py"
           ).read_text(encoding="utf-8")
    assert "app.repositories.interaction_record" not in src
    assert "app.persistence" not in src
    assert "InteractionRecord" not in src
