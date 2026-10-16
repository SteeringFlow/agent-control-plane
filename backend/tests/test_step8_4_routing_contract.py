"""Step 8.4 —— Routing Contract 冻结（不实现 Router，不设计 Routing Rules）。

8.4 冻结的是 Routing 的**输出契约**与职责边界，正好兑现 8.3 的裁决：
Connection 在 Routing 中被选择。

    InboundInteraction
        ↓ Identity Resolution（8.2）
        ↓ Session Resolution（8.3）
        ↓
    Routing                  ← 本文件冻结其输出契约（不实现 Router）
        ↓
    Connection（唯一、确定；Agent Access Path）
        ↓
    Agent

锁定项：
A  契约本体：`RoutingResult`（frozen dataclass = connection_id + agent_id）+
   `ROUTING_FAILED` 失败码；
B  输出形状：只表达「选中的 Access Path」——不含 identity/session/conversation/
   content 等会诱使职责倒流的字段；agent_id 派生自 Connection（Connection 域对象
   带 agent_id）；
C  不引入规则引擎：全仓无 Router / RoutingRule / RoutingPolicy / RoutingTable /
   priority / weight / fallback / load_balance / round_robin；
D  Fail Closed：无法唯一确定 Connection → ROUTING_FAILED；
E  mutation 探测：注入 `Router` 偷渡必须报红（6.7/7.3 白名单精确到文件+类名）。
"""

import ast
import dataclasses
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]
CONTRACT_PY = APP_DIR / "domain" / "routing_result.py"

EXPECTED_FIELDS = ["connection_id", "agent_id"]
FORBIDDEN_OUTPUT_FIELDS = {
    "identity_id", "user_id", "session_id", "external_conversation_id",
    "external_identity_id", "channel_id", "content", "candidate",
}
# 8.4 明确不做的规则引擎 / 决策权重类组件
RULES_ENGINE_TOKENS = (
    "router", "routingrule", "routingpolicy", "routingtable",
    "roundrobin", "loadbalance", "fallback", "priority", "weight",
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


def _is_frozen_dataclass(cls: ast.ClassDef) -> bool:
    return any(
        isinstance(d, ast.Call)
        and isinstance(d.func, ast.Name) and d.func.id == "dataclass"
        and any(kw.arg == "frozen" and getattr(kw.value, "value", False)
                for kw in d.keywords)
        for d in cls.decorator_list
    )


# ================================================================== A 契约本体


def test_a_contract_module_is_pure_domain() -> None:
    """契约模块只 import dataclasses（纯 domain，无 infra / 无外层）。"""
    assert CONTRACT_PY.is_file(), "缺 app/domain/routing_result.py"
    assert _roots(_tree(CONTRACT_PY)) == {"dataclasses"}, \
        f"契约模块越层导入：{_roots(_tree(CONTRACT_PY))}"


def test_a_routing_result_shape() -> None:
    """RoutingResult = frozen dataclass；字段 = 恰好 (connection_id, agent_id)。"""
    cls = _classes(_tree(CONTRACT_PY))["RoutingResult"]
    assert _is_frozen_dataclass(cls), "RoutingResult 应为 frozen dataclass"
    anns = _annassigns(cls)
    names = [s.target.id for s in anns]
    assert names == EXPECTED_FIELDS, f"字段面漂移：{names}"
    for stmt in anns:
        assert isinstance(stmt.annotation, ast.Name) and stmt.annotation.id == "int", \
            f"{stmt.target.id} 应为 int"
        assert stmt.value is None, f"{stmt.target.id} 应是必填字段（无默认值）"


def test_a_failure_code_frozen() -> None:
    """Fail-Closed 失败码 ROUTING_FAILED 已冻结。"""
    from app.domain.routing_result import ROUTING_FAILED
    assert ROUTING_FAILED == "ROUTING_FAILED"


def test_a_contract_behavior() -> None:
    """运行时行为：可构造唯一确定的 Access Path；frozen 不可变。"""
    from app.domain.routing_result import RoutingResult

    result = RoutingResult(connection_id=7, agent_id=3)
    assert result.connection_id == 7
    assert result.agent_id == 3
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.agent_id = 9  # type: ignore[misc]


def test_a_domain_package_exports() -> None:
    """domain 包导出 RoutingResult（与其他契约同待遇）。"""
    from app.domain import RoutingResult as _E1
    from app.domain.routing_result import RoutingResult as _E2

    assert _E1 is _E2


# ================================================================== B 职责边界 / 输出形状


def test_b_output_is_access_path_only() -> None:
    """输出只表达「选中的 Access Path」：无 identity/session/conversation/content 字段。

    否则 Routing 要么重复解析（identity/session 倒流），要么开始按内容选路
    （那是未来规则引擎才做的事）。
    """
    names = {s.target.id for s in _annassigns(
        _classes(_tree(CONTRACT_PY))["RoutingResult"])}
    assert not (names & FORBIDDEN_OUTPUT_FIELDS), \
        f"Routing 输出混入解析/内容字段：{names & FORBIDDEN_OUTPUT_FIELDS}"
    assert names == set(EXPECTED_FIELDS)


def test_b_agent_id_derives_from_connection() -> None:
    """agent_id 派生自 Connection（Connection 域对象带 agent_id）——不是独立选择。"""
    from app.domain.connection import Connection

    assert "agent_id" in {f.name for f in dataclasses.fields(Connection)}
    # 模块 docstring 明确派生关系，避免「选了 C 却派给别的 Agent」
    src = CONTRACT_PY.read_text(encoding="utf-8")
    assert "agent_id：**派生**自被选中 Connection" in src


def test_b_no_resolution_responsibility_in_result() -> None:
    """RoutingResult 不是 Resolver：不含任何查找/解析意图（无 key/filter/func）。"""
    src = CONTRACT_PY.read_text(encoding="utf-8")
    for marker in ("find_", "resolve_", "get_by_", "select_connection"):
        assert marker not in src.split("@dataclass")[1], f"契约不应含查找意图：{marker}"


# ================================================================== C 不引入规则引擎


def test_c_no_routing_engine_classes_anywhere() -> None:
    """全仓（models 除外）无 Router / RoutingRule / 权重 / fallback 等类或函数。"""
    problems: list[str] = []
    for py in sorted(APP_DIR.rglob("*.py")):
        if (APP_DIR / "persistence" / "models") in py.parents:
            continue
        tree = _tree(py)
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                lower = node.name.lower()
                if any(tok in lower for tok in RULES_ENGINE_TOKENS):
                    problems.append(f"{py.relative_to(BACKEND_DIR)}::{node.name}")
    assert not problems, "Routing 规则引擎被提前实现：\n" + "\n".join(problems)


def test_c_mvp_selection_basis_is_frozen_in_contract() -> None:
    """MVP 选择依据 = Channel → 唯一 Connection → Agent（确定性，非规则引擎）。"""
    src = CONTRACT_PY.read_text(encoding="utf-8")
    assert "Channel → 唯一 Connection → Agent" in src
    assert "结果必须唯一且确定" in src
    assert "ROUTING_FAILED" in src
    # 结构面：本模块只有一个 dataclass 类（RoutingResult）+ 一个失败码常量；
    # 不出现任何 Rule/Policy/Table/权重 组件（类或常量都不是）
    tree = _tree(CONTRACT_PY)
    assert set(_classes(tree)) == {"RoutingResult"}
    module_constants: set[str] = set()
    for n in tree.body:
        if isinstance(n, ast.Assign):
            module_constants |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            module_constants.add(n.target.id)
    assert module_constants == {"ROUTING_FAILED"}, \
        f"契约模块出现额外常量/组件：{module_constants}"


# ================================================================== D Fail Closed


def test_d_fail_closed_when_not_unique() -> None:
    """无法唯一确定 Connection（0 条 / 多条）→ ROUTING_FAILED，不猜测不轮询。"""
    src = CONTRACT_PY.read_text(encoding="utf-8")
    assert 'ROUTING_FAILED = "ROUTING_FAILED"' in src
    assert "Fail Closed" in src
    assert "0 条或 >1 条 → 无法唯一确定 → ROUTING_FAILED" in src
    # 输出不是候选列表：没有 list/tuple 类型字段
    cls = _classes(_tree(CONTRACT_PY))["RoutingResult"]
    for stmt in _annassigns(cls):
        assert not isinstance(stmt.annotation, ast.Subscript), \
            f"{stmt.target.id} 不可能是候选集合（必须唯一确定）"


# ================================================================== E mutation 探测


def test_e_smuggled_router_is_caught() -> None:
    """变异探测：注入 Router / routing 组件偷渡必须报红（白名单精确到文件+类名）。"""
    smuggled = APP_DIR / "domain" / "router.py"
    assert not smuggled.exists(), "router.py 不应已存在"
    try:
        smuggled.write_text(
            "class Router:\n"
            "    def route(self) -> None:\n"
            "        pass\n",
            encoding="utf-8",
        )
        # 1) 7.3 反向审计必须报红（文件 stem "router" + 类名 token "router"）
        audit_py = BACKEND_DIR / "tests" / "test_step7_3_connection_management_audit.py"
        ns: dict = {"__file__": str(audit_py), "__name__": "audit_under_8_4"}
        exec(compile(audit_py.read_text(encoding="utf-8"), str(audit_py), "exec"), ns)
        problems = ns["_next_stage_problems"]()
        assert any("router" in p.lower() for p in problems), \
            f"反向审计未抓到 Router 偷渡：{problems}"

        # 2) 本文件 C 组规则引擎守卫必须报红
        found = [
            "/".join(smuggled.relative_to(BACKEND_DIR).parts)
            for node in ast.walk(_tree(smuggled))
            if isinstance(node, (ast.ClassDef, ast.FunctionDef))
            and any(tok in node.name.lower() for tok in RULES_ENGINE_TOKENS)
        ]
        assert found, "规则引擎守卫未抓到 Router 偷渡"
    finally:
        if smuggled.exists():
            smuggled.unlink()
