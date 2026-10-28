"""P1 -- Delivery Lifecycle Separation (C1/C3/C6) dedicated tests."""

import dataclasses
from typing import Callable

import pytest

from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor, build_outbound_response
from app.application.errors import (
    AgentDispatchFailedError,
    ChannelDeliveryFailedError,
)
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
from app.domain.user import User
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork


def _factory(db_engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=db_engine, expire_on_commit=False)


def _uow_factory(db_engine) -> Callable[[], SqlAlchemyUnitOfWork]:
    return lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))


def _seed(db_engine) -> dict:
    """Minimal chain: user / channel / agent / identity / connection / grant."""
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
        # Implementation Package 2 / Step 2: default-deny requires an explicit grant
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="conv-1",
            connection_id=path.id))
        uow.commit()
        return {"channel": channel.id, "identity": identity.id}


def _inbound(channel_id: int, content: str = "hello") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id="msg-1",
        external_conversation_id="conv-1", external_identity_id="wx_a",
        content=content)


class _EchoAgentAdapter:
    """Fake agent that records its call count."""

    def __init__(self, content: str = "echo:hello") -> None:
        self.content = content
        self.calls = 0

    def send(self, request, connection) -> AgentResponse:
        self.calls += 1
        return AgentResponse(content=self.content,
                             external_session_id="agent-session-1")


class _FailingAgentAdapter:
    def send(self, request, connection) -> AgentResponse:
        raise AgentDispatchFailedError(agent_id=request.agent_id)


class _RecordingChannelAdapter:
    """Fake channel that records deliveries; can inject a delivery failure."""

    def __init__(self, failure: Exception | None = None,
                 on_deliver: Callable[[], None] | None = None) -> None:
        self.deliveries: list[OutboundResponse] = []
        self._failure = failure
        self._on_deliver = on_deliver

    def deliver(self, outbound: OutboundResponse) -> None:
        if self._on_deliver is not None:
            self._on_deliver()
        self.deliveries.append(outbound)
        if self._failure is not None:
            raise self._failure


def _build(db_engine, *, agent_adapter, channel_adapter):
    uow = _uow_factory(db_engine)
    runtime = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=agent_adapter, uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=uow)
    return uow, phases, worker


def _activities(db_engine, interaction_id: int):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.persistence.models.activity import ActivityModel

    with Session(db_engine) as s:
        rows = s.execute(
            select(ActivityModel).where(
                ActivityModel.interaction_id == interaction_id)
        ).scalars().all()
        return [(r.type, r.stage, r.status) for r in rows]


def test_1_success_path_persists_result_then_delivers(db_engine) -> None:
    """Happy path: result persisted first, then delivery succeeds."""
    g = _seed(db_engine)
    agent, channel = _EchoAgentAdapter(), _RecordingChannelAdapter()
    uow, phases, worker = _build(
        db_engine, agent_adapter=agent, channel_adapter=channel)

    interaction_id = phases.create_pending(_inbound(g["channel"]))
    assert worker.process(interaction_id) is True

    with uow() as u:
        rec = u.interactions.get_by_id(interaction_id)
    assert rec.processing_status == "succeeded"
    assert rec.delivery_status == "delivered"
    assert rec.response_payload is not None
    assert rec.response_payload["content"] == "echo:hello"
    assert agent.calls == 1
    assert len(channel.deliveries) == 1
    assert channel.deliveries[0].content == "echo:hello"
    types = [t for t, _, _ in _activities(db_engine, interaction_id)]
    assert types[0] == "interaction_received"
    assert types[-1] == "interaction_completed"


def test_2_delivery_failure_keeps_persisted_result(db_engine) -> None:
    """Core regression: Agent success + delivery failure -> result is KEPT.

    Before P1 the payload was always NULL on delivery failure (production).
    """
    g = _seed(db_engine)
    agent = _EchoAgentAdapter()
    channel = _RecordingChannelAdapter(
        failure=ChannelDeliveryFailedError(channel_id=g["channel"],
                                          external_conversation_id="conv-1"))
    uow, phases, worker = _build(
        db_engine, agent_adapter=agent, channel_adapter=channel)

    interaction_id = phases.create_pending(_inbound(g["channel"]))
    with pytest.raises(ChannelDeliveryFailedError):
        worker.process(interaction_id)

    with uow() as u:
        rec = u.interactions.get_by_id(interaction_id)
    assert rec.processing_status == "succeeded"      # Agent succeeded
    assert rec.delivery_status == "failed"             # delivery failed (separate)
    assert rec.failure_stage == "delivery"
    assert rec.failure_code == "CHANNEL_DELIVERY_FAILED"
    assert rec.response_payload is not None            # result NOT lost
    assert rec.response_payload["content"] == "echo:hello"
    assert agent.calls == 1
    assert len(channel.deliveries) == 1


def test_3_unknown_ack_result_keeps_result_and_does_not_resend(db_engine) -> None:
    """ACK timeout (unknown outcome): result kept; NO automatic resend."""
    g = _seed(db_engine)
    agent = _EchoAgentAdapter()
    unknown = ChannelDeliveryFailedError(
        channel_id=g["channel"], external_conversation_id="conv-1",
        retryable=True, outcome_known=False)
    channel = _RecordingChannelAdapter(failure=unknown)
    uow, phases, worker = _build(
        db_engine, agent_adapter=agent, channel_adapter=channel)

    interaction_id = phases.create_pending(_inbound(g["channel"]))
    with pytest.raises(ChannelDeliveryFailedError):
        worker.process(interaction_id)

    with uow() as u:
        rec = u.interactions.get_by_id(interaction_id)
    assert rec.response_payload is not None
    assert rec.response_payload["content"] == "echo:hello"
    # P2：ACK 超时 = 结果**未知** ⇒ 落 delivery=unknown（**永不**自动重投）
    assert rec.delivery_status == "unknown"
    assert len(channel.deliveries) == 1        # no automatic resend
    assert agent.calls == 1                     # Agent not re-run


def test_4_agent_failure_fabricates_nothing(db_engine) -> None:
    """Agent failure: processing=failed x delivery=pending, NO payload."""
    g = _seed(db_engine)
    channel = _RecordingChannelAdapter()
    uow, phases, worker = _build(
        db_engine, agent_adapter=_FailingAgentAdapter(),
        channel_adapter=channel)

    interaction_id = phases.create_pending(_inbound(g["channel"]))
    with pytest.raises(AgentDispatchFailedError):
        worker.process(interaction_id)

    with uow() as u:
        rec = u.interactions.get_by_id(interaction_id)
    assert rec.processing_status == "failed"
    assert rec.delivery_status == "pending"
    assert rec.response_payload is None          # nothing fabricated
    assert channel.deliveries == []              # Agent failure -> NO Delivery


def test_5_result_is_persisted_before_delivery_is_attempted(db_engine) -> None:
    """Ordering: at the moment delivery is called, the payload is ALREADY stored."""
    g = _seed(db_engine)
    observed: dict = {}

    def probe() -> None:
        with _uow_factory(db_engine)() as uow:
            rec = uow.interactions.find_by_channel_and_external_id(
                g["channel"], "msg-1")
            observed["payload_at_delivery_time"] = (
                None if rec is None else rec.response_payload)
            observed["delivery_status_at_delivery_time"] = (
                None if rec is None else rec.delivery_status)

    agent = _EchoAgentAdapter()
    channel = _RecordingChannelAdapter(on_deliver=probe)
    uow, phases, worker = _build(
        db_engine, agent_adapter=agent, channel_adapter=channel)

    interaction_id = phases.create_pending(_inbound(g["channel"]))
    assert worker.process(interaction_id) is True

    assert observed["payload_at_delivery_time"] is not None
    assert observed["payload_at_delivery_time"]["content"] == "echo:hello"
    # P2：投递已被 claim ⇒ 该时刻状态是 delivering（attempt 在途）
    assert observed["delivery_status_at_delivery_time"] == "delivering"


def test_6_delivery_failure_does_not_re_execute_agent(db_engine) -> None:
    """A delivery failure never re-runs the Agent (Recovery Driver is P2)."""
    g = _seed(db_engine)
    agent = _EchoAgentAdapter()
    channel = _RecordingChannelAdapter(
        failure=ChannelDeliveryFailedError(channel_id=g["channel"],
                                          external_conversation_id="conv-1"))
    uow, phases, worker = _build(
        db_engine, agent_adapter=agent, channel_adapter=channel)

    interaction_id = phases.create_pending(_inbound(g["channel"]))
    with pytest.raises(ChannelDeliveryFailedError):
        worker.process(interaction_id)
    assert worker.process(interaction_id) is False   # terminal -> no re-execution
    assert agent.calls == 1
    assert len(channel.deliveries) == 1


def test_7_runtime_execution_result_field_face_is_three() -> None:
    """RuntimeExecutionResult keeps exactly three fields (P1 adds none)."""
    names = [f.name for f in dataclasses.fields(RuntimeExecutionResult)]
    assert names == ["session_mode", "session_id", "agent_response"]


def test_8_build_outbound_response_fields_and_metadata_precedence() -> None:
    """Pure function: fields correct; channel metadata WINS over agent metadata."""
    inbound = InboundInteraction(
        channel_id=7, external_interaction_id="m1",
        external_conversation_id="conv-1", external_identity_id="wx_a",
        content="hi",
        metadata={"bot_reply": {"req_id": "REQ-1"}, "shared": "from-channel"})
    agent_response = AgentResponse(
        content="echo:hi", external_session_id="agent-session-1",
        metadata={"shared": "from-agent", "agent_note": "keep-me"})

    outbound = build_outbound_response(inbound, agent_response)
    assert outbound.channel_id == 7
    assert outbound.external_conversation_id == "conv-1"
    assert outbound.content == "echo:hi"
    assert outbound.metadata["shared"] == "from-channel"      # 12.8.2
    assert outbound.metadata["agent_note"] == "keep-me"
    assert outbound.metadata["bot_reply"] == {"req_id": "REQ-1"}

    bare = build_outbound_response(
        InboundInteraction(channel_id=1, external_interaction_id="m",
                           external_conversation_id="c",
                           external_identity_id="i", content="x"),
        AgentResponse(content="y"))
    assert bare.metadata is None
