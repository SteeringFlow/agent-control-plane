"""Step 10.1.4.4 —— Runtime Vertical Slice Closure Audit（10.1.3 ~ 10.1.4.3 横切封板）。

性质：不新增生产能力、不改 Runtime 架构，只做 **Closure Audit + Baseline Freeze**。

与 9.A 的分工（避免重复）：
- 9.A 已封：执行顺序常量唯一、stage I/O 契约面、Coordinator import 面、
  Executor 越层 import、Adapter 是 Port、failure typed per stage、无未来基础设施；
- **本审计新增 10.1.3/10.1.4 才出现的事实**：
    A. Coordinator 无隐藏分支 + 不吞异常（错误按 stage 语义原样传播）；
    B. **真实执行顺序**（spy 跟踪六段），并证明 `session_creation` 是条件阶段；
    C. Session 隔离键在 Port 签名与 Coordinator 实参两处一致；
    D. Dispatch 边界：Router 不与 Agent 通信；Runtime Core 无任何传输/协议实现，
       协议只属于 `app/adapters/agents/acp.py`；
    E. Delivery 边界：OutboundResponse 构造面最小；Delivery 只依赖 ChannelAdapter；
       processing success ≠ delivery success；
    F. 六个 failure stage → 六个 typed error，且每 stage 只用「自己的」错误。

行为证据（REUSE_ACTIVE / Session Continuity）由 10.1.4.3 提供；真实 Agent 链路由
10.1.4.2 提供——本文件不重复覆盖。
"""

import ast
from pathlib import Path

from sqlalchemy.orm import Session as SaSession, sessionmaker

from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.errors import (
    AccessAuthorizationDeniedError,
    AgentDispatchFailedError,
    AgentResponseInvalidError,
    ApplicationError,
    ChannelDeliveryFailedError,
    IdentityResolutionFailedError,
    RoutingFailedError,
    SessionResolutionFailedError,
)
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.routing import RoutingExecutor
from app.application.runtime import RUNTIME_EXECUTION_ORDER
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.agent import Agent
from app.domain.agent_request import AgentRequest
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"
APPLICATION_DIR = APP_DIR / "application"
DOMAIN_DIR = APP_DIR / "domain"

RUNTIME_EXECUTORS = {
    "identity_resolution": "app/application/identity_resolution.py",
    "authorization": "app/application/access_authorization.py",
    "routing": "app/application/routing.py",
    "session_resolution": "app/application/session_resolution.py",
    "session_creation": "app/application/session_creation.py",
    "agent_dispatch": "app/application/agent_dispatch.py",
    "delivery": "app/application/delivery.py",
}

# 七个 failure stage → 唯一 typed error（AGENT_EXECUTION == AGENT_RESPONSE_INVALID）
FAILURE_STAGES = {
    "IDENTITY": IdentityResolutionFailedError,
    "AUTHORIZATION": AccessAuthorizationDeniedError,
    "ROUTING": RoutingFailedError,
    "SESSION": SessionResolutionFailedError,
    "AGENT_DISPATCH": AgentDispatchFailedError,
    "AGENT_EXECUTION": AgentResponseInvalidError,
    "CHANNEL_DELIVERY": ChannelDeliveryFailedError,
}

# 每 stage 只允许 raise 自己的 typed error（不借用其他 stage 的错误）
STAGE_OWN_ERRORS = {
    "identity_resolution": {"IdentityResolutionFailedError"},
    "authorization": {"AccessAuthorizationDeniedError"},
    "routing": {"RoutingFailedError"},
    "session_resolution": {"SessionResolutionFailedError"},
    "session_creation": {"SessionResolutionFailedError"},
    "agent_dispatch": {"AgentDispatchFailedError", "AgentResponseInvalidError"},
    "delivery": {"ChannelDeliveryFailedError"},
}

# 传输 / 协议实现：只允许出现在 adapters 层
TRANSPORT_ROOTS = {"http", "urllib", "socket", "ssl", "httpx", "requests",
                   "aiohttp", "websockets", "websocket", "grpc"}


# ------------------------------------------------------------------ helpers


def _src(rel: str) -> str:
    return (BACKEND_DIR / rel).read_text(encoding="utf-8")


def _roots(src: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def _coordinator_call_args(attr: str) -> list[str]:
    """取 coordinator 中 `self.<attr>.execute(...)` 的实参源码。"""
    tree = ast.parse(_src("app/application/coordinator.py"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == attr):
            return [ast.unparse(a) for a in node.args]
    raise AssertionError(f"未找到 self.{attr}.execute 调用")


# ------------------------------------------------------------------ 测试底座（行为跟踪）


# Coordinator 注入名 → 执行顺序 stage 名（只有 authorization 的前缀不同：
# 注入名 access_authorization / stage 名 authorization）
_STAGE_NAME = {"access_authorization": "authorization"}


class _Spy:
    """记录调用顺序并原样委托真实 Executor（不改变任何语义）。"""

    def __init__(self, name: str, inner, trace: list[str]) -> None:
        self._name = name
        self._inner = inner
        self._trace = trace

    def execute(self, *args, **kwargs):
        self._trace.append(self._name)
        return self._inner.execute(*args, **kwargs)


class _TestAgentAdapter:
    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []

    def send(self, request: AgentRequest, connection: Connection) -> AgentResponse:
        self.requests.append(request)
        return AgentResponse(content="ack", external_session_id="agent-s-1")


class _TestChannelAdapter:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=SaSession, expire_on_commit=False)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))


def _seed(db_engine) -> int:
    """最小数据图：User → Channel → Identity → Agent → 唯一 active Connection。"""
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a"))
        # Implementation Package 2 / Step 2：显式授权该会话（default-deny 前提）
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="conv-1",
            connection_id=path.id))
        uow.commit()
        return channel.id


def _inbound(channel_id: int, conversation: str, interaction_id: str) -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id=interaction_id,
        external_conversation_id=conversation,
        external_identity_id="wx_a", content="hello")


def _coordinator_with_trace(db_engine, trace, agent_adapter, channel_adapter):
    uow = _uow_factory(db_engine)
    inner = {
        "identity_resolution": IdentityResolutionExecutor(uow),
        # 注意：**kwargs 的键是 Coordinator 的参数名（access_authorization），
        # 而 trace 记录的是执行顺序里的 stage 名（authorization）——两者刻意不同名，
        # 由 _STAGE_NAME 映射，避免为了让 spy 好看而改 Coordinator 的注入名。
        "access_authorization": AccessAuthorizationExecutor(uow),
        "routing": RoutingExecutor(uow),
        "session_resolution": SessionResolutionExecutor(uow),
        "session_creation": SessionCreationExecutor(uow),
        "agent_dispatch": AgentDispatchExecutor(agent_adapter=agent_adapter,
                                                uow_factory=uow),
        "delivery": DeliveryExecutor(channel_adapter=channel_adapter),
    }
    return RuntimeCoordinator(
        **{name: _Spy(_STAGE_NAME.get(name, name), ex, trace)
           for name, ex in inner.items()})


# ================================================================== A. Coordinator 边界


def test_coordinator_has_no_repository_orm_adapter_identifiers() -> None:
    """Coordinator 只依赖六段能力：无 Repository / ORM / SQLAlchemy / Adapter 标识符。"""
    tree = ast.parse(_src("app/application/coordinator.py"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    forbidden = ("Repository", "SqlAlchemy", "ORM", "Model", "sessionmaker",
                 "Engine", "create_engine", "ACPAdapter", "ChannelAdapter")
    hit = sorted(n for n in names if any(f in n for f in forbidden))
    assert not hit, f"Coordinator 越层标识符：{hit}"


def test_coordinator_does_not_swallow_stage_errors() -> None:
    """错误按 stage 语义原样向上传播：Coordinator 无 try/except 吞包。"""
    tree = ast.parse(_src("app/application/coordinator.py"))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Try)], \
        "Coordinator 不应捕获/吞掉 stage 错误"


def _compares_decision_mode(node: ast.AST) -> bool:
    return any(isinstance(c, ast.Compare) and ast.unparse(c.left) == "decision.mode"
               for c in ast.walk(node))


def test_coordinator_session_branch_has_no_hidden_branch() -> None:
    """Session 分支只有 REUSE_ACTIVE / CREATE_NEW 两条 + Fail Closed else。

    注：Python 把 `elif` 表示为**嵌套 If**（挂在 orelse[0]），因此
    `if / elif / else` 三个形态在 AST 里是 2 个 If 节点——不是隐藏分支。
    """
    tree = ast.parse(_src("app/application/coordinator.py"))
    mode_ifs = [n for n in ast.walk(tree)
                if isinstance(n, ast.If) and _compares_decision_mode(n.test)]
    assert len(mode_ifs) == 2, f"Session 分支数漂移：{len(mode_ifs)}（期望 if+elif）"

    compared = {ast.unparse(c.comparators[0])
                for n in mode_ifs for c in ast.walk(n.test)
                if isinstance(c, ast.Compare)
                and ast.unparse(c.left) == "decision.mode"}
    assert compared == {"REUSE_ACTIVE", "CREATE_NEW"}, compared

    # 顺着 orelse 链走到最后：未知 mode → Fail Closed（else 必须 raise）
    tail = mode_ifs[0]
    while len(tail.orelse) == 1 and isinstance(tail.orelse[0], ast.If) \
            and _compares_decision_mode(tail.orelse[0].test):
        tail = tail.orelse[0]
    assert isinstance(tail.orelse[-1], ast.Raise), "未知 mode 必须 Fail Closed"


# ================================================================== B. 执行顺序（真实执行）


def test_create_new_path_executes_all_seven_stages_in_order(db_engine) -> None:
    """CREATE_NEW：identity → authorization → routing → session_resolution
    → session_creation → agent_dispatch → delivery（与冻结常量一致）。

    Package 2 / Step 2：第 7 段（authorization）加入后仍按 RUNTIME_EXECUTION_ORDER
    真实执行——顺序不是文档声明，而是被 spy 录到的调用序列。
    """
    channel_id = _seed(db_engine)
    trace: list[str] = []
    coordinator = _coordinator_with_trace(db_engine, trace, _TestAgentAdapter(),
                                          _TestChannelAdapter())
    coordinator.execute(_inbound(channel_id, "conv-1", "msg-1"))
    assert tuple(trace) == tuple(RUNTIME_EXECUTION_ORDER)
    assert "session_creation" in trace


def test_reuse_active_path_skips_session_creation(db_engine) -> None:
    """REUSE_ACTIVE：session_creation 是条件阶段，被真实跳过（不是常量里写写而已）。"""
    channel_id = _seed(db_engine)
    trace: list[str] = []
    coordinator = _coordinator_with_trace(db_engine, trace, _TestAgentAdapter(),
                                          _TestChannelAdapter())
    coordinator.execute(_inbound(channel_id, "conv-1", "msg-1"))
    trace.clear()

    coordinator.execute(_inbound(channel_id, "conv-1", "msg-2"))

    expected = tuple(s for s in RUNTIME_EXECUTION_ORDER if s != "session_creation")
    assert tuple(trace) == expected
    assert "session_creation" not in trace


# ================================================================== C. Session 边界


def test_session_isolation_key_is_three_tuple() -> None:
    """隔离键 = (Identity, Connection, external_conversation_id)，三处一致。"""
    import inspect

    from app.repositories.session import SessionRepository

    params = list(inspect.signature(
        SessionRepository.find_active).parameters)
    assert params == ["self", "identity_id", "connection_id",
                      "external_conversation_id"], params
    # Coordinator 传的实参 = identity_id（不是 user_id）+ connection_id + conversation
    assert _coordinator_call_args("_session_resolution") == [
        "identity_id", "routing_result.connection_id",
        "inbound.external_conversation_id"]


def test_coordinator_never_resolves_session_by_user_id() -> None:
    """Session 边界用 Identity.id（Channel 边界），绝不用 User.id。"""
    src = _src("app/application/coordinator.py")
    assert "user_id" not in src


# ================================================================== D. Dispatch 边界


def test_router_does_not_communicate_with_agent() -> None:
    """Routing 只选 Connection：不 import AgentAdapter、不调用 send。"""
    src = _src("app/application/routing.py")
    roots = _roots(src)
    assert not any(r.startswith("app.repositories.agent_adapter") for r in roots)
    calls = {n.func.attr for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not (calls & {"send", "deliver"}), calls


def test_runtime_core_has_no_transport_or_protocol() -> None:
    """Runtime Core（application + domain）无任何传输 / 协议实现。"""
    files = sorted(APPLICATION_DIR.glob("*.py")) + sorted(DOMAIN_DIR.glob("*.py"))
    for py in files:
        roots = _roots(py.read_text(encoding="utf-8"))
        hit = sorted(r for r in roots if r.split(".")[0] in TRANSPORT_ROOTS)
        assert not hit, f"Runtime Core 出现传输依赖 {py.name}：{hit}"


def test_protocol_implementation_lives_only_in_adapter_layer() -> None:
    """传输 / 协议实现全仓唯一归属 Adapter 层：
    agents/acp.py（Agent 出站，10.1.2）+ channels/wecom.py（渠道 HTTP 双向，
    10.2.2）+ channels/wecom_bot.py（渠道 WebSocket 双向，3B-2.2）。

    白名单随「已实现的传输 Adapter」同步增长——这正是本守卫的语义：新增传输
    必须落在 adapters 层，而不是放开纪律。
    """
    owners = []
    for py in APP_DIR.rglob("*.py"):
        roots = _roots(py.read_text(encoding="utf-8"))
        if any(r.split(".")[0] in TRANSPORT_ROOTS for r in roots):
            owners.append(py.name)
    assert sorted(owners) == ["acp.py", "wecom.py", "wecom_bot.py",
                              "workbuddy.py"], owners


def test_dispatch_calls_only_injected_adapter() -> None:
    """AgentDispatchExecutor 自己不实现协议：只调注入的 agent_adapter.send。"""
    src = _src("app/application/agent_dispatch.py")
    calls = {
        ast.unparse(n.func) for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "send"
    }
    assert calls == {"self._agent_adapter.send"}, calls
    assert not any(r.startswith("app.adapters") for r in _roots(src))


# ================================================================== E. Delivery 边界


def test_outbound_construction_is_minimal() -> None:
    """OutboundResponse 构造面 = 4 个冻结字段，不带任何内部 id。"""
    tree = ast.parse(_src("app/application/coordinator.py"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "OutboundResponse"]
    assert len(calls) == 1
    kwargs = {k.arg for k in calls[0].keywords}
    assert kwargs == {"channel_id", "external_conversation_id", "content",
                      "metadata"}, kwargs


def test_delivery_depends_only_on_channel_adapter() -> None:
    """Delivery 只依赖 ChannelAdapter Port：不读库（无 UoW / Repository）。"""
    assert _roots(_src("app/application/delivery.py")) == {
        "app.application.errors", "app.domain.outbound_response",
        "app.repositories.channel_adapter"}


def test_processing_success_is_not_delivery_success() -> None:
    """processing ≠ delivery：两者是各自独立的 typed error，不被合并成一个。"""
    assert issubclass(AgentDispatchFailedError, ApplicationError)
    assert issubclass(ChannelDeliveryFailedError, ApplicationError)
    assert ChannelDeliveryFailedError is not AgentDispatchFailedError
    # runtime.py 冻结「Delivery failure → processing 仍 SUCCESS、delivery = FAILED」
    runtime_src = _src("app/application/runtime.py")
    assert "processing 仍 SUCCESS、delivery = FAILED" in runtime_src


# ================================================================== F. Failure 边界


def test_seven_failure_stages_map_to_distinct_typed_errors() -> None:
    """七个 failure stage → 七个 typed ApplicationError（无 RuntimeFailed 吞包类）。

    Package 2 / Step 2：+ AUTHORIZATION（`AccessAuthorizationDeniedError`，
    UNAUTHORIZED）。仍不引入吞包类——每个 stage 一个专属 typed error。
    """
    assert len(set(FAILURE_STAGES.values())) == 7
    for stage, error in FAILURE_STAGES.items():
        assert issubclass(error, ApplicationError), stage
    assert set(FAILURE_STAGES) == {"IDENTITY", "AUTHORIZATION", "ROUTING",
                                   "SESSION", "AGENT_DISPATCH", "AGENT_EXECUTION",
                                   "CHANNEL_DELIVERY"}


def test_each_stage_raises_only_its_own_error() -> None:
    """每 stage 只 import 自己的 typed error：失败语义不跨 stage 借用。"""
    for stage, rel in RUNTIME_EXECUTORS.items():
        tree = ast.parse(_src(rel))
        imported = {alias.name for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                    and node.module == "app.application.errors"
                    for alias in node.names}
        assert imported == STAGE_OWN_ERRORS[stage], f"{stage} 错误面漂移：{imported}"
