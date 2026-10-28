"""P1.1 / M1 -- guard failure must NOT deliver (no stale message)."""

import ast
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.application.interaction_recording import PersistingCoordinator
from app.domain.activity_record import (
    ACTIVITY_COMPLETED,
    ACTIVITY_RECEIVED,
    ACTIVITY_STAGE_FAILURE,
)
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session import Session as SessionEntity
from app.domain.session_resolution import REUSE_ACTIVE
from app.domain.user import User
from app.persistence.models.activity import ActivityModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]


class _RecordingRuntime:
    """Records execute / deliver calls (delivery is only recorded, never sent)."""

    def __init__(self, result=None) -> None:
        self.result = result
        self.execute_calls = 0
        self.deliveries = []

    def execute(self, inbound):
        self.execute_calls += 1
        return self.result

    def deliver(self, outbound) -> None:
        self.deliveries.append(outbound)


def _uow_factory(db_engine) -> Callable[[], SqlAlchemyUnitOfWork]:
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine, with_session: bool = True) -> dict:
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        identity = uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        session_id = None
        if with_session:
            session_id = uow.sessions.create(SessionEntity(
                identity_id=identity.id, channel_id=channel.id,
                external_conversation_id="conv-1", status="active")).id
        uow.commit()
        return {"channel": channel.id, "session": session_id}


def _inbound(channel_id: int) -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id="msg-1",
        external_conversation_id="conv-1", external_identity_id="wx_a",
        content="hello")


def _coord(db_engine) -> PersistingCoordinator:
    class _Never:
        def execute(self, inbound):
            raise AssertionError("no real execution in this test")

        def deliver(self, outbound):
            raise AssertionError("no real delivery in this test")

    return PersistingCoordinator(inner=_Never(),  # type: ignore[arg-type]
                                 uow_factory=_uow_factory(db_engine))


def _claim(db_engine, interaction_id: int) -> None:
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.claim_processing(interaction_id) is True
        uow.commit()


def _backdate(db_engine, interaction_id: int, seconds: int) -> None:
    with Session(db_engine) as s:
        s.execute(text(
            "UPDATE interactions SET processing_started_at = "
            "datetime('now', :minus) WHERE id = :i"),
            {"minus": f"-{seconds} seconds", "i": interaction_id})
        s.commit()


def _row(db_engine, interaction_id: int):
    with _uow_factory(db_engine)() as uow:
        return uow.interactions.get_by_id(interaction_id)


def _activities(db_engine, interaction_id: int):
    with Session(db_engine) as s:
        rows = s.execute(select(ActivityModel).where(
            ActivityModel.interaction_id == interaction_id)).scalars().all()
        return [(r.type, r.stage, r.status) for r in rows]


def _result(session_id: int, content: str) -> RuntimeExecutionResult:
    return RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=session_id,
        agent_response=AgentResponse(content=content))


# ================================================================== T1. COMMITTED

def test_t1_committed_continues_to_delivery(db_engine) -> None:
    """Fence hit (COMMITTED) -> build outbound -> deliver -> delivery terminal state."""
    g = _seed(db_engine)
    phases = _coord(db_engine)
    iid = phases.create_pending(_inbound(g["channel"]))
    _claim(db_engine, iid)
    t = _row(db_engine, iid).processing_started_at

    runtime = _RecordingRuntime()
    phases.record_agent_success_and_deliver(
        runtime, _inbound(g["channel"]), _result(g["session"], "answer-1"),
        interaction_id=iid, expected_started_at=t)

    assert len(runtime.deliveries) == 1
    assert runtime.deliveries[0].content == "answer-1"
    row = _row(db_engine, iid)
    assert (row.processing_status, row.delivery_status) == ("succeeded", "delivered")
    assert row.response_payload["content"] == "answer-1"
    types = [t for t, _, _ in _activities(db_engine, iid)]
    assert types.count(ACTIVITY_COMPLETED) == 1


# ================================================================== T2. REJECTED -> no delivery

def test_t2_rejected_never_calls_delivery(db_engine) -> None:
    """Fence mismatch (REJECTED) -> delivery is NEVER called."""
    g = _seed(db_engine)
    phases = _coord(db_engine)
    iid = phases.create_pending(_inbound(g["channel"]))
    _claim(db_engine, iid)

    runtime = _RecordingRuntime()
    returned = phases.record_agent_success_and_deliver(
        runtime, _inbound(g["channel"]), _result(g["session"], "stale-answer"),
        interaction_id=iid, expected_started_at=datetime(2000, 1, 1))

    assert returned is None              # silently stops (no exception)
    assert runtime.deliveries == []      # never delivers


# ================================================================== T3. REJECTED -> no wrong terminal state

def test_t3_rejected_does_not_write_delivery_terminal_state(db_engine) -> None:
    """REJECTED -> no delivery terminal state, no overwrite, NO fake failure."""
    g = _seed(db_engine)
    phases = _coord(db_engine)
    iid = phases.create_pending(_inbound(g["channel"]))
    _claim(db_engine, iid)
    t = _row(db_engine, iid).processing_started_at

    runtime = _RecordingRuntime()
    phases.record_agent_success_and_deliver(
        runtime, _inbound(g["channel"]), _result(g["session"], "stale-answer"),
        interaction_id=iid, expected_started_at=datetime(2000, 1, 1))

    row = _row(db_engine, iid)
    assert row.processing_status == "processing"
    assert row.delivery_status == "pending"
    assert row.processing_started_at == t
    assert row.response_payload is None            # stale response never lands
    assert row.failure_stage is None              # not recorded as delivery failure
    assert row.failure_code is None
    assert row.retryable is False

    types = [t for t, _, _ in _activities(db_engine, iid)]
    assert types == [ACTIVITY_RECEIVED]           # no stage_failure / completed
    assert ACTIVITY_STAGE_FAILURE not in types
    assert ACTIVITY_COMPLETED not in types


# ================================================================== T4. stale reclaim

def test_t4_stale_attempt_after_reclaim_never_sends(db_engine) -> None:
    """Core scenario: a reclaimed (stale) attempt must never send a message.

    A claim(T1) -> reclaim -> B claim(T2) -> B completes (deliver + terminal)
      -> A arrives late with T1 fence -> REJECTED -> no delivery, B untouched.
    """
    g = _seed(db_engine)
    phases = _coord(db_engine)
    iid = phases.create_pending(_inbound(g["channel"]))

    # 1) attempt A claim (T1), then stalls (backdate = T1 is old)
    _claim(db_engine, iid)
    _backdate(db_engine, iid, seconds=120)
    t1 = _row(db_engine, iid).processing_started_at

    # 2) reclaim: A loses ownership, row goes back to PENDING
    assert phases.reclaim_stale_processing(30) == 1

    # 3) attempt B claim (T2)
    _claim(db_engine, iid)
    t2 = _row(db_engine, iid).processing_started_at
    assert t1 is not None and t2 is not None and t1 != t2

    # 4) B completes normally
    runtime_b = _RecordingRuntime()
    phases.record_agent_success_and_deliver(
        runtime_b, _inbound(g["channel"]), _result(g["session"], "fresh-answer"),
        interaction_id=iid, expected_started_at=t2)
    assert len(runtime_b.deliveries) == 1
    assert runtime_b.deliveries[0].content == "fresh-answer"

    # 5) A arrives late with its T1 fence -> REJECTED -> never delivers
    runtime_a = _RecordingRuntime()
    phases.record_agent_success_and_deliver(
        runtime_a, _inbound(g["channel"]), _result(g["session"], "stale-answer"),
        interaction_id=iid, expected_started_at=t1)
    assert runtime_a.deliveries == []             # stale attempt sends nothing

    # 6) B's state untouched by A
    row = _row(db_engine, iid)
    assert (row.processing_status, row.delivery_status) == ("succeeded", "delivered")
    assert row.response_payload["content"] == "fresh-answer"
    assert row.processing_started_at == t2
    types = [t for t, _, _ in _activities(db_engine, iid)]
    assert types.count(ACTIVITY_COMPLETED) == 1
    assert ACTIVITY_STAGE_FAILURE not in types


# ================================================================== T5. shared orchestration

def test_t5_single_delivery_callsite_and_sync_path_agrees(db_engine) -> None:
    """One delivery call site only; the sync path uses the same orchestration."""
    src = (BACKEND / "app" / "application" / "interaction_recording.py").read_text(
        encoding="utf-8")
    ast.parse(src)
    # The orchestration method is the ONLY place that calls runtime.deliver(...)
    assert src.count("runtime.deliver(") == 1
    # Both paths (sync execute() and Worker) go through it
    assert "self.record_agent_success_and_deliver(" in src

    # sync path: same behaviour (persist -> deliver -> terminal state)
    g = _seed(db_engine)

    class _Inner:
        def __init__(self) -> None:
            self.deliveries = []

        def execute(self, inbound):
            return _result(g["session"], "sync-answer")

        def deliver(self, outbound) -> None:
            self.deliveries.append(outbound)

    inner = _Inner()
    phases = PersistingCoordinator(inner=inner, uow_factory=_uow_factory(db_engine))
    phases.execute(_inbound(g["channel"]))

    assert len(inner.deliveries) == 1
    assert inner.deliveries[0].content == "sync-answer"
    row = _row(db_engine, 1)
    assert (row.processing_status, row.delivery_status) == ("succeeded", "delivered")
    assert row.response_payload["content"] == "sync-answer"
