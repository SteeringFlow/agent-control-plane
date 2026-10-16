"""Step 9.8.2 —— Runtime Coordinator Implementation。

`RuntimeExecutionCoordinator`（9.1 边界）第一次获得真正实现 `RuntimeCoordinator`：
把已独立验收的六段能力按 `RUNTIME_EXECUTION_ORDER` 串起来。不设计新 Runtime 语义，
全部使用已冻结组件；仍不接真实 ACP/WeCom——用 Test Agent/Channel Adapter。

Coordinator = Orchestrator，不是 Domain Service：本模块不 import Repository /
Adapter / Persistence / SQLAlchemy，六段依赖全部注入。

锁定项：
A  真实 E2E：REUSE_ACTIVE 分支（复用 existing_session_id，不创建）；
B  真实 E2E：CREATE_NEW 分支（物化 Session，把新 id 交给 Dispatch）；
C  调用顺序来自**真实执行**（录制 fake executor 的 calls）：
   CREATE_NEW → identity/routing/session/session_creation/dispatch/delivery；
   REUSE     → identity/routing/session/dispatch/delivery（无 session_creation）；
D  失败传播（typed error 原样上抛，保留 stage 语义，观察停止点）：
   Identity/Routing/Session/Session Creation → STOP；Dispatch → NO Delivery；
   Delivery 失败 → Dispatch 已成功、Delivery 抛 ChannelDeliveryFailedError；
E  不变量：进入 Dispatch 的 control_plane_session_id 绝不可能是 None；
F  模块边界：无 Repository/Adapter/ORM；只调 executor.execute；无 None 控制流。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.application import RuntimeCoordinator
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.errors import (
    AccessAuthorizationDeniedError,
    AgentDispatchFailedError,
    ChannelDeliveryFailedError,
    IdentityResolutionFailedError,
    RoutingFailedError,
    SessionResolutionFailedError,
)
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.access_authorization import AccessAuthorization
from app.domain.agent import Agent
from app.domain.agent_request import AgentRequest
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.identity_resolution import IdentityResolution
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.routing_result import RoutingResult
from app.domain.session import Session as DomainSession
from app.domain.session_resolution import CREATE_NEW, REUSE_ACTIVE, SessionResolution
from app.domain.user import User
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]
COORDINATOR_PY = APP_DIR / "application" / "coordinator.py"


# ------------------------------------------------------------------ 测试底座


class _TestAgentAdapter:
    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []

    def send(self, request: AgentRequest, connection: Connection) -> AgentResponse:
        self.requests.append(request)
        return AgentResponse(content="test response", external_session_id="agent-s-1")


class _FailingAgentAdapter:
    def send(self, request: AgentRequest, connection: Connection) -> AgentResponse:
        raise RuntimeError("agent transport down")


class _TestChannelAdapter:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


class _FailingChannelAdapter:
    def deliver(self, outbound: OutboundResponse) -> None:
        raise RuntimeError("channel transport down")


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))


def _seed(db_engine, *, conversation: str = "conversation-X") -> dict[str, int]:
    """1 User / 1 Channel / 1 Identity(wx_1) / 2 Agent / 1 active Connection(ch→a1)。

    Implementation Package 2 / Step 2：同时播种该会话的**授权**（显式前提）——
    default-deny 下没有 grant 就不会进入 Routing，成功路径必须显式被授权。
    """
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        user = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        a1 = uow.agents.create(Agent(name="agent-1", type="acp"))
        a2 = uow.agents.create(Agent(name="agent-2", type="acp"))
        identity = uow.identities.create(Identity(
            user_id=user.id, channel_id=channel.id, external_id="wx_1"))
        conn = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=a1.id, name="path-1"))
        uow.connections.create(Connection(
            channel_id=channel.id, agent_id=a2.id, name="path-2", status="inactive"))
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id=conversation,
            connection_id=conn.id))
        uow.commit()
        return {"user": user.id, "channel": channel.id, "identity": identity.id,
                "agent1": a1.id, "connection": conn.id}


def _real_coordinator(db_engine, agent_adapter, channel_adapter) -> RuntimeCoordinator:
    uow = _uow_factory(db_engine)
    return RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(agent_adapter=agent_adapter,
                                             uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )


def _inbound(channel_id: int, conversation: str = "conversation-X") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id,
        external_interaction_id="i-1",
        external_conversation_id=conversation,
        external_identity_id="wx_1",
        content="hello agent",
    )


def _count(db_engine) -> int:
    from sqlalchemy import func, select
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(SessionModel)
        ).scalar_one()


def _get_session(db_engine, session_id: int):
    with Session(db_engine) as session:
        return session.get(SessionModel, session_id)


# ------------------------------------------------------------------ A/B 真实 E2E


def test_happy_path_reuse_existing_session(db_engine) -> None:
    g = _seed(db_engine)
    # 先物化一条 session（等价于此前 CREATE_NEW 已发生）
    pre = SessionCreationExecutor(_uow_factory(db_engine)).execute(
        identity_id=g["identity"], channel_id=g["channel"],
        connection_id=g["connection"], agent_id=g["agent1"],
        external_conversation_id="conversation-X")
    agent_adapter = _TestAgentAdapter()
    channel_adapter = _TestChannelAdapter()
    before = _count(db_engine)

    _real_coordinator(db_engine, agent_adapter, channel_adapter).execute(
        _inbound(g["channel"]))

    assert _count(db_engine) == before  # REUSE 分支不创建新 session
    request = agent_adapter.requests[-1]
    assert request.control_plane_session_id == pre.id
    assert request.agent_id == g["agent1"]
    outbound = channel_adapter.deliveries[-1]
    assert outbound.channel_id == g["channel"]
    assert outbound.external_conversation_id == "conversation-X"
    assert outbound.content == "test response"


def test_happy_path_create_new_session(db_engine) -> None:
    g = _seed(db_engine)
    agent_adapter = _TestAgentAdapter()
    channel_adapter = _TestChannelAdapter()
    before = _count(db_engine)

    _real_coordinator(db_engine, agent_adapter, channel_adapter).execute(
        _inbound(g["channel"]))

    assert _count(db_engine) == before + 1  # CREATE_NEW 物化一条
    cp_session_id = agent_adapter.requests[-1].control_plane_session_id
    assert isinstance(cp_session_id, int) and cp_session_id > 0
    row = _get_session(db_engine, cp_session_id)
    assert row is not None, "Dispatch 的 control_plane_session_id 必须对应真实 Session"
    assert row.identity_id == g["identity"]
    assert row.connection_id == g["connection"]
    assert row.agent_id == g["agent1"]
    assert channel_adapter.deliveries[-1].content == "test response"


# ------------------------------------------------------------------ C 调用顺序（真实执行录制）


class _Fakes:
    """按 RUNTIME_EXECUTION_ORDER 录制的 fake 执行器（fail 可注入 typed error）。"""

    def __init__(self, *, session_mode=CREATE_NEW, existing_session_id=None,
                 identity_fail=None, authorization_fail=None, routing_fail=None,
                 session_fail=None,
                 creation_fail=None, dispatch_fail=None, delivery_fail=None,
                 agent_adapter=None, channel_adapter=None):
        self.calls: list[str] = []
        self.last_cp_session_id = None
        self.identity_fail = identity_fail
        self.authorization_fail = authorization_fail
        self.routing_fail = routing_fail
        self.session_fail = session_fail
        self.creation_fail = creation_fail
        self.dispatch_fail = dispatch_fail
        self.delivery_fail = delivery_fail
        self.session_mode = session_mode
        self.existing_session_id = existing_session_id
        self.agent_adapter = agent_adapter or _TestAgentAdapter()
        self.channel_adapter = channel_adapter or _TestChannelAdapter()

    # ---- stages ----
    def identity(self, channel_id: int, external_identity_id: str) -> IdentityResolution:
        self.calls.append("identity")
        if self.identity_fail:
            raise self.identity_fail
        return IdentityResolution(
            identity=Identity(user_id=1, channel_id=channel_id,
                              external_id=external_identity_id, id=5),
            user=User(name="alice", id=1))

    def authorization(self, channel_id: int,
                      external_conversation_id: str) -> AccessAuthorization:
        self.calls.append("authorization")
        if self.authorization_fail:
            raise self.authorization_fail
        # 许可集合与下方 routing 的 connection_id=10 对齐（Router 会与 active 取交集）
        return AccessAuthorization(granted_connection_ids=frozenset({10}))

    def routing(self, channel_id: int,
                granted_connection_ids: frozenset[int]) -> RoutingResult:
        self.calls.append("routing")
        if self.routing_fail:
            raise self.routing_fail
        assert 10 in granted_connection_ids, "Router 必须收到授权候选集"
        return RoutingResult(connection_id=10, agent_id=20)

    def session(self, identity_id: int, connection_id: int,
                external_conversation_id: str) -> SessionResolution:
        self.calls.append("session")
        if self.session_fail:
            raise self.session_fail
        return SessionResolution(
            mode=self.session_mode, identity_id=identity_id,
            connection_id=connection_id,
            external_conversation_id=external_conversation_id,
            existing_session_id=self.existing_session_id)

    def session_creation(self, **kwargs) -> DomainSession:
        self.calls.append("session_creation")
        if self.creation_fail:
            raise self.creation_fail
        return DomainSession(
            identity_id=kwargs["identity_id"], channel_id=kwargs["channel_id"],
            connection_id=kwargs["connection_id"], agent_id=kwargs["agent_id"],
            external_conversation_id=kwargs["external_conversation_id"], id=300)

    def dispatch(self, routing_result: RoutingResult, control_plane_session_id: int,
                 content: str, external_session_id: str | None = None,
                 metadata: dict | None = None) -> AgentResponse:
        self.calls.append("dispatch")
        self.last_cp_session_id = control_plane_session_id
        if self.dispatch_fail:
            raise self.dispatch_fail
        request = AgentRequest(agent_id=routing_result.agent_id,
                               control_plane_session_id=control_plane_session_id,
                               content=content)
        return self.agent_adapter.send(request, Connection(
            channel_id=1, agent_id=routing_result.agent_id, name="p", id=10))

    def delivery(self, outbound: OutboundResponse) -> None:
        self.calls.append("delivery")
        if self.delivery_fail:
            raise self.delivery_fail
        self.channel_adapter.deliver(outbound)

    def coordinator(self) -> RuntimeCoordinator:
        return RuntimeCoordinator(
            identity_resolution=_StageAdapter(self.identity),
            access_authorization=_StageAdapter(self.authorization),
            routing=_StageAdapter(self.routing),
            session_resolution=_StageAdapter(self.session),
            session_creation=_StageAdapter(self.session_creation),
            agent_dispatch=_StageAdapter(self.dispatch),
            delivery=_StageAdapter(self.delivery),
        )


class _StageAdapter:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


def test_call_order_create_new() -> None:
    fakes = _Fakes(session_mode=CREATE_NEW)
    fakes.coordinator().execute(_inbound(7))
    assert fakes.calls == ["identity", "authorization", "routing", "session",
                           "session_creation", "dispatch", "delivery"]
    assert fakes.last_cp_session_id == 300


def test_call_order_reuse_active() -> None:
    fakes = _Fakes(session_mode=REUSE_ACTIVE, existing_session_id=100)
    fakes.coordinator().execute(_inbound(7))
    assert fakes.calls == ["identity", "authorization", "routing", "session",
                           "dispatch", "delivery"]
    assert fakes.last_cp_session_id == 100


# ------------------------------------------------------------------ D 失败传播


def test_identity_failure_stops_before_routing(db_engine) -> None:
    fakes = _Fakes(identity_fail=IdentityResolutionFailedError(7, "wx"))
    with pytest.raises(IdentityResolutionFailedError):
        fakes.coordinator().execute(_inbound(7))
    assert fakes.calls == ["identity"]


def test_authorization_failure_stops_before_routing() -> None:
    """未授权 → 停在 Authorization：**不 Routing、不建 Session、不 Dispatch、不投递**。

    Implementation Package 2 / Step 2（ADR-7/ADR-8）：这是 `UNAUTHORIZED` 的
    Runtime 停止点证据——授权拒绝必须在 Router 之前截断整条管线。
    """
    fakes = _Fakes(authorization_fail=AccessAuthorizationDeniedError(7, "X"))
    with pytest.raises(AccessAuthorizationDeniedError):
        fakes.coordinator().execute(_inbound(7))
    assert fakes.calls == ["identity", "authorization"]
    assert fakes.channel_adapter.deliveries == []
    assert fakes.agent_adapter.requests == []


def test_routing_failure_stops_before_session() -> None:
    fakes = _Fakes(routing_fail=RoutingFailedError(7))
    with pytest.raises(RoutingFailedError):
        fakes.coordinator().execute(_inbound(7))
    # NO Session / NO Dispatch / NO Delivery
    assert fakes.calls == ["identity", "authorization", "routing"]


def test_session_failure_stops_before_creation_and_dispatch() -> None:
    fakes = _Fakes(session_fail=SessionResolutionFailedError(5, 10, "X"))
    with pytest.raises(SessionResolutionFailedError):
        fakes.coordinator().execute(_inbound(7))
    assert fakes.calls == ["identity", "authorization", "routing", "session"]


def test_session_creation_failure_stops_before_dispatch() -> None:
    fakes = _Fakes(session_mode=CREATE_NEW,
                   creation_fail=SessionResolutionFailedError(5, 10, "X"))
    with pytest.raises(SessionResolutionFailedError):
        fakes.coordinator().execute(_inbound(7))
    assert fakes.calls == ["identity", "authorization", "routing", "session",
                           "session_creation"]


def test_dispatch_failure_means_no_delivery() -> None:
    fakes = _Fakes(session_mode=REUSE_ACTIVE, existing_session_id=100,
                   dispatch_fail=AgentDispatchFailedError(20, 10))
    with pytest.raises(AgentDispatchFailedError):
        fakes.coordinator().execute(_inbound(7))
    assert fakes.calls == ["identity", "authorization", "routing", "session",
                           "dispatch"]
    assert fakes.channel_adapter.deliveries == []  # NO Delivery


def test_delivery_failure_after_dispatch_success() -> None:
    fakes = _Fakes(session_mode=REUSE_ACTIVE, existing_session_id=100,
                   delivery_fail=ChannelDeliveryFailedError(7, "X"))
    with pytest.raises(ChannelDeliveryFailedError):
        fakes.coordinator().execute(_inbound(7))
    # Dispatch 已成功（请求已发给 agent），Delivery 才失败
    assert fakes.calls == ["identity", "authorization", "routing", "session",
                           "dispatch", "delivery"]
    assert fakes.last_cp_session_id == 100
    assert fakes.agent_adapter.requests, "Dispatch 应已成功（才进入 Delivery）"
    assert fakes.channel_adapter.deliveries == []


def test_real_delivery_failure_after_dispatch_success(db_engine) -> None:
    """真实执行器 + 失败 Channel Adapter：Dispatch 成功后 Delivery 抛错。"""
    g = _seed(db_engine)
    agent_adapter = _TestAgentAdapter()
    with pytest.raises(ChannelDeliveryFailedError):
        _real_coordinator(db_engine, agent_adapter, _FailingChannelAdapter()).execute(
            _inbound(g["channel"]))
    assert agent_adapter.requests, "Dispatch 已成功（才进入 Delivery）"


# ------------------------------------------------------------------ E 不变量


def test_cp_session_id_never_none() -> None:
    for mode, existing, expected in ((REUSE_ACTIVE, 100, 100), (CREATE_NEW, None, 300)):
        fakes = _Fakes(session_mode=mode, existing_session_id=existing)
        fakes.coordinator().execute(_inbound(7))
        assert fakes.last_cp_session_id == expected
        assert fakes.last_cp_session_id is not None


# ------------------------------------------------------------------ F 模块边界


def _roots(src: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(src, filename=str(COORDINATOR_PY))):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def test_coordinator_is_orchestrator_not_domain_service() -> None:
    """只 import domain + application（runtime/errors）；无 Repository/Adapter/Persistence。"""
    src = COORDINATOR_PY.read_text(encoding="utf-8")
    roots = _roots(src)
    for r in roots:
        ok = (r.startswith("app.domain")
              or r in {"app.application.errors", "app.application.runtime",
                       "collections", "typing", "dataclasses"})
        assert ok, f"Coordinator 越层依赖 {r}"
    for r in roots:
        assert not any(r == f or r.startswith(f + ".") for f in (
            "sqlalchemy", "fastapi", "app.persistence", "app.adapters",
            "app.repositories", "app.api")), f"Coordinator 不应依赖 {r}"


def test_coordinator_only_calls_executors() -> None:
    """只调六段 executor.execute：无 repo/ORM 调用、无 executor 自行构造。"""
    src = COORDINATOR_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    call_attrs = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert call_attrs == {"execute"}, f"Coordinator 只应调 execute：{call_attrs}"
    constructor_calls = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert constructor_calls <= {"IdentityResolutionFailedError",
                                 "SessionResolutionFailedError",
                                 "OutboundResponse",
                                 # 10.8.3 翻转：+ 薄结果契约构造（执行结果）
                                 "RuntimeExecutionResult"}, constructor_calls
    # 类面 = 恰好 {RuntimeCoordinator}
    classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert classes == {"RuntimeCoordinator"}, classes


def test_no_none_control_flow() -> None:
    """None 不作控制流：唯一的 `is None` 守卫都紧跟 raise（Fail Closed）。"""
    src = COORDINATOR_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            for child in ast.walk(node.test):
                if isinstance(child, ast.Compare) \
                        and any(isinstance(op, ast.Is) and isinstance(c, ast.Constant)
                                and c.value is None for op, c in
                                zip(child.ops, child.comparators)):
                    body_raises = any(
                        isinstance(s, ast.Raise) for s in node.body)
                    assert body_raises, f"`is None` 守卫必须紧跟 raise：{ast.unparse(node)}"
