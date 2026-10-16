"""Step 10.3.1 Layer D —— Multi-Agent Isolation Guard（AST 架构守卫）。

多 Agent 场景下边界必须原样成立（10.3.1 §八）：

    RoutingExecutor       ❌ 不 import AgentAdapter   ❌ 不直接 send()
    RuntimeCoordinator    ❌ 无 agent-specific logic（不按 agent/内容分支）
    AgentDispatchExecutor ❌ 不自己选择 Agent          ❌ 不绕过 RoutingResult
    ACPAdapter            ❌ 不知道 Channel / Identity / SessionRepository

最终保持：Routing → RoutingResult → Dispatch → AgentAdapter（→ Delivery）。
本步生产代码零改动——守卫把"零改动"变成可回归断言。
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"

ROUTING_PY = APP / "application" / "routing.py"
COORDINATOR_PY = APP / "application" / "coordinator.py"
DISPATCH_PY = APP / "application" / "agent_dispatch.py"
ACP_PY = APP / "adapters" / "agents" / "acp.py"


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def _symbols(tree: ast.AST) -> set[str]:
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.Name):
            symbols.add(node.id)
        elif isinstance(node, ast.Attribute):
            symbols.add(node.attr)
    return symbols


# ================================================================== Routing 边界


def test_d1_routing_never_touches_agent_adapter() -> None:
    """RoutingExecutor 不 import AgentAdapter、不 send()、不派生 Agent 概念。"""
    tree = _tree(ROUTING_PY)
    roots = _import_roots(tree)
    forbidden = {"app.repositories.agent_adapter", "app.adapters.agents.acp",
                 "app.adapters", "app.domain.agent", "app.domain.agent_request",
                 "app.domain.agent_response"}
    hit = [r for r in roots if any(r == f or r.startswith(f + ".") for f in forbidden)]
    assert not hit, f"routing.py 触碰 Agent 概念：{hit}"

    symbols = _symbols(tree)
    for forbidden_symbol in ("AgentAdapter", "send", "AgentRequest",
                             "AgentResponse", "AgentDispatchExecutor"):
        assert forbidden_symbol not in symbols, \
            f"routing.py 出现 Agent 域符号：{forbidden_symbol}"


def test_d2_routing_selects_only_connection() -> None:
    """Routing 的仓库读路径只有 find_active_by_channel（唯一选择输入 = channel_id）。"""
    tree = _tree(ROUTING_PY)
    calls = {ast.unparse(n.func) for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert calls == {"self._uow_factory", "uow.connections.find_active_by_channel"}, calls


# ================================================================== Coordinator 边界


def test_d3_coordinator_import_allowlist() -> None:
    """Coordinator 只 import errors / runtime Port / domain——无任何 agent 细节。

    （10.8.3 翻转：+ app.domain.runtime_execution_result —— 薄结果契约。）"""
    roots = _import_roots(_tree(COORDINATOR_PY))
    allowed = {"app.application.errors", "app.application.runtime",
               "app.domain.inbound_interaction", "app.domain.outbound_response",
               "app.domain.runtime_execution_result",
               "app.domain.session_resolution"}
    assert roots == allowed, roots


def test_d4_coordinator_has_no_agent_specific_branching() -> None:
    """无 agent-specific logic 的可测定义：所有 if/elif 条件不涉及
    agent_id / content / external_conversation_id / metadata ——
    分支只允许 Session mode（REUSE_ACTIVE/CREATE_NEW）与 Fail Closed 判空。"""
    tree = _tree(COORDINATOR_PY)
    forbidden_in_conditions = {"agent_id", "content", "external_conversation_id",
                               "metadata", "connection_id"}
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            cond_names = {n.id for n in ast.walk(node.test)
                          if isinstance(n, ast.Name)} | \
                         {n.attr for n in ast.walk(node.test)
                          if isinstance(n, ast.Attribute)}
            leaked = forbidden_in_conditions & cond_names
            assert not leaked, \
                f"coordinator 条件分支出现 agent-specific 判据：{leaked}"


# ================================================================== Dispatch 边界


def test_d5_dispatch_never_selects() -> None:
    """Dispatch is execution, not selection：不重新选路、不 import Routing 实现。"""
    roots = _import_roots(_tree(DISPATCH_PY))
    assert not any(r.startswith("app.application.routing") for r in roots), roots
    assert not any(r.startswith("app.adapters") for r in roots), roots

    symbols = _symbols(_tree(DISPATCH_PY))
    for forbidden_symbol in ("find_active_by_channel", "RoutingExecutor",
                             "RoutingFailedError"):
        assert forbidden_symbol not in symbols, \
            f"agent_dispatch.py 出现选路符号：{forbidden_symbol}"
    # 唯一 Agent 调用点 = 注入的 Port（10.1.4 既有守卫的回归点名）
    calls = {ast.unparse(n.func) for n in ast.walk(_tree(DISPATCH_PY))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "send"}
    assert calls == {"self._agent_adapter.send"}, calls


# ================================================================== Adapter 边界


def test_d6_acp_adapter_is_agent_domain_only() -> None:
    """ACPAdapter 不知道 Channel / Identity / Session——Agent 域纯净。"""
    roots = _import_roots(_tree(ACP_PY))
    forbidden = {"app.domain.channel", "app.domain.identity",
                 "app.domain.session", "app.domain.session_resolution",
                 "app.domain.inbound_interaction",
                 "app.domain.outbound_response",
                 "app.repositories.session", "app.repositories.channel",
                 "app.repositories.channel_adapter", "app.repositories.identity",
                 "app.application.coordinator", "app.persistence", "app.api"}
    hit = [r for r in roots if any(r == f or r.startswith(f + ".") for f in forbidden)]
    assert not hit, f"acp.py 触碰 Channel/Session/persistence：{hit}"

    symbols = _symbols(_tree(ACP_PY))
    for forbidden_symbol in ("ChannelAdapter", "SessionRepository",
                             "IdentityRepository", "InboundInteraction",
                             "OutboundResponse"):
        assert forbidden_symbol not in symbols, \
            f"acp.py 出现跨域符号：{forbidden_symbol}"


# ================================================================== 无新基础设施


def test_d7_no_new_infrastructure_imports() -> None:
    """10.3.1 不引入任何队列/缓存/注册中心基础设施（全仓 production AST）。"""
    forbidden_roots = {"celery", "redis", "rq", "dramatiq", "huey", "kombu",
                       "kafka", "pika", "consul", "etcd3"}
    for py in APP.rglob("*.py"):
        roots = _import_roots(_tree(py))
        hit = roots & forbidden_roots
        assert not hit, f"{py.name} 引入基础设施依赖：{hit}"
