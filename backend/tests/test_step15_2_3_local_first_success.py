"""Step 15.2.3 —— Local First Success（external-system-free）。

验证 OSS V0.x 在**不接任何外部系统**（无 Tencent ACP / 无企业微信 / 无公网 HTTPS /
无凭据）时，能通过**完整、未改动的 Runtime 链**跑通一次真实交互：

    POST /api/channels/{id}/local/message {"external_id", "text"}
        ↓ LocalChannelAdapter.parse_message → InboundInteraction（8.1 契约）
        ↓ ChannelInboundService → PENDING + COMMIT → 200 ACK（ACK ≠ 执行）
        ↓ （响应之后）Worker → RuntimeCoordinator（完整六段）
            IdentityResolution → Routing → SessionResolution → [SessionCreation]
              → AgentDispatch（LocalAgentAdapter）→ Delivery（LocalChannelAdapter）
        ↓ interactions.response_payload（Observation 读取出口）

覆盖：
    A. LocalAgentAdapter：deterministic echo / prefix 覆写 / Fail Closed
    B. MultiAgentAdapter：按 Agent.type 路由（Runtime 不知 local / acp）
    C. LocalChannelAdapter：JSON 入站归一化 / 本地 outbox sink / Fail Closed
    D. MultiChannelAdapter：按渠道类型路由（入站 + 出站）
    E. 全链路 First Success（真实六段 + 真实 Local Adapter + Observation）
    F. Identity 不绕过（未知 external_id → IDENTITY_RESOLUTION_FAILED）
    G. P1 校验：非法 channel / agent type → 422；local 合法
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.agents.local import LocalAgentAdapter
from app.adapters.agents.registry import MultiAgentAdapter
from app.adapters.channels.local import LocalChannelAdapter
from app.adapters.channels.registry import MultiChannelAdapter
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.agents import (
    ALLOWED_AGENT_TYPES,
    AgentApplicationService,
    CreateAgent,
)
from app.application.channel_inbound import ChannelInboundService
from app.application.channels import (
    ALLOWED_CHANNEL_TYPES,
    ChannelApplicationService,
    CreateChannel,
)
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.errors import (
    AgentDispatchFailedError,
    ChannelDeliveryFailedError,
    IdentityResolutionFailedError,
    InvalidAgentInputError,
    InvalidChannelInputError,
)
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import PersistingCoordinator
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.application.worker import Worker
from app.api.runtime.channels import build_channel_inbound_api
from app.domain.agent import Agent
from app.domain.agent_request import AgentRequest
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.interaction_record import (
    DELIVERY_DELIVERED,
    DELIVERY_PENDING,
    PROCESSING_FAILED,
    PROCESSING_SUCCEEDED,
)
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.channel_inbound import (
    InboundChannelConfigError,
    InboundFormatError,
)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _session_count(db_engine) -> int:
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(SessionModel)).scalar_one()


class _SpyAgentAdapter:
    """记录调用并返回固定响应的 AgentAdapter（用于验证路由，无网络）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def send(self, request, connection) -> AgentResponse:
        self.calls.append(request.content)
        return AgentResponse(content=f"spy:{request.content}")


# ================================================================== A. LocalAgentAdapter


def test_a1_local_agent_deterministic_echo() -> None:
    """LocalAgentAdapter = deterministic echo，响应满足 AgentResponse 契约。"""
    adapter = LocalAgentAdapter()
    connection = Connection(channel_id=1, agent_id=1, name="p", id=1)
    response = adapter.send(
        AgentRequest(agent_id=1, control_plane_session_id=1, content="hello"),
        connection,
    )
    assert response.content == "Echo: hello"
    assert response.external_session_id is None
    assert response.metadata == {"protocol": "local-echo"}


def test_a2_local_agent_prefix_override_from_connection() -> None:
    """prefix 可经 Connection.config 覆写（Adapter 只从 Connection 读连接信息）。"""
    adapter = LocalAgentAdapter()
    connection = Connection(channel_id=1, agent_id=1, name="p", id=1,
                            config={"prefix": "You said: "})
    response = adapter.send(
        AgentRequest(agent_id=1, control_plane_session_id=1, content="hi"), connection)
    assert response.content == "You said: hi"


def test_a3_local_agent_fail_closed_on_blank_content() -> None:
    adapter = LocalAgentAdapter()
    connection = Connection(channel_id=1, agent_id=1, name="p", id=1)
    with pytest.raises(AgentDispatchFailedError):
        adapter.send(
            AgentRequest(agent_id=1, control_plane_session_id=1, content="   "),
            connection,
        )


# ================================================================== B. MultiAgentAdapter 路由


def test_b1_routes_by_agent_type(db_engine) -> None:
    with _uow_factory(db_engine)() as uow:
        agent = uow.agents.create(Agent(name="local-echo", type="local"))
        uow.commit()
        agent_id = agent.id
    spy = _SpyAgentAdapter()
    router = MultiAgentAdapter({"local": spy}, uow_factory=_uow_factory(db_engine))
    connection = Connection(channel_id=1, agent_id=agent_id, name="p", id=1)
    response = router.send(
        AgentRequest(agent_id=agent_id, control_plane_session_id=1, content="x"),
        connection)
    assert response.content == "spy:x"
    assert spy.calls == ["x"]


def test_b2_unknown_agent_type_fails_closed(db_engine) -> None:
    """type 无对应 Adapter → AGENT_DISPATCH_FAILED（不 fallback）。"""
    with _uow_factory(db_engine)() as uow:
        agent = uow.agents.create(Agent(name="acp-agent", type="acp"))
        uow.commit()
        agent_id = agent.id
    router = MultiAgentAdapter({"local": _SpyAgentAdapter()},
                               uow_factory=_uow_factory(db_engine))
    connection = Connection(channel_id=1, agent_id=agent_id, name="p", id=1)
    with pytest.raises(AgentDispatchFailedError):
        router.send(
            AgentRequest(agent_id=agent_id, control_plane_session_id=1, content="x"),
            connection)


# ================================================================== C. LocalChannelAdapter


def _local_channel(channel_id: int = 7) -> Channel:
    return Channel(name="local-1", type="local", id=channel_id)


def test_c1_parse_message_normalizes_json(tmp_path) -> None:
    adapter = LocalChannelAdapter(outbox_path=tmp_path / "o.jsonl")
    inbound = adapter.parse_message(
        _local_channel(), signature="", timestamp="", nonce="",
        body='{"external_id": "local-user-1", "text": "hello"}')
    assert inbound.channel_id == 7
    assert inbound.external_identity_id == "local-user-1"
    assert inbound.external_conversation_id == "local-user-1"
    assert inbound.content == "hello"
    assert inbound.external_interaction_id  # 缺省 uuid4


def test_c2_parse_message_honors_message_id(tmp_path) -> None:
    adapter = LocalChannelAdapter(outbox_path=tmp_path / "o.jsonl")
    inbound = adapter.parse_message(
        _local_channel(), signature="", timestamp="", nonce="",
        body='{"external_id": "u", "text": "x", "message_id": "m-1"}')
    assert inbound.external_interaction_id == "m-1"


@pytest.mark.parametrize("bad", [
    "not-json", "[]", "{}", '{"text": "x"}', '{"external_id": "u"}',
    '{"external_id": "  ", "text": "x"}', "",
])
def test_c3_parse_message_invalid_400_semantics(tmp_path, bad) -> None:
    """非 JSON / 非对象 / 缺字段 / 空白 → InboundFormatError（HTTP 400）。"""
    adapter = LocalChannelAdapter(outbox_path=tmp_path / "o.jsonl")
    with pytest.raises(InboundFormatError):
        adapter.parse_message(
            _local_channel(), signature="", timestamp="", nonce="", body=bad)


def test_c4_verify_handshake_echoes(tmp_path) -> None:
    adapter = LocalChannelAdapter(outbox_path=tmp_path / "o.jsonl")
    assert adapter.verify_handshake(
        _local_channel(), signature="", timestamp="", nonce="",
        echostr="plain-echo") == "plain-echo"


def test_c5_deliver_writes_local_outbox(tmp_path) -> None:
    """deliver = 真实本地 sink（append-only JSONL），不是裸 return。"""
    outbox = tmp_path / "o.jsonl"
    adapter = LocalChannelAdapter(outbox_path=outbox)
    adapter.deliver(OutboundResponse(
        channel_id=1, external_conversation_id="u-1", content="hi",
        metadata={"k": "v"}))
    lines = [ln for ln in outbox.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert json.loads(lines[0]) == {
        "channel_id": 1,
        "external_conversation_id": "u-1",
        "content": "hi",
        "metadata": {"k": "v"},
    }


def test_c6_deliver_fail_closed_on_blank_content(tmp_path) -> None:
    adapter = LocalChannelAdapter(outbox_path=tmp_path / "o.jsonl")
    with pytest.raises(ChannelDeliveryFailedError):
        adapter.deliver(OutboundResponse(
            channel_id=1, external_conversation_id="u", content="   "))


# ================================================================== D. MultiChannelAdapter 路由


def test_d1_inbound_unknown_channel_type_fails(db_engine, tmp_path) -> None:
    router = MultiChannelAdapter(
        {"local": LocalChannelAdapter(outbox_path=tmp_path / "o.jsonl")},
        uow_factory=_uow_factory(db_engine))
    with pytest.raises(InboundChannelConfigError):
        router.parse_message(
            Channel(name="wecom-1", type="wecom", id=1),
            signature="", timestamp="", nonce="", body="{}")


def test_d2_deliver_routes_by_channel_type(db_engine, tmp_path) -> None:
    with _uow_factory(db_engine)() as uow:
        channel = uow.channels.create(Channel(name="local-1", type="local"))
        uow.commit()
        channel_id = channel.id
    outbox = tmp_path / "o.jsonl"
    router = MultiChannelAdapter(
        {"local": LocalChannelAdapter(outbox_path=outbox)},
        uow_factory=_uow_factory(db_engine))
    router.deliver(OutboundResponse(
        channel_id=channel_id, external_conversation_id="u", content="hi"))
    assert json.loads(outbox.read_text(encoding="utf-8").splitlines()[0])["content"] == "hi"


def test_d3_deliver_unknown_channel_type_fails(db_engine, tmp_path) -> None:
    with _uow_factory(db_engine)() as uow:
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        uow.commit()
        channel_id = channel.id
    router = MultiChannelAdapter(
        {"local": LocalChannelAdapter(outbox_path=tmp_path / "o.jsonl")},
        uow_factory=_uow_factory(db_engine))
    with pytest.raises(ChannelDeliveryFailedError):
        router.deliver(OutboundResponse(
            channel_id=channel_id, external_conversation_id="u", content="hi"))


# ================================================================== E. 全链路 First Success


def _seed(db_engine) -> tuple[int, int]:
    """user → identity(local) → channel(local) → connection → agent(local)。"""
    with _uow_factory(db_engine)() as uow:
        user = uow.users.create(User(name="local-user"))
        channel = uow.channels.create(Channel(name="local-1", type="local"))
        agent = uow.agents.create(Agent(name="local-echo", type="local"))
        uow.identities.create(Identity(
            user_id=user.id, channel_id=channel.id, external_id="local-user-1"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="local-path",
            status="active"))
        # Implementation Package 2 / Step 2：单聊会话键 = external_id（local-user-1）
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="local-user-1",
            connection_id=path.id))
        uow.commit()
        return channel.id, agent.id


def _build_app(db_engine, outbox_path) -> TestClient:
    """真实六段 Runtime + 真实 Local（Agent / Channel）Adapter + 路由。"""
    uow = _uow_factory(db_engine)
    channel_adapter = MultiChannelAdapter(
        {"local": LocalChannelAdapter(outbox_path=outbox_path)}, uow_factory=uow)
    agent_adapter = MultiAgentAdapter(
        {"local": LocalAgentAdapter()}, uow_factory=uow)
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
    service = ChannelInboundService(
        uow_factory=uow, intake=phases, inbound_adapter=channel_adapter)
    app = FastAPI()
    app.include_router(build_channel_inbound_api(service, worker))
    return TestClient(app)


def test_e1_local_first_success_full_chain(db_engine, tmp_path) -> None:
    """POST local message → ACK → Worker → 六段 Runtime → Observation First Success。"""
    channel_id, _ = _seed(db_engine)
    outbox = tmp_path / "local_outbox.jsonl"
    client = _build_app(db_engine, outbox)

    resp = client.post(
        f"/api/channels/{channel_id}/local/message",
        json={"external_id": "local-user-1", "text": "hello"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    interaction_id = body["interaction_id"]
    assert isinstance(interaction_id, int)

    with _uow_factory(db_engine)() as uow:
        record = uow.interactions.get_by_id(interaction_id)
    # Observation（持久化证据）：完整成功
    assert record.processing_status == PROCESSING_SUCCEEDED
    assert record.delivery_status == DELIVERY_DELIVERED
    assert record.response_payload is not None
    assert record.response_payload["content"] == "Echo: hello"
    assert record.session_id is not None
    # Identity / Session 真实物化（未绕过核心模型）
    assert _session_count(db_engine) == 1

    # 本地 delivery sink 真实写入（与 Observation 互为印证）
    lines = [ln for ln in outbox.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # M6.6-C3 / ADR-12 §13：入站 metadata 的 `chat_type`（local = 单会话）经既有
    # 12.8.2 合并规则进入 OutboundResponse.metadata（channel-originated 后展开）。
    assert json.loads(lines[0]) == {
        "channel_id": channel_id,
        "external_conversation_id": "local-user-1",
        "content": "Echo: hello",
        "metadata": {"protocol": "local-echo", "chat_type": "single"},
    }


# ================================================================== F. Identity 不绕过


def test_f1_unknown_identity_fails_closed(db_engine, tmp_path) -> None:
    """未预置 Identity → IDENTITY_RESOLUTION_FAILED（绝不自动建 Identity/User）。

    生产语义：HTTP 200 ACK 先发出，Worker 之后的 typed failure 落在 interaction
    终态。TestClient 会把 background task 的异常向上抛（真实 ASGI server 只记
    日志）——两种形态都接受，核心断言是「终态 = 失败、无响应、无 Session」。
    """
    channel_id, _ = _seed(db_engine)
    outbox = tmp_path / "local_outbox.jsonl"
    client = _build_app(db_engine, outbox)

    with pytest.raises(IdentityResolutionFailedError):
        client.post(
            f"/api/channels/{channel_id}/local/message",
            json={"external_id": "ghost", "text": "hi", "message_id": "m-fail-1"},
        )

    # PENDING 已在 ACK 前落库并提交；失败终态由 Worker 的 phase API 落定。
    with _uow_factory(db_engine)() as uow:
        record = uow.interactions.find_by_channel_and_external_id(
            channel_id, "m-fail-1")
    assert record is not None
    assert record.processing_status == PROCESSING_FAILED
    assert record.failure_code == "IDENTITY_RESOLUTION_FAILED"
    assert record.response_payload is None
    assert _session_count(db_engine) == 0          # 从未创建 Session
    assert not outbox.exists()                      # Delivery 从未到达


# ================================================================== G. P1 校验


def test_g1_channel_type_controlled_set(db_engine) -> None:
    # 3B-2.2：受控集合 == 已实现的 Channel Adapter 集合（新增 wecom_bot transport）。
    assert ALLOWED_CHANNEL_TYPES == ("wecom", "local", "wecom_bot")
    service = ChannelApplicationService(uow_factory=_uow_factory(db_engine))
    with pytest.raises(InvalidChannelInputError):
        service.create_channel(CreateChannel(name="c", type="console"))
    created = service.create_channel(CreateChannel(name="c", type="local"))
    assert created.type == "local"


def test_g2_agent_type_controlled_set(db_engine) -> None:
    assert ALLOWED_AGENT_TYPES == ("acp", "local")
    service = AgentApplicationService(uow_factory=_uow_factory(db_engine))
    with pytest.raises(InvalidAgentInputError):
        service.create_agent(CreateAgent(name="a", type="openai"))
    created = service.create_agent(CreateAgent(name="a", type="local"))
    assert created.type == "local"
