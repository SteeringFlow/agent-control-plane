"""Step 8.5 —— Agent Dispatch Contract & Boundary 冻结（不实现 AgentDispatcher/Adapter）。

8.5 不再做任何“决策”（Identity/Session/Connection/Agent 选择已完成），只冻结：

    RoutingResult(connection_id, agent_id)
              ↓
    AgentRequest  →  AgentAdapter（未来）→  Agent  →  AgentResponse

核心原则：**Dispatch is execution, not selection.**

锁定项：
A  AgentRequest：frozen dataclass，字段面冻结（含 agent_id / control_plane_session_id /
   external_session_id / content / metadata）；不携带任何 Runtime Context /
   Agent 侧状态字段（结构化锁死，不是穷举 JSON key）；
B  AgentResponse：frozen dataclass，字段面冻结（content / external_session_id /
   metadata）；external_session_id 只是 Agent-side reference，不引入 Session 更新机制；
C  Failure codes：AGENT_DISPATCH_FAILED / AGENT_TIMEOUT / AGENT_RESPONSE_INVALID
   互相独立；
D  Dispatch ≠ Selection：无 AgentDispatcher / Dispatch DTO / Router / Rule /
   Resolver / Policy 类；Agent 侧 ACPAdapter 自 10.1.2 落地（app/adapters/agents/
   acp.py，唯一真实实现）；Channel/WeCom 侧仍空壳；
E  Connection Boundary：AgentRequest 消费 RoutingResult 的 agent_id（与 Connection
   一致），不携带 connection_id / channel_id——Dispatch 不重新 Routing；
F  Agent ownership：Control Plane 不拥有 Agent Conversation / Context / Memory；
   external_session_id 只是 Agent-side reference；
G  Fail Closed：不 Dispatch 的三种情形由 dispatch_failure 失败码表达（不猜）；
H  mutation 探测：注入 AgentDispatcher 偷渡必须报红。
"""

import ast
import dataclasses
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]
REQUEST_PY = APP_DIR / "domain" / "agent_request.py"
RESPONSE_PY = APP_DIR / "domain" / "agent_response.py"
FAILURE_PY = APP_DIR / "domain" / "dispatch_failure.py"

# AgentRequest = Control Plane → Agent 的调用请求（不是 Runtime Context / Agent 内存）
REQUEST_FIELDS = {"agent_id", "control_plane_session_id",
                  "external_session_id", "content", "metadata"}
# AgentResponse 字段面：content / external_session_id / metadata
# + ACP V0.2 加性 artifacts（Agent 工作成果；默认 None ⇒ 旧 Agent 完全兼容）。
RESPONSE_FIELDS = {"content", "external_session_id", "metadata", "artifacts"}
# 结构化禁入字段（不许把边界塞进 metadata 偷渡）
FORBIDDEN_FIELDS = {
    "user_id", "identity_id", "channel_id", "connection_id", "routing_rule",
    "capability", "permission", "tool", "memory", "conversation_history",
    "session_state", "routing", "authorization", "policy",
}
# 8.5 明确不新增的 Dispatch DTO / 组件
DISPATCH_COMPONENT_TOKENS = (
    "dispatch", "dispatcher", "dispatchrequest", "dispatchresult",
    "dispatchcontext", "dispatchpolicy", "routingrule", "routingpolicy",
    "routingtable", "router", "resolver", "capability", "policy", "permission",
)


# ------------------------------------------------------------------ AST helpers


def _tree(py: Path) -> ast.AST:
    return ast.parse(py.read_text(encoding="utf-8"), filename=str(py))


def _roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def _classes(tree: ast.AST) -> dict[str, ast.ClassDef]:
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}


def _annassigns(cls: ast.ClassDef) -> list[ast.AnnAssign]:
    return [s for s in cls.body
            if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)]


def _ann_text(stmt: ast.AnnAssign) -> str:
    def _render(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Constant) and node.value is None:
            return "None"
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            return f"{node.value.id}[{_render(node.slice)}]"
        return "?"

    a = stmt.annotation
    if isinstance(a, ast.BinOp) and isinstance(a.op, ast.BitOr):
        return f"{_render(a.left)} | {_render(a.right)}"
    return _render(a)


def _is_frozen_dataclass(cls: ast.ClassDef) -> bool:
    return any(
        isinstance(d, ast.Call)
        and isinstance(d.func, ast.Name) and d.func.id == "dataclass"
        and any(kw.arg == "frozen" and getattr(kw.value, "value", False)
                for kw in d.keywords)
        for d in cls.decorator_list
    )


def _module_class(py: Path, name: str) -> ast.ClassDef:
    assert py.is_file(), f"缺 {py.relative_to(BACKEND_DIR)}"
    return _classes(_tree(py))[name]


# ================================================================== A AgentRequest


def test_a_request_module_pure() -> None:
    assert _roots(_tree(REQUEST_PY)) == {"dataclasses"}, \
        f"AgentRequest 模块越层导入：{_roots(_tree(REQUEST_PY))}"


def test_a_request_field_face_frozen() -> None:
    """AgentRequest 字段面 = 恰好 5 个（结构化锁死，不带任何 Context/状态字段）。"""
    cls = _module_class(REQUEST_PY, "AgentRequest")
    assert _is_frozen_dataclass(cls), "AgentRequest 应为 frozen dataclass"
    anns = _annassigns(cls)
    names = {s.target.id for s in anns}
    assert names == REQUEST_FIELDS, f"AgentRequest 字段面漂移：{names}"
    assert not (names & FORBIDDEN_FIELDS), \
        f"AgentRequest 混入 Runtime Context / Agent 状态字段：{names & FORBIDDEN_FIELDS}"

    types = {s.target.id: _ann_text(s) for s in anns}
    assert types["agent_id"] == "int"
    assert types["control_plane_session_id"] == "int"
    assert types["content"] == "str"
    assert types["external_session_id"] == "str | None"
    assert types["metadata"] == "dict | None"

    # 必填：agent_id / control_plane_session_id / content；optional：external_session_id / metadata
    for field, required in (("agent_id", True), ("control_plane_session_id", True),
                            ("content", True), ("external_session_id", False),
                            ("metadata", False)):
        stmt = next(s for s in anns if s.target.id == field)
        assert (stmt.value is None) == required, f"{field} 必填性错误"


def test_a_request_is_call_request_not_context() -> None:
    """AgentRequest = 调用请求：只含 agent_id + 归属 + 内容；不得偷渡 channel/connection 等。"""
    src = REQUEST_PY.read_text(encoding="utf-8")
    assert "Dispatch is execution, not selection." in src
    for marker in ("user_id", "identity_id", "channel_id", "connection_id"):
        assert marker not in src.split("@dataclass")[1], f"不应携带 {marker}"


def test_a_request_behavior() -> None:
    """运行时行为：可构造（最小 = agent_id + session + content）；frozen。"""
    from app.domain.agent_request import AgentRequest

    req = AgentRequest(agent_id=3, control_plane_session_id=42, content="hello")
    assert req.agent_id == 3
    assert req.control_plane_session_id == 42
    assert req.content == "hello"
    assert req.external_session_id is None
    assert req.metadata is None

    req2 = dataclasses.replace(
        req, external_session_id="agent-session-abc", metadata={"trace": "1"}
    )
    assert req2.external_session_id == "agent-session-abc"
    assert req.external_session_id is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.content = "x"  # type: ignore[misc]


# ================================================================== B AgentResponse


def test_b_response_module_pure() -> None:
    # ACP V0.2：AgentResponse 引用**同层 domain** 的 Artifact（非越层导入）。
    assert _roots(_tree(RESPONSE_PY)) == {"dataclasses", "app.domain.artifact"}, \
        f"AgentResponse 模块越层导入：{_roots(_tree(RESPONSE_PY))}"


def test_b_response_field_face_frozen() -> None:
    """AgentResponse 字段面 = {content, external_session_id, metadata, artifacts}；
    V0.2 加性 artifacts 默认 None（旧用法不变），不引入 Session 更新机制。"""
    cls = _module_class(RESPONSE_PY, "AgentResponse")
    assert _is_frozen_dataclass(cls), "AgentResponse 应为 frozen dataclass"
    anns = _annassigns(cls)
    names = {s.target.id for s in anns}
    assert names == RESPONSE_FIELDS, f"AgentResponse 字段面漂移：{names}"
    assert not (names & FORBIDDEN_FIELDS), f"AgentResponse 混入状态字段：{names & FORBIDDEN_FIELDS}"

    types = {s.target.id: _ann_text(s) for s in anns}
    assert types["content"] == "str"
    assert types["external_session_id"] == "str | None"
    assert types["metadata"] == "dict | None"
    assert types["artifacts"] == "list[Artifact] | None"
    content_stmt = next(s for s in anns if s.target.id == "content")
    assert content_stmt.value is None, "content 应必填"
    artifacts_stmt = next(s for s in anns if s.target.id == "artifacts")
    assert artifacts_stmt.value is not None, "artifacts 应可选（默认 None）"


def test_b_response_behavior() -> None:
    from app.domain.agent_response import AgentResponse

    resp = AgentResponse(content="hi", external_session_id="abc123")
    assert resp.content == "hi"
    assert resp.external_session_id == "abc123"
    with pytest.raises(dataclasses.FrozenInstanceError):
        resp.content = "no"  # type: ignore[misc]


# ================================================================== C Failure codes


def test_c_failure_codes_frozen_and_independent() -> None:
    """三个 Dispatch 失败码互相独立（语义不可合并）。"""
    from app.domain.dispatch_failure import (
        AGENT_DISPATCH_FAILED,
        AGENT_RESPONSE_INVALID,
        AGENT_TIMEOUT,
    )

    assert AGENT_DISPATCH_FAILED == "AGENT_DISPATCH_FAILED"
    assert AGENT_TIMEOUT == "AGENT_TIMEOUT"
    assert AGENT_RESPONSE_INVALID == "AGENT_RESPONSE_INVALID"
    assert len({AGENT_DISPATCH_FAILED, AGENT_TIMEOUT, AGENT_RESPONSE_INVALID}) == 3
    src = FAILURE_PY.read_text(encoding="utf-8")
    for code in ("AGENT_DISPATCH_FAILED", "AGENT_TIMEOUT", "AGENT_RESPONSE_INVALID"):
        assert code in src


# ================================================================== D Dispatch ≠ Selection


# 9.5 Agent Dispatch Execution（是 Execution，不是 Dispatcher）——精确白名单：
# AgentDispatchExecutor（执行组件）+ AgentDispatchFailedError（Fail-Closed 错误）。
# 其余任何 dispatch 命名类仍报红。
DISPATCH_EXECUTION_ALLOWED = {
    ("app/application/agent_dispatch.py", "AgentDispatchExecutor"),
    ("app/application/errors.py", "AgentDispatchFailedError"),
    # Cloud-0.1.1-B：TenantResolver = 入口租户解析端口（Protocol，非 Dispatch 组件）
    ("app/core/tenant.py", "TenantResolver"),
}


def test_d_no_dispatch_component_classes() -> None:
    """全仓（models 除外）无 AgentDispatcher / Dispatch DTO / Router / Rule / Resolver / Policy。"""
    problems: list[str] = []
    for py in sorted(APP_DIR.rglob("*.py")):
        if (APP_DIR / "persistence" / "models") in py.parents:
            continue
        rel = "/".join(py.relative_to(BACKEND_DIR).parts)
        tree = _tree(py)
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if (rel, node.name) in DISPATCH_EXECUTION_ALLOWED:
                    continue
                lower = node.name.lower()
                if any(tok in lower for tok in DISPATCH_COMPONENT_TOKENS):
                    problems.append(f"{py.relative_to(BACKEND_DIR)}::{node.name}")
    assert not problems, "Dispatch/DTO/Router/Policy 组件提前出现：\n" + "\n".join(problems)


def test_d_no_extra_dispatch_dto() -> None:
    """除 AgentRequest/AgentResponse 外，不再造 DispatchResult/Context/Policy 等 DTO。"""
    classes: set[str] = set()
    for py in (REQUEST_PY, RESPONSE_PY):
        classes |= set(_classes(_tree(py)))
    assert classes == {"AgentRequest", "AgentResponse"}, \
        f"dispatch 相关类面漂移：{classes}"


def test_d_adapters_remain_shells() -> None:
    """Channel/WeCom Adapter 与 runtime API 仍空壳；adapters/agents 自 10.1.2 起
    含唯一真实实现 acp.py（ACPAdapter，Port 的实现）。"""
    # 15.2.3：agents/channels 各含真实 Adapter + registry.py；api/runtime 含 channels.py
    for sub in ("adapters/channels", "adapters/agents", "api/runtime"):
        files = sorted(p.name for p in (APP_DIR / sub).glob("*.py"))
        expected = ["__init__.py", "acp.py", "local.py", "registry.py", "workbuddy.py"] \
            if sub == "adapters/agents" \
            else ["__init__.py", "local.py", "registry.py", "wecom.py",
                  "wecom_bot.py"] \
            if sub == "adapters/channels" \
            else ["__init__.py", "channels.py"] if sub == "api/runtime" \
            else ["__init__.py"]
        assert files == expected, f"{sub} 实现面漂移：{files}"


# ================================================================== E Connection Boundary


def test_e_request_consumes_routed_agent() -> None:
    """Dispatch 消费 Routing 的 agent_id（与 Connection 一致），不携带 connection_id。"""
    from app.domain.agent_request import AgentRequest
    from app.domain.connection import Connection
    from app.domain.routing_result import RoutingResult

    # Routing 输出同时给出 connection_id + agent_id（8.4 契约）
    assert {f.name for f in dataclasses.fields(RoutingResult)} == {
        "connection_id", "agent_id"}
    # Connection 是 agent_id 的唯一来源（Access Path = Channel ↔ Agent）
    assert "agent_id" in {f.name for f in dataclasses.fields(Connection)}
    # AgentRequest 只带 agent_id（已确定），不带 connection_id / channel_id
    request_fields = {f.name for f in dataclasses.fields(AgentRequest)}
    assert "agent_id" in request_fields
    assert "connection_id" not in request_fields
    assert "channel_id" not in request_fields


# ================================================================== F Agent ownership


def test_f_control_plane_does_not_own_agent_state() -> None:
    """Control Plane 不拥有 Agent Conversation/Context/Memory：请求/响应均无此类字段。"""
    from app.domain.agent_request import AgentRequest
    from app.domain.agent_response import AgentResponse

    for cls in (AgentRequest, AgentResponse):
        fields = {f.name for f in dataclasses.fields(cls)}
        assert not (fields & {"memory", "conversation_history", "context",
                              "tool", "tool_permissions"}), f"{cls.__name__} 携带 Agent 状态"


def test_f_session_id_semantics_separate() -> None:
    """control_plane_session_id ≠ external_session_id：前者指 CP Session，后者只是 Agent 侧 ref。"""
    from app.domain.agent_request import AgentRequest

    field_names = {f.name for f in dataclasses.fields(AgentRequest)}
    assert "control_plane_session_id" in field_names
    assert "external_session_id" in field_names
    assert field_names == REQUEST_FIELDS
    # 语义在 docstring 冻结：control_plane_session_id ≠ Agent internal session
    src = REQUEST_PY.read_text(encoding="utf-8")
    assert "不等于" in src


# ================================================================== G metadata 边界 / Fail Closed


def test_g_metadata_is_opaque_dict_only() -> None:
    """metadata 是普通 dict|None，不做专用 Context 结构（结构化限制而非穷举 JSON key）。"""
    for py in (REQUEST_PY, RESPONSE_PY):
        cls = next(iter(_classes(_tree(py)).values()))
        for stmt in _annassigns(cls):
            if stmt.target.id == "metadata":
                assert _ann_text(stmt) == "dict | None"


def test_g_fail_closed_codes_cover_no_dispatch() -> None:
    """Fail Closed：三种不 Dispatch 情形都有独立失败码表达（不猜）。"""
    src = FAILURE_PY.read_text(encoding="utf-8")
    for phrase in ("不 Dispatch", "Fail Closed", "猜一个 content"):
        assert phrase in src


# ================================================================== H mutation 探测


def test_h_smuggled_agent_dispatcher_is_caught() -> None:
    """变异探测：注入 AgentDispatcher 偷渡必须报红。"""
    smuggled = APP_DIR / "domain" / "agent_dispatcher.py"
    assert not smuggled.exists(), "agent_dispatcher.py 不应已存在"
    try:
        smuggled.write_text(
            "class AgentDispatcher:\n"
            "    def dispatch(self) -> None:\n"
            "        pass\n",
            encoding="utf-8",
        )
        # 1) 7.3 反向审计必须报红（类名 token "dispatcher"）
        audit_py = BACKEND_DIR / "tests" / "test_step7_3_connection_management_audit.py"
        ns: dict = {"__file__": str(audit_py), "__name__": "audit_under_8_5"}
        exec(compile(audit_py.read_text(encoding="utf-8"), str(audit_py), "exec"), ns)
        problems = ns["_next_stage_problems"]()
        assert any("AgentDispatcher" in p for p in problems), \
            f"反向审计未抓到 AgentDispatcher 偷渡：{problems}"

        # 2) 本文件 D 组组件守卫必须报红
        found = [
            "/".join(smuggled.relative_to(BACKEND_DIR).parts)
            for node in ast.walk(_tree(smuggled))
            if isinstance(node, (ast.ClassDef, ast.FunctionDef))
            and any(tok in node.name.lower() for tok in DISPATCH_COMPONENT_TOKENS)
        ]
        assert found, "Dispatch 组件守卫未抓到偷渡"
    finally:
        if smuggled.exists():
            smuggled.unlink()
