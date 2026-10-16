"""Step 9.5 —— Agent Dispatch Execution（8.5 契约第一次穿过 Control Plane → Agent）。

    RoutingResult(connection_id, agent_id)
        ↓ ConnectionRepository.get_by_id(routing_result.connection_id)  （既有方法）
        ↓ Connection（读取 Routing 已选定的 Access Path，不是重新选路）
        ↓ AgentRequest（agent_id 来自 RoutingResult.agent_id）
        ↓ AgentAdapter.send(request, connection)
        ↓ AgentResponse

**Dispatch is execution, not selection.** 本阶段不接真实 Agent：用 Test Agent
Adapter 注入，证明链路完整可执行。不创建 AgentDispatcher / AgentRuntime /
AgentPolicy；不 import SQLAlchemy / ORM；不新造底层错误（transport 失败统一映射
AGENT_DISPATCH_FAILED，响应无效映射 AGENT_RESPONSE_INVALID）。

锁定项：
A  Agent ID 来源 = RoutingResult.agent_id（Executor 不自己找 Agent）；
B  Connection 来源 = routing_result.connection_id（get_by_id，不是再次选路）；
C  一致性 Fail Closed：Connection.agent_id ≠ RoutingResult.agent_id → 失败
   （不允许 Connection=10 / Agent=9）；Connection 读不到 → 失败；
D  control_plane_session_id（CP Session）≠ external_session_id（Agent 侧 ref）；
E  AgentResponse 无效 → AgentResponseInvalidError；transport 失败 →
   AgentDispatchFailedError（不泄漏底层异常类型）；
F  只依赖 Repository/UoW Port + AgentAdapter Port；无 ORM/SQLAlchemy；
G  mutation guard（AgentDispatcher 偷渡必须报红）。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.application import (
    AgentDispatchExecutor,
    AgentDispatchFailedError,
    AgentResponseInvalidError,
)
from app.domain.agent import Agent
from app.domain.agent_request import AgentRequest
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.routing_result import RoutingResult
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]
DISPATCH_PY = APP_DIR / "application" / "agent_dispatch.py"

FORBIDDEN_ROOTS = (
    "sqlalchemy", "fastapi", "starlette", "pydantic",
    "app.persistence", "app.adapters", "app.api", "app.schemas",
)


def _root_allowed(root: str) -> bool:
    return (root in {"collections", "dataclasses", "typing"}
            or root.startswith("collections.")
            or root.startswith("typing.")
            or root == "app.domain" or root.startswith("app.domain.")
            or root == "app.repositories" or root.startswith("app.repositories.")
            or root == "app.application.errors")


# --------------------------------------------------------------- Test Agent Adapter


class _TestAgentAdapter:
    """满足 AgentAdapter Port 的测试 Adapter：记录调用并返回固定响应。"""

    def __init__(self) -> None:
        self.calls: list[tuple[AgentRequest, Connection]] = []

    def send(self, request: AgentRequest, connection: Connection) -> AgentResponse:
        self.calls.append((request, connection))
        return AgentResponse(
            content="test response",
            external_session_id="agent-session-abc",
            metadata={"echo": request.content},
        )


class FailingAgentAdapter:
    """模拟 Agent transport 失败（真实 Adapter 失败 → Dispatch failure）。"""

    def send(self, request: AgentRequest, connection: Connection) -> AgentResponse:
        raise RuntimeError("transport down")


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _executor(db_engine, adapter) -> AgentDispatchExecutor:
    return AgentDispatchExecutor(
        agent_adapter=adapter,
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)),
    )


def _graph(db_engine) -> dict[str, int]:
    """1 Channel + 2 Agents + 1 条 active Connection（另一 Agent 供一致性测试）。"""
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        a1 = uow.agents.create(Agent(name="agent-1", type="acp"))
        a2 = uow.agents.create(Agent(name="agent-2", type="acp"))
        conn = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=a1.id, name="path-1"))
        conn2 = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=a2.id, name="path-2"))
        uow.commit()
        return {"channel": channel.id, "agent1": a1.id, "agent2": a2.id,
                "connection1": conn.id, "connection2": conn2.id}


# ------------------------------------------------------------------ AST helpers


def _roots(src: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(src, filename=str(DISPATCH_PY))):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def _classes(src: str) -> set[str]:
    return {n.name for n in ast.walk(ast.parse(src, filename=str(DISPATCH_PY)))
            if isinstance(n, ast.ClassDef)}


def _boundary_problems(src: str) -> list[str]:
    problems: list[str] = []
    for root in sorted(_roots(src)):
        if not _root_allowed(root):
            problems.append(f"越层 import {root}")
        if root.startswith("app.repositories.identity") \
                or root.startswith("app.repositories.session"):
            problems.append(f"不应 import 解析/会话组件：{root}")
    tree = ast.parse(src, filename=str(DISPATCH_PY))
    forbidden_names = {"SqlAlchemyUnitOfWork", "Session", "sessionmaker",
                       "create_engine", "Engine", "ConnectionModel", "AgentModel",
                       "HTTPException", "APIRouter", "AcpAdapter", "ACPAdapter",
                       "IdentityRepository", "SessionRepository"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            problems.append(f"出现基础设施标识符 {node.id}")
    write_calls = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"create", "update", "delete"}
    }
    if write_calls:
        problems.append(f"Execution 不应写：{write_calls}")
    reselect_calls = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"find_active_by_channel", "find_by_channel_and_agent"}
    }
    if reselect_calls:
        problems.append(f"Dispatch 不应再次选路：{reselect_calls}")
    extra = _classes(src) - {"AgentDispatchExecutor"}
    if extra:
        problems.append(f"出现非 Execution 组件类：{sorted(extra)}")
    return problems


# ================================================================== A/B 成功路径


def test_a_dispatch_returns_agent_response(db_engine) -> None:
    g = _graph(db_engine)
    adapter = _TestAgentAdapter()
    result = _executor(db_engine, adapter).execute(
        RoutingResult(connection_id=g["connection1"], agent_id=g["agent1"]),
        control_plane_session_id=42,
        content="hello agent",
        external_session_id="agent-session-prev",
    )
    assert isinstance(result, AgentResponse)
    assert result.content == "test response"
    assert result.external_session_id == "agent-session-abc"


def test_a_agent_id_comes_from_routing_result(db_engine) -> None:
    g = _graph(db_engine)
    adapter = _TestAgentAdapter()
    _executor(db_engine, adapter).execute(
        RoutingResult(connection_id=g["connection1"], agent_id=g["agent1"]),
        control_plane_session_id=42,
        content="hello",
    )
    request, connection = adapter.calls[-1]
    assert request.agent_id == g["agent1"]  # 来自 RoutingResult，不是自己找
    assert request.content == "hello"
    assert request.control_plane_session_id == 42
    assert request.external_session_id is None


def test_a_connection_read_via_routing_connection_id(db_engine) -> None:
    g = _graph(db_engine)
    adapter = _TestAgentAdapter()
    _executor(db_engine, adapter).execute(
        RoutingResult(connection_id=g["connection2"], agent_id=g["agent2"]),
        control_plane_session_id=7,
        content="hi",
    )
    request, connection = adapter.calls[-1]
    # Connection 来自 routing_result.connection_id（get_by_id 读取 Access Path）
    assert connection.id == g["connection2"]
    assert connection.agent_id == g["agent2"]
    assert request.agent_id == g["agent2"]
    assert connection.agent_id == request.agent_id


def test_a_request_and_connection_are_passed_to_adapter(db_engine) -> None:
    g = _graph(db_engine)
    adapter = _TestAgentAdapter()
    _executor(db_engine, adapter).execute(
        RoutingResult(connection_id=g["connection1"], agent_id=g["agent1"]),
        control_plane_session_id=1,
        content="hi",
        external_session_id="agent-sid-1",
        metadata={"trace": "t1"},
    )
    request, connection = adapter.calls[-1]
    assert request.external_session_id == "agent-sid-1"
    assert request.metadata == {"trace": "t1"}
    assert connection.id == g["connection1"]


# ================================================================== C 一致性 Fail Closed


def test_c_agent_id_mismatch_with_connection_fails(db_engine) -> None:
    """不允许 Connection=10 / Agent=9：Connection 与 RoutingResult 必须一致。"""
    g = _graph(db_engine)
    with pytest.raises(AgentDispatchFailedError):
        _executor(db_engine, _TestAgentAdapter()).execute(
            RoutingResult(connection_id=g["connection1"], agent_id=g["agent2"]),
            control_plane_session_id=1,
            content="hi",
        )


def test_c_missing_connection_fails(db_engine) -> None:
    with pytest.raises(AgentDispatchFailedError):
        _executor(db_engine, _TestAgentAdapter()).execute(
            RoutingResult(connection_id=999999, agent_id=1),
            control_plane_session_id=1,
            content="hi",
        )


# ================================================================== D Session 语义


def test_d_cp_session_and_external_session_not_confused(db_engine) -> None:
    g = _graph(db_engine)
    adapter = _TestAgentAdapter()
    result = _executor(db_engine, adapter).execute(
        RoutingResult(connection_id=g["connection1"], agent_id=g["agent1"]),
        control_plane_session_id=42,
        content="hi",
        external_session_id="agent-old",
    )
    request, _ = adapter.calls[-1]
    assert request.control_plane_session_id == 42      # CP Session（int）
    assert request.external_session_id == "agent-old"  # Agent 侧引用（str）
    assert result.external_session_id == "agent-session-abc"
    # 语义上绝不把 Agent 侧 id 当作 CP Session
    assert request.control_plane_session_id != "agent-session-abc"
    assert not isinstance(request.control_plane_session_id, str)


# ================================================================== E 失败映射


def test_e_invalid_response_is_rejected(db_engine) -> None:
    g = _graph(db_engine)

    class _NonResponseAdapter:
        def send(self, request, connection):
            return "not an AgentResponse"

    class _EmptyContentAdapter:
        def send(self, request, connection):
            return AgentResponse(content="   ")

    class _BadTypeAdapter:
        def send(self, request, connection):
            return AgentResponse(content=123)  # type: ignore[arg-type]

    for adapter in (_NonResponseAdapter(), _EmptyContentAdapter(), _BadTypeAdapter()):
        with pytest.raises(AgentResponseInvalidError):
            _executor(db_engine, adapter).execute(
                RoutingResult(connection_id=g["connection1"], agent_id=g["agent1"]),
                control_plane_session_id=1,
                content="hi",
            )


def test_e_transport_failure_maps_to_dispatch_failed(db_engine) -> None:
    g = _graph(db_engine)
    with pytest.raises(AgentDispatchFailedError):
        _executor(db_engine, FailingAgentAdapter()).execute(
            RoutingResult(connection_id=g["connection1"], agent_id=g["agent1"]),
            control_plane_session_id=1,
            content="hi",
        )


# ================================================================== F 依赖边界


def test_f_module_imports_only_ports_and_domain() -> None:
    src = DISPATCH_PY.read_text(encoding="utf-8")
    assert not _boundary_problems(src), _boundary_problems(src)
    roots = _roots(src)
    assert "app.repositories.agent_adapter" in roots, "应依赖 AgentAdapter Port"
    assert "app.repositories.unit_of_work" in roots, "应依赖 UoW Port"
    for r in roots:
        assert not any(r == f or r.startswith(f + ".") for f in FORBIDDEN_ROOTS), \
            f"越层 import {r}"


def test_f_executor_does_not_reselect() -> None:
    """Dispatch 只 get_by_id 读取已选定的 Connection，绝不再次选路。"""
    src = DISPATCH_PY.read_text(encoding="utf-8")
    read_calls = {
        node.func.attr for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
    }
    # strip 是输入校验（content.strip），不是选路/写
    assert read_calls <= {"get_by_id", "send", "strip"}, read_calls


def test_f_executor_is_not_dispatcher_runtime_policy() -> None:
    """模块只有一个 Execution 类：无 AgentDispatcher / AgentRuntime / AgentPolicy。"""
    src = DISPATCH_PY.read_text(encoding="utf-8")
    assert _classes(src) == {"AgentDispatchExecutor"}, _classes(src)
    for banned in ("AgentDispatcher", "AgentRuntime", "AgentPolicy",
                   "AgentPool", "AgentHealthCheck"):
        assert f"class {banned}" not in src, f"不应出现 {banned}"


# ================================================================== G mutation 探测


@pytest.mark.parametrize("inject", [
    "import sqlalchemy",
    "from app.persistence.models.connection import ConnectionModel",
    "from app.adapters.agents.acp_adapter import AcpAdapter",
])
def test_g_forbidden_import_is_caught(inject: str) -> None:
    src = DISPATCH_PY.read_text(encoding="utf-8") + "\n" + inject + "\n"
    assert _boundary_problems(src), f"边界守卫未抓到 {inject}"


def test_g_write_call_is_caught() -> None:
    src = DISPATCH_PY.read_text(encoding="utf-8").replace(
        "connection = uow.connections.get_by_id(routing_result.connection_id)",
        "connection = uow.connections.find_active_by_channel(1)",
    )
    assert any("再次选路" in p for p in _boundary_problems(src)), "选路调用未被守卫抓住"


def test_g_dispatcher_injection_caught_by_reverse_audit() -> None:
    """真实文件注入 AgentDispatcher → 7.3 反向审计必须报红（Execution ≠ Dispatcher）。"""
    backup = DISPATCH_PY.read_text(encoding="utf-8")
    try:
        DISPATCH_PY.write_text(
            backup + "\n\nclass AgentDispatcher:\n    pass\n", encoding="utf-8")
        audit_py = BACKEND_DIR / "tests" / "test_step7_3_connection_management_audit.py"
        ns: dict = {"__file__": str(audit_py), "__name__": "audit_under_9_5"}
        exec(compile(audit_py.read_text(encoding="utf-8"), str(audit_py), "exec"), ns)
        problems = ns["_next_stage_problems"]()
        assert any("AgentDispatcher" in p for p in problems), \
            f"反向审计未抓到 AgentDispatcher 偷渡：{problems}"
        assert _boundary_problems(DISPATCH_PY.read_text(encoding="utf-8")), \
            "模块类面守卫未抓到偷渡"
    finally:
        DISPATCH_PY.write_text(backup, encoding="utf-8")
