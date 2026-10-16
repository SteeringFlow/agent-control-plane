"""Step 8.A —— Runtime Boundary Closure Audit（8.1→8.5 契约层横向闭环）。

不是再写一半 Runtime，而是证明 8.1~8.5 的契约层边界已经稳定、且“有人越界时系统会
报红”。覆盖 A~N 共 14 组机器化审计。

核心结论可固化：

    RUNTIME CONTRACT LAYER
    InboundInteraction → IdentityResolution → SessionResolution
                       → RoutingResult → AgentRequest → AgentResponse
    （DispatchFailure = 失败码）

Registry 是“允许存在什么”，不是“未来应有什么”：
    IdentityResolver / SessionResolver / Router / AgentDispatcher 不进入 Registry。
"""

import ast
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]

# ---- 8.1~8.5 契约注册表（允许存在的最小集合）------------------------------
CONTRACTS = {
    # M6.6-C3 / ADR-12 §13：+ `chat_type` 承载常量（会话形态；走既有 metadata 通路，
    # **不**新增 8.1 契约字段）
    "app/domain/inbound_interaction.py": {
        "InboundInteraction", "CHAT_TYPE_METADATA_KEY",
        "CHAT_TYPE_SINGLE", "CHAT_TYPE_GROUP"},
    "app/domain/identity_resolution.py": {
        "IdentityResolution", "IDENTITY_RESOLUTION_FAILED"},
    "app/domain/session_resolution.py": {
        "SessionResolution", "SESSION_RESOLUTION_FAILED",
        "REUSE_ACTIVE", "CREATE_NEW"},
    "app/domain/routing_result.py": {"RoutingResult", "ROUTING_FAILED"},
    "app/domain/agent_request.py": {"AgentRequest"},
    "app/domain/agent_response.py": {"AgentResponse"},
    "app/domain/dispatch_failure.py": {
        "AGENT_DISPATCH_FAILED", "AGENT_TIMEOUT", "AGENT_RESPONSE_INVALID"},
}
CONTRACT_MODULES = {f"app.domain.{Path(p).stem}" for p in CONTRACTS}
# 失败码 → 归属契约文件
FAILURE_OWNER = {
    "IDENTITY_RESOLUTION_FAILED": "identity_resolution.py",
    "SESSION_RESOLUTION_FAILED": "session_resolution.py",
    "ROUTING_FAILED": "routing_result.py",
    "AGENT_DISPATCH_FAILED": "dispatch_failure.py",
    "AGENT_TIMEOUT": "dispatch_failure.py",
    "AGENT_RESPONSE_INVALID": "dispatch_failure.py",
}
FORBIDDEN_TOKENS = (
    "resolver", "router", "dispatcher", "dispatch", "routingrule", "routingpolicy",
    "routingtable", "capability", "permission", "policy", "tool", "memory",
    "agentruntime", "mcpserver", "failover", "loadbalancer", "circuitbreaker",
    "fallback", "priority", "weight", "roundrobin", "loadbalance", "authorization",
    "rbac",
)
# 每个契约的“绝不允许”字段（精确名，不做子串匹配，避免误伤 control_plane_session_id）
OWNERSHIP_FORBIDDEN = {
    "IdentityResolution": {"session", "conversation", "connection",
                            "user_id", "agent_id", "agent"},
    "SessionResolution": {"user_id", "agent_id", "routing", "rule", "policy"},
    "RoutingResult": {"identity", "session", "content", "conversation",
                      "user_id", "user"},
    "AgentRequest": {"connection_id", "channel_id", "identity_id", "user_id",
                     "permission", "capability", "tool", "memory"},
    "AgentResponse": {"routing", "identity", "session_state", "user_id",
                      "connection_id", "channel_id"},
}


# ----------------------------------------------------------------- AST helpers


def _tree(py: Path) -> ast.AST:
    return ast.parse(py.read_text(encoding="utf-8"), filename=str(py))


def _src_tree(src: str) -> ast.AST:
    return ast.parse(src, filename="<mutation>")


def _roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def _toplevel_names(src: str) -> set[str]:
    out: set[str] = set()
    for n in ast.parse(src).body:
        if isinstance(n, ast.ClassDef):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            out |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out.add(n.target.id)
    return out


def _class_field_names(tree: ast.AST, cls: str) -> set[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {s.target.id for s in node.body
                    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}
    raise AssertionError(f"{cls} 未找到")


def _field_has_default(tree: ast.AST, cls: str, field: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for s in node.body:
                if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name) \
                        and s.target.id == field:
                    return s.value is not None
    raise AssertionError(f"{cls}.{field} 未找到")


def _method_names(py: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_tree(py)):
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(sub.name)
    return names


# ================================================================== A 契约注册表


def test_a_contract_registry_complete() -> None:
    """8.1~8.5 恰好 7 个契约文件；每个文件顶层符号 = 注册表声明（不夹带额外类/DTO）。"""
    for rel, expected in CONTRACTS.items():
        py = BACKEND_DIR / rel
        assert py.is_file(), f"缺契约文件 {rel}"
        assert _toplevel_names(py.read_text(encoding="utf-8")) == expected, \
            f"{rel} 顶层符号漂移：{_toplevel_names(py.read_text(encoding='utf-8'))}"
    # 全部可导入（契约真实可用）
    import app.domain.agent_request  # noqa: F401
    import app.domain.agent_response  # noqa: F401
    import app.domain.dispatch_failure  # noqa: F401
    import app.domain.identity_resolution  # noqa: F401
    import app.domain.inbound_interaction  # noqa: F401
    import app.domain.routing_result  # noqa: F401
    import app.domain.session_resolution  # noqa: F401


def test_a_resolver_router_dispatcher_not_registered() -> None:
    """Registry 拒绝提前引入 Resolver / Router / Dispatcher。"""
    banned = {"IdentityResolver", "SessionResolver", "Router", "AgentDispatcher"}
    for rel in CONTRACTS:
        assert not (banned & _toplevel_names(
            (BACKEND_DIR / rel).read_text(encoding="utf-8"))), f"{rel} 偷渡了 Runtime 组件"


# ================================================================== B Domain 纯净度


def test_b_contract_modules_pure_domain() -> None:
    """契约模块只依赖 dataclasses / typing / 实体 domain；不 import 仓储/ORM/应用/适配。"""
    forbidden_roots = (
        "sqlalchemy", "fastapi", "starlette", "app.repositories",
        "app.persistence", "app.application", "app.adapters", "app.api",
    )
    for rel in CONTRACTS:
        tree = _tree(BACKEND_DIR / rel)
        roots = _roots(tree)
        assert not any(r.startswith(f) for r in roots for f in forbidden_roots), \
            f"{rel} 越层 import：{roots}"
        for r in roots:
            assert r in ("dataclasses", "typing") or r.startswith("app.domain"), \
                f"{rel} 不应依赖 {r}"


def test_b_no_reverse_dependency_to_repository() -> None:
    """Contract 描述结果，不执行解析：不得反向 import Repository / 执行查询。"""
    # IdentityResolution 不应 import IdentityRepository
    ir_src = (APP_DIR / "domain/identity_resolution.py").read_text(encoding="utf-8")
    assert "IdentityRepository" not in ir_src or "import" not in ir_src.split(
        "IdentityRepository")[0][-20:]
    assert "app.repositories" not in _roots(_tree(
        APP_DIR / "domain/identity_resolution.py"))
    # RoutingResult 不应 import ConnectionRepository
    rr_src = (APP_DIR / "domain/routing_result.py").read_text(encoding="utf-8")
    assert "ConnectionRepository" not in rr_src
    assert "app.repositories" not in _roots(_tree(
        APP_DIR / "domain/routing_result.py"))


# ================================================================== C 五阶段单向（无反向依赖）


def test_c_no_cross_contract_import() -> None:
    """契约模块之间不互相 import（解耦 + 无反向职责）。RoutingResult 不依赖前序契约。"""
    for rel in CONTRACTS:
        roots = _roots(_tree(BACKEND_DIR / rel))
        contract_roots = {r for r in roots if r in CONTRACT_MODULES}
        assert not contract_roots, f"{rel} 反向依赖契约：{contract_roots}"


# ================================================================== D Contract Ownership


def test_d_ownership_forbidden_fields() -> None:
    """每个契约的字段面都不含其“所有权之外”的字段。"""
    for rel, cls in (
        ("domain/identity_resolution.py", "IdentityResolution"),
        ("domain/session_resolution.py", "SessionResolution"),
        ("domain/routing_result.py", "RoutingResult"),
        ("domain/agent_request.py", "AgentRequest"),
        ("domain/agent_response.py", "AgentResponse"),
    ):
        fields = _class_field_names(_tree(APP_DIR / rel), cls)
        forbidden = OWNERSHIP_FORBIDDEN[cls]
        assert not (fields & forbidden), f"{cls} 越权字段：{fields & forbidden}"


# ================================================================== E Identity 边界


def test_e_external_identity_not_internal_id() -> None:
    """external_identity_id ≠ Identity.id / User.id；不出现 fuzzy/guess/link/merge。"""
    ii_fields = _class_field_names(
        _tree(APP_DIR / "domain/inbound_interaction.py"), "InboundInteraction")
    assert "external_identity_id" in ii_fields
    assert "identity_id" not in ii_fields
    assert "user_id" not in ii_fields
    ir_src = (APP_DIR / "domain/identity_resolution.py").read_text(encoding="utf-8")
    assert "external_identity_id" in ir_src and "channel_id" in ir_src
    # 全仓禁止“猜测式”身份匹配方法
    problems = []
    for py in sorted(APP_DIR.rglob("*.py")):
        if (APP_DIR / "persistence" / "models") in py.parents:
            continue
        for node in ast.walk(_tree(py)):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                low = node.name.lower()
                if any(t in low for t in (
                        "fuzzy", "guess", "autolink", "mergeidentity",
                        "emailfinder", "phonefinder", "findbyemail",
                        "findbyphone", "userlink", "identitymerge")):
                    problems.append(f"{py.relative_to(BACKEND_DIR)}::{node.name}")
    assert not problems, "身份边界污染：\n" + "\n".join(problems)


# ================================================================== F Session 边界


def test_f_session_boundary_preserved() -> None:
    """Session 以 (identity_id × connection_id × external_conversation_id) 隔离；
    user_id 不得进入；external_conversation_id 必填（不可弱化/可选化）。"""
    fields = _class_field_names(
        _tree(APP_DIR / "domain/session_resolution.py"), "SessionResolution")
    assert {"identity_id", "connection_id", "external_conversation_id"} <= fields
    assert "user_id" not in fields
    assert not _field_has_default(
        _tree(APP_DIR / "domain/session_resolution.py"),
        "SessionResolution", "external_conversation_id")


# ================================================================== G Connection 归属


def test_g_connection_ownership_routing_selects() -> None:
    """8.3 消费 connection_id；8.4 选择 connection_id；agent_id 来源 = Connection。"""
    sr_fields = _class_field_names(
        _tree(APP_DIR / "domain/session_resolution.py"), "SessionResolution")
    rr_fields = _class_field_names(
        _tree(APP_DIR / "domain/routing_result.py"), "RoutingResult")
    assert "connection_id" in sr_fields, "SessionResolution 必须消费 connection_id"
    assert {"connection_id", "agent_id"} <= rr_fields, "RoutingResult 必须选择 connection_id + agent_id"
    # Connection 是 agent_id 的来源
    from app.domain.connection import Connection
    import dataclasses as _dc
    assert "agent_id" in {f.name for f in _dc.fields(Connection)}


# ================================================================== H Dispatch 边界


def test_h_dispatch_does_not_reselect() -> None:
    """Dispatch 不重选 Connection/Agent：AgentRequest 只带已确定的 agent_id，
    不携带 connection_id / channel_id / identity_id / user_id。"""
    req_fields = _class_field_names(
        _tree(APP_DIR / "domain/agent_request.py"), "AgentRequest")
    assert "agent_id" in req_fields
    assert not (req_fields & {"connection_id", "channel_id",
                              "identity_id", "user_id"})


# ================================================================== I Agent Session 边界


def test_i_agent_session_boundary() -> None:
    """control_plane_session_id ≠ external_session_id；
    AgentRequest 不带 Agent 内部状态（conversation_history/memory/context/session_state）。"""
    req_fields = _class_field_names(
        _tree(APP_DIR / "domain/agent_request.py"), "AgentRequest")
    assert "control_plane_session_id" in req_fields
    assert "external_session_id" in req_fields
    assert not (req_fields & {"conversation_history", "memory",
                              "context", "session_state"})


# ================================================================== J Failure 边界


def test_j_failure_codes_stage_boundaries() -> None:
    """每个失败码只在其归属契约文件定义一次，且不与其它 stage 串味。"""
    defined: dict[str, str] = {}
    for py in sorted(APP_DIR.rglob("domain/*.py")):
        for name in _toplevel_names(py.read_text(encoding="utf-8")):
            if name in FAILURE_OWNER:
                defined[name] = py.name
    assert set(defined) == set(FAILURE_OWNER), f"失败码缺失/多余：{set(defined) ^ set(FAILURE_OWNER)}"
    for code, owner in FAILURE_OWNER.items():
        assert defined[code] == owner, f"{code} 归属错误：{defined[code]} != {owner}"


# ================================================================== K Management / Runtime 分离


def test_k_management_has_no_runtime_methods() -> None:
    """IdentityApplicationService 无 resolve_identity()；ConnectionApplicationService
    无 select_connection()/route()/dispatch()。"""
    forbidden_methods = {
        "resolve_identity", "resolve_session", "select_connection",
        "select_agent", "route", "dispatch",
    }
    # 精确检查两个管理 Service
    id_svc = APP_DIR / "application/identities.py"
    conn_svc = APP_DIR / "application/connections.py"
    assert "resolve_identity" not in _method_names(id_svc)
    assert not (forbidden_methods & _method_names(conn_svc))
    # 全 application 层不得出现运行时决策方法
    problems = []
    for py in sorted((APP_DIR / "application").rglob("*.py")):
        bad = forbidden_methods & _method_names(py)
        if bad:
            problems.append(f"{py.relative_to(BACKEND_DIR)}::{bad}")
    assert not problems, "Management 层混入 Runtime 决策：\n" + "\n".join(problems)


# ================================================================== L 禁入概念扫描


def test_l_forbidden_concept_sweep() -> None:
    """全仓（models 除外）无 Resolver/Router/Dispatcher/RoutingRule/Policy/Tool/
    Memory/AgentRuntime/Failover/CircuitBreaker 等提前偷渡概念。"""
    problems = _forbidden_class_problems()
    assert not problems, "禁入概念提前出现：\n" + "\n".join(problems)


# ================================================================== M Mutation 探测（跨层）


# 9.5 Agent Dispatch Execution（是 Execution，不是 Dispatcher）——精确白名单：
# AgentDispatchExecutor + AgentDispatchFailedError（FORBIDDEN_TOKENS 含 "dispatch"）。
RUNTIME_EXECUTION_ALLOWED = {
    ("app/application/agent_dispatch.py", "AgentDispatchExecutor"),
    ("app/application/errors.py", "AgentDispatchFailedError"),
    # Cloud-0.1.1-B：TenantResolver = 入口租户解析端口（Protocol，非 Runtime 决策实现）
    ("app/core/tenant.py", "TenantResolver"),
    # Package 2 / Step 2：Access Authorization 阶段（ADR-3/7/8）——结果契约 +
    # 执行器 + 失败类型。含 "authorization" 词，但结果只有许可集合，
    # 无 effect / role / principal / 条件组合（ADR-9 NON-GOAL 仍然成立）。
    ("app/domain/access_authorization.py", "AccessAuthorization"),
    ("app/application/access_authorization.py", "AccessAuthorizationExecutor"),
    ("app/application/errors.py", "AccessAuthorizationDeniedError"),
}


def _forbidden_class_problems() -> list[str]:
    problems = []
    for py in sorted(APP_DIR.rglob("*.py")):
        if (APP_DIR / "persistence" / "models") in py.parents:
            continue
        for node in ast.walk(_tree(py)):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                rel = "/".join(py.relative_to(BACKEND_DIR).parts)
                if (rel, node.name) in RUNTIME_EXECUTION_ALLOWED:
                    continue
                low = node.name.lower()
                if any(tok in low for tok in FORBIDDEN_TOKENS):
                    problems.append(f"{py.relative_to(BACKEND_DIR)}::{node.name}")
    return problems


@pytest.mark.parametrize("fname,src", [
    ("identity_resolver.py", "class IdentityResolver:\n    pass\n"),
    ("session_resolver.py", "class SessionResolver:\n    pass\n"),
    ("router.py", "class Router:\n    pass\n"),
    ("agent_dispatcher.py", "class AgentDispatcher:\n    pass\n"),
])
def test_m_smuggled_runtime_components_caught(fname: str, src: str) -> None:
    """注入 Resolver/Router/Dispatcher 偷渡必须报红（7.3 反向审计 + 禁入概念扫描）。"""
    smuggled = APP_DIR / "domain" / fname
    assert not smuggled.exists(), f"{fname} 不应已存在"
    try:
        smuggled.write_text(src, encoding="utf-8")
        # 7.3 反向审计必须报红
        audit_py = BACKEND_DIR / "tests" / "test_step7_3_connection_management_audit.py"
        ns: dict = {"__file__": str(audit_py), "__name__": "audit_under_8A"}
        exec(compile(audit_py.read_text(encoding="utf-8"), str(audit_py), "exec"), ns)
        problems = ns["_next_stage_problems"]()
        assert any(fname.replace(".py", "") in p.lower() or
                   src.split()[1].rstrip(":") in p for p in problems), \
            f"反向审计未抓到 {fname}：{problems}"
        # 禁入概念扫描必须报红
        assert _forbidden_class_problems(), "禁入概念扫描未抓到偷渡"
    finally:
        if smuggled.exists():
            smuggled.unlink()


@pytest.mark.parametrize("rel,cls,inject_field", [
    ("domain/agent_request.py", "AgentRequest", "connection_id: int = 0"),
    ("domain/agent_request.py", "AgentRequest", "identity_id: int = 0"),
    ("domain/routing_result.py", "RoutingResult", "user_id: int = 0"),
    ("domain/session_resolution.py", "SessionResolution", "user_id: int = 0"),
])
def test_m_field_smuggling_caught_by_ownership(rel: str, cls: str, inject_field: str) -> None:
    """往契约里夹带越权字段（connection_id/identity_id/user_id 等）必须被字段面守卫抓到。"""
    orig = (APP_DIR / rel).read_text(encoding="utf-8")
    mutated = orig.replace(
        f"class {cls}:", f"class {cls}:\n    {inject_field}\n", 1)
    fields = _class_field_names(_src_tree(mutated), cls)
    forbidden = OWNERSHIP_FORBIDDEN[cls]
    assert fields & forbidden, f"字段面守卫未抓到越权字段 {inject_field}（{fields & forbidden}）"


# ================================================================== N Runtime 契约完整性


def test_n_runtime_contract_completeness() -> None:
    """8.1→8.5 覆盖了 Runtime 入口所需最小语义；未提前引入 Authorization/Tool/Memory 等。"""
    # 注册表恰好 7 个契约文件（无多余）
    assert set(CONTRACTS) == {
        "app/domain/inbound_interaction.py",
        "app/domain/identity_resolution.py",
        "app/domain/session_resolution.py",
        "app/domain/routing_result.py",
        "app/domain/agent_request.py",
        "app/domain/agent_response.py",
        "app/domain/dispatch_failure.py",
    }
    # 仍不应存在 Runtime 决策组件（与 L 呼应）
    assert not _forbidden_class_problems()
