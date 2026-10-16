"""Step 9.1 —— Runtime Execution Boundary & Coordinator Contract（不实现管线）。

9.x 总目标：让 8.x 契约获得执行能力，最终真实执行一次
Channel → Control Plane → Agent → Control Plane → Channel。

9.1 只冻结 **Runtime Execution Boundary**：
    RuntimeExecutionCoordinator = Execution Coordinator（编排 8.x 已冻结边界），
    不是 Resolver / Router / Dispatcher / DeliveryService——9.2~9.6 才让各阶段
    契约获得执行能力。

锁定项：
A  Boundary 存在：application/runtime.py，接受 InboundInteraction；
B  只依赖允许的抽象：不 import SQLAlchemy / persistence / adapters / api；
   无任何对象实例化（不直接访问 ORM / Adapter 实现）；
C  Execution Ordering（9.8.1 依输入依赖裁决）：RUNTIME_EXECUTION_ORDER =
   identity → routing → session → [session_creation（仅 CREATE_NEW）] →
   dispatch → delivery（不允许 Routing→Identity / Session→Routing /
   Dispatch→Routing / Delivery→Dispatch / Delivery→Routing）；
D  Fail Closed：前置阶段失败 STOP；Dispatch 失败 NO Delivery；
   Agent failed → No Delivery（delivery 条件 = AgentResponse 成功）；
E  不重新定义 8.x Domain Contract（一律从 app.domain import）；
F  不引入 RuntimeResult 巨型 DTO；不产生新的 Resolver/Router/Dispatcher；
G  mutation 探测：注入 sqlalchemy / 倒序 / RuntimeResult / IdentityResolver /
   AgentDispatcher 必须报红。
"""

import ast
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]
RUNTIME_PY = APP_DIR / "application" / "runtime.py"

COORDINATOR = "RuntimeExecutionCoordinator"
# 8.x 已冻结契约：一律 import，不得在此重新定义
CONTRACT_NAMES = {
    "InboundInteraction", "IdentityResolution", "SessionResolution",
    "RoutingResult", "AgentRequest", "AgentResponse", "OutboundResponse",
}
# 9.1 仍是 Coordinator：不得提前实现 Runtime 组件（类名不得带这些 token）
RUNTIME_COMPONENT_TOKENS = (
    "resolver", "router", "dispatcher", "dispatch", "delivery", "capability",
    "policy", "permission", "tool", "memory", "authorization", "adapter",
)
FORBIDDEN_ROOTS = (
    "sqlalchemy", "fastapi", "starlette", "pydantic",
    "app.api", "app.persistence", "app.adapters", "app.schemas",
)
CANONICAL_EXECUTION_ORDER = (
    "identity_resolution", "authorization", "routing", "session_resolution",
    "session_creation", "agent_dispatch", "delivery",
)


# ------------------------------------------------------------------ AST helpers


def _roots(src: str) -> set[str]:
    roots: set[str] = set()
    tree = ast.parse(src, filename="<runtime>")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def _class_names(src: str) -> set[str]:
    return {n.name for n in ast.walk(ast.parse(src, filename="<runtime>"))
            if isinstance(n, ast.ClassDef)}


def _method_names(src: str) -> set[str]:
    names: set[str] = set()
    tree = ast.parse(src, filename="<runtime>")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef):
                    names.add(sub.name)
    return names


def _stages(src: str) -> tuple[str, ...]:
    tree = ast.parse(src, filename="<runtime>")
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "RUNTIME_EXECUTION_ORDER":
                    assert isinstance(node.value, ast.Tuple)
                    return tuple(el.value for el in node.value.elts
                                 if isinstance(el, ast.Constant))
    raise AssertionError("缺 RUNTIME_EXECUTION_ORDER 常量")


def _boundary_problems(src: str) -> list[str]:
    """Runtime Boundary 综合守卫：越层依赖 / 实例化 / 契约重定义 / 组件偷渡 /
    巨型 DTO / 未引用 InboundInteraction。"""
    problems: list[str] = []
    for root in sorted(_roots(src)):
        if any(root == f or root.startswith(f + ".") for f in FORBIDDEN_ROOTS):
            problems.append(f"越层 import {root}")
    if any(isinstance(n, ast.Call) for n in ast.walk(ast.parse(src, filename="<runtime>"))):
        problems.append("出现对象实例化（Boundary 不应直接构造任何东西）")
    classes = _class_names(src)
    if classes & CONTRACT_NAMES:
        problems.append(f"重新定义 8.x 契约：{classes & CONTRACT_NAMES}")
    extra = classes - {COORDINATOR}
    if extra:
        problems.append(f"多余符号/巨型 DTO：{sorted(extra)}")
    for name in classes:
        if any(tok in name.lower() for tok in RUNTIME_COMPONENT_TOKENS):
            problems.append(f"Runtime 组件偷渡：{name}")
    roots = _roots(src)
    if "app.domain.inbound_interaction" not in roots \
            and "InboundInteraction" not in src:
        problems.append("Boundary 未引用 InboundInteraction")
    return problems


# ================================================================== A Boundary 存在


def test_a_runtime_application_boundary_exists() -> None:
    """Runtime Execution application boundary 存在并可经 application 包导入。"""
    assert RUNTIME_PY.is_file(), "缺 application/runtime.py"
    from app.application import RuntimeExecutionCoordinator as _E1
    from app.application.runtime import RuntimeExecutionCoordinator as _E2

    assert _E1 is _E2


# ================================================================== B 只依赖抽象


def test_b_allowed_abstraction_deps_only() -> None:
    """Boundary 只 import typing / app.domain；无越层依赖、无对象实例化。

    （10.8.3 翻转：+ app.domain.runtime_execution_result —— 10.8.2 薄结果
    契约正式进入 Runtime 边界签名。）"""
    src = RUNTIME_PY.read_text(encoding="utf-8")
    assert _roots(src) <= {"typing", "app.domain", "app.domain.inbound_interaction",
                           "app.domain.runtime_execution_result"}, \
        f"依赖面漂移：{_roots(src)}"
    assert not _boundary_problems(src), _boundary_problems(src)


def test_b_no_orm_or_adapter_access() -> None:
    """不直接访问 ORM / Adapter：代码无 session.query / db.execute /
    ChannelAdapter(...) / ACPAdapter(...) 等调用。"""
    src = RUNTIME_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        node.func.id if isinstance(node.func, ast.Name) else ast.unparse(node.func)
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    ]
    assert not calls, f"Boundary 不应构造/调用任何东西：{calls}"


# ================================================================== C Execution Ordering


def test_c_execution_ordering_locked() -> None:
    """顺序固定（9.8.1 依输入依赖）：identity → authorization → routing →
    session → [session_creation?] → dispatch → delivery。

    Package 2 / Step 2：authorization 由 ADR-7/ADR-8 插入（Identity 之后、
    Routing 之前）；既有六段相对顺序未变。
    """
    from app.application.runtime import RUNTIME_EXECUTION_ORDER

    assert RUNTIME_EXECUTION_ORDER == CANONICAL_EXECUTION_ORDER, \
        f"顺序漂移：{RUNTIME_EXECUTION_ORDER}"
    # delivery 最后；routing 先于 session_resolution（Session 需要 Routing 产出的
    # connection_id）；session_creation 是条件阶段（位于 session 与 dispatch 之间）
    assert RUNTIME_EXECUTION_ORDER[-1] == "delivery"
    assert RUNTIME_EXECUTION_ORDER.index("identity_resolution") < \
        RUNTIME_EXECUTION_ORDER.index("routing")
    assert RUNTIME_EXECUTION_ORDER.index("routing") < \
        RUNTIME_EXECUTION_ORDER.index("session_resolution")
    assert RUNTIME_EXECUTION_ORDER.index("session_resolution") < \
        RUNTIME_EXECUTION_ORDER.index("session_creation")
    assert RUNTIME_EXECUTION_ORDER.index("session_creation") < \
        RUNTIME_EXECUTION_ORDER.index("agent_dispatch")


def test_c_no_reversed_order_frozen() -> None:
    """docstring 冻结反向序列：Routing→Identity / Dispatch→Routing / Delivery→Routing。"""
    src = RUNTIME_PY.read_text(encoding="utf-8")
    for seq in ("Routing → Identity", "Dispatch → Routing", "Delivery → Routing"):
        assert seq in src


# ================================================================== D Fail Closed / Delivery 条件


def test_d_fail_closed_contract_frozen() -> None:
    """Fail Closed：前置失败 STOP；Dispatch 失败 NO Delivery；Agent failed → No Delivery。"""
    src = RUNTIME_PY.read_text(encoding="utf-8")
    for phrase in ("Fail Closed", "STOP", "NO Delivery", "No Delivery"):
        assert phrase in src, f"缺 Fail-Closed 语义：{phrase}"


def test_d_delivery_requires_agent_success() -> None:
    """Delivery 条件：只有 AgentResponse 成功才进入 Delivery（agent 在 delivery 之前）。"""
    from app.application.runtime import RUNTIME_EXECUTION_ORDER

    dispatch = RUNTIME_EXECUTION_ORDER.index("agent_dispatch")
    delivery = RUNTIME_EXECUTION_ORDER.index("delivery")
    assert dispatch < delivery  # dispatch 失败 → 永远到不了 delivery
    src = RUNTIME_PY.read_text(encoding="utf-8")
    assert "AgentResponse 成功" in src or "agent_response 成功" in src


# ================================================================== E 不重新定义 / 巨型 DTO


def test_e_contracts_imported_not_redefined() -> None:
    """不重新定义 8.x Domain Contract：契约一律从 app.domain import。"""
    src = RUNTIME_PY.read_text(encoding="utf-8")
    assert "from app.domain.inbound_interaction import InboundInteraction" in src
    assert not (_class_names(src) & CONTRACT_NAMES), "重定义 8.x 契约"


def test_e_no_runtime_result_dto() -> None:
    """不引入 RuntimeResult 巨型 DTO：模块顶层类面 = 恰好 {Coordinator}。"""
    src = RUNTIME_PY.read_text(encoding="utf-8")
    assert _class_names(src) == {COORDINATOR}, _class_names(src)
    assert "class RuntimeResult" not in src


def test_e_no_new_component_classes() -> None:
    """不产生 Resolver/Router/Dispatcher/DeliveryService 类（也不藏方法名里）。"""
    src = RUNTIME_PY.read_text(encoding="utf-8")
    assert not _boundary_problems(src)
    # Coordinator 只暴露 execute——是 Coordinator，不是各阶段 Service
    assert _method_names(src) == {"execute"}, _method_names(src)


# ================================================================== G mutation 探测


def test_m_sqlalchemy_import_is_caught() -> None:
    src = RUNTIME_PY.read_text(encoding="utf-8") + "\nimport sqlalchemy\n"
    assert any("sqlalchemy" in p for p in _boundary_problems(src)), \
        "Boundary 守卫未抓到 sqlalchemy 越层 import"


def test_m_reversed_stages_is_caught() -> None:
    """倒序 mutation：session 提到 routing 之前（Session→Routing）必须报红。"""
    src = RUNTIME_PY.read_text(encoding="utf-8").replace(
        '"identity_resolution",\n    "authorization",',
        '"session_resolution",\n    "routing",\n    "identity_resolution",\n'
        '    "authorization",',
    )
    assert _stages(src) != CANONICAL_EXECUTION_ORDER, "mutation 未生效"


def test_m_runtime_result_dto_is_caught() -> None:
    src = RUNTIME_PY.read_text(encoding="utf-8") + \
        "\n@dataclasses.dataclass(frozen=True)\nclass RuntimeResult:\n    pass\n"
    problems = _boundary_problems(src)
    assert any("RuntimeResult" in p or "巨型 DTO" in p for p in problems), problems


def test_m_resolver_class_is_caught() -> None:
    src = RUNTIME_PY.read_text(encoding="utf-8") + "\nclass IdentityResolver:\n    pass\n"
    problems = _boundary_problems(src)
    assert any("IdentityResolver" in p for p in problems), problems


def test_m_contract_redefinition_is_caught() -> None:
    src = RUNTIME_PY.read_text(encoding="utf-8") + \
        "\nclass InboundInteraction:\n    pass\n"
    problems = _boundary_problems(src)
    assert any("重新定义 8.x 契约" in p for p in problems), problems


def test_m_real_file_dispatcher_injection_caught_by_reverse_audit() -> None:
    """真实文件注入 AgentDispatcher → 7.3 反向审计必须报红（不能只靠本文件守卫）。"""
    backup = RUNTIME_PY.read_text(encoding="utf-8")
    try:
        RUNTIME_PY.write_text(
            backup + "\n\nclass AgentDispatcher:\n    def dispatch(self) -> None:\n        pass\n",
            encoding="utf-8",
        )
        audit_py = BACKEND_DIR / "tests" / "test_step7_3_connection_management_audit.py"
        ns: dict = {"__file__": str(audit_py), "__name__": "audit_under_9_1"}
        exec(compile(audit_py.read_text(encoding="utf-8"), str(audit_py), "exec"), ns)
        problems = ns["_next_stage_problems"]()
        assert any("AgentDispatcher" in p for p in problems), \
            f"反向审计未抓到偷渡：{problems}"
    finally:
        RUNTIME_PY.write_text(backup, encoding="utf-8")
