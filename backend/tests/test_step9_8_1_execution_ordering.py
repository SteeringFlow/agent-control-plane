"""Step 9.8.1 —— Runtime Execution Ordering Resolution（Ordering Architecture Tests）。

第一次发现真实的「契约语义顺序 ≠ 实际执行顺序」问题，并正式修正：

    Runtime Contract Graph（8.x 契约定义职责与数据依赖，不等同执行顺序）
        Inbound
          ├── external_identity_id → Identity ── identity.id ──┐
          ├── channel_id → Routing ── connection_id/agent_id ──┤
          └── external_conversation_id ─────────────────────────┤
                                                               ▼
                                                        Session Resolution
                                                       （REUSE / CREATE_NEW）
                                                               ▼
                                                         Dispatch ──> Delivery

    Runtime Execution Order（本文件冻结）
        identity → routing → session_resolution → [session_creation?] →
        agent_dispatch → delivery

为什么 Routing 先于 Session：`SessionResolutionExecutor` 必填 `connection_id`
（Routing 的产物）；Routing 只需要 `channel_id`。8.1~8.6 的契约不要求按旧顺序执行。

本文件是 **Ordering Architecture Tests**（不是业务测试）：
①  顺序：IDENTITY → ROUTING → SESSION → SESSION_CREATION(conditional) → DISPATCH → DELIVERY
②  Routing 必须先于 Session（Session 需要 connection_id，Routing 产出它）
③  Session 不得自己 Routing
④  Routing 不得创建 Session
⑤  CREATE_NEW 才允许 Session Creation（REUSE → NO CREATE）
⑥  Dispatch 必须位于 Session 之后（AgentRequest.control_plane_session_id 已获得）
⑦  Delivery 必须最后
⑧  Fail Closed 顺序一起冻结（… → STOP / Dispatch → NO DELIVERY / Delivery → 仅 delivery=FAILED）
"""

import ast
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]
RUNTIME_PY = APP_DIR / "application" / "runtime.py"

CANONICAL = ("identity_resolution", "authorization", "routing",
             "session_resolution", "session_creation", "agent_dispatch",
             "delivery")


# ------------------------------------------------------------------ helpers


def _method_params(rel: str, cls: str, method: str) -> set[str]:
    tree = ast.parse((BACKEND_DIR / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == method:
                    args = list(sub.args.args) + list(sub.args.kwonlyargs)
                    return {a.arg for a in args if a.arg != "self"}
    raise AssertionError(f"{rel}::{cls}.{method} 未找到")


def _module_roots(rel: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse((BACKEND_DIR / rel).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def _roots_of(src: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def _module_src(rel: str) -> str:
    return (BACKEND_DIR / rel).read_text(encoding="utf-8")


def _runtime_src() -> str:
    return RUNTIME_PY.read_text(encoding="utf-8")


def _order() -> tuple[str, ...]:
    from app.application.runtime import RUNTIME_EXECUTION_ORDER
    return RUNTIME_EXECUTION_ORDER


# ================================================================== ① 顺序


def test_order_is_canonical_seven_stages() -> None:
    """Implementation Package 2 / Step 2：六段 → 七段（ADR-7/ADR-8 插入 authorization）。

    既有六段的**相对顺序一字不改**；authorization 落在 Identity 之后、
    Routing 之前（授权先收窄候选，再由 Router 选择）。
    """
    order = _order()
    assert order == CANONICAL, f"顺序漂移：{order}"
    assert order[-1] == "delivery"
    assert order.index("session_creation") == order.index("session_resolution") + 1
    assert order.index("session_creation") < order.index("agent_dispatch")
    assert order.index("identity_resolution") < order.index("authorization")
    assert order.index("authorization") < order.index("routing")


def test_order_includes_conditional_session_creation() -> None:
    src = _runtime_src()
    # session_creation 是条件阶段（注释明确：仅 CREATE_NEW 分支执行）
    assert "conditional" in src or "仅 CREATE_NEW" in src


# ================================================================== ② Routing 先于 Session


def test_routing_precedes_session_in_order() -> None:
    order = _order()
    assert order.index("routing") < order.index("session_resolution")


def test_evidence_session_requires_connection_routing_produces_it() -> None:
    """Session executor 必填 connection_id；Routing executor 只要 channel_id。"""
    session_params = _method_params("app/application/session_resolution.py",
                                    "SessionResolutionExecutor", "execute")
    routing_params = _method_params("app/application/routing.py",
                                    "RoutingExecutor", "execute")
    assert "connection_id" in session_params  # 输入依赖：Session 需要 Routing 产物
    # Package 2 / Step 2：Routing 新增**已授权候选集**输入（ADR-8：Router 只消费
    # 授权产出，不自行扩大候选集）；它仍不依赖 Session / Identity。
    assert routing_params == {"channel_id", "granted_connection_ids"}


# ================================================================== ③ Session 不得自己 Routing


def test_session_stage_does_not_self_route() -> None:
    for rel in ("app/application/session_resolution.py",
                "app/application/session_creation.py"):
        roots = _module_roots(rel)
        bad = [r for r in roots
               if r.startswith("app.application.routing")
               or r.startswith("app.repositories.connection")
               or r.startswith("app.repositories.agent")]
        assert not bad, f"{rel} 不应自己 Routing：{bad}"
        assert "RoutingExecutor" not in _module_src(rel)


def test_session_self_routing_mutation_is_caught() -> None:
    """若有人在 Session 执行里 import RoutingExecutor → 守卫必须报红。"""
    src = _module_src("app/application/session_resolution.py") \
        + "\nfrom app.application.routing import RoutingExecutor\n"
    roots = _roots_of(src)
    assert any(r.startswith("app.application.routing") for r in roots), "mutation 未生效"


# ================================================================== ④ Routing 不得创建 Session


def test_routing_does_not_create_session() -> None:
    """Routing 不 import Session 仓储/创建执行（docstring 里的否定式提及不算，看 AST）。"""
    src = _module_src("app/application/routing.py")
    roots = _module_roots("app/application/routing.py")
    bad = [r for r in roots
           if r.startswith("app.repositories.session")
           or r.startswith("app.application.session_creation")
           or r.startswith("app.application.session_resolution")]
    assert not bad, f"Routing 不应创建/解析 Session：{bad}"
    tree = ast.parse(src)
    class_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert "SessionCreationExecutor" not in class_names
    # 只允许对 connections 仓储的读调用：无任何 create（尤其对 sessions）
    call_attrs = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "create" not in call_attrs, f"Routing 不应写：{call_attrs}"


def test_routing_creates_session_mutation_is_caught() -> None:
    src = _module_src("app/application/routing.py") \
        + "\nfrom app.application.session_creation import SessionCreationExecutor\n"
    roots = _roots_of(src)
    assert any(r.startswith("app.application.session_creation") for r in roots), \
        "mutation 未生效"


# ================================================================== ⑤ CREATE_NEW 才创建


def test_reuse_does_not_create_new_session() -> None:
    """REUSE_ACTIVE → NO CREATE（语义冻结在 Coordinator 边界）。"""
    src = _runtime_src()
    assert "REUSE_ACTIVE" in src
    assert "CREATE_NEW" in src
    # 两个分支的差异：REUSE 不创建；CREATE_NEW 才物化 Session（9.7）
    assert "不创建" in src and "CREATE_NEW 分支才物化" in src


def test_creation_sits_between_session_and_dispatch() -> None:
    order = _order()
    # Session Creation 只在 CREATE_NEW 分支，位置在 session 与 dispatch 之间
    assert order.index("session_resolution") < order.index("session_creation") \
        < order.index("agent_dispatch")


# ================================================================== ⑥ Dispatch 在 Session 之后


def test_dispatch_after_session_in_order() -> None:
    order = _order()
    assert order.index("session_resolution") < order.index("agent_dispatch")
    assert order.index("session_creation") < order.index("agent_dispatch")


def test_evidence_dispatch_needs_cp_session_id() -> None:
    """AgentRequest.control_plane_session_id 必须先获得 → Dispatch 在 Session 之后。"""
    params = _method_params("app/application/agent_dispatch.py",
                            "AgentDispatchExecutor", "execute")
    assert "routing_result" in params
    assert "control_plane_session_id" in params


# ================================================================== ⑦ Delivery 最后


def test_delivery_is_last_in_order() -> None:
    order = _order()
    assert order[-1] == "delivery"
    assert order.index("agent_dispatch") < order.index("delivery")


def test_evidence_delivery_consumes_only_outbound() -> None:
    params = _method_params("app/application/delivery.py", "DeliveryExecutor", "execute")
    assert params == {"outbound"}, params


@pytest.mark.parametrize("reversed_seq", [
    "Dispatch → Routing", "Delivery → Dispatch", "Delivery → Routing",
    "Session Resolution → Routing", "Routing → Identity",
])
def test_reversed_orders_frozen_in_docstring(reversed_seq: str) -> None:
    assert reversed_seq in _runtime_src()


# ================================================================== ⑧ Fail Closed


def test_fail_closed_order_frozen() -> None:
    src = _runtime_src()
    for phrase in ("Identity failure → STOP", "Routing failure → STOP",
                   "Session failure → STOP", "Session Creation failure → STOP",
                   "Dispatch failure → NO Delivery",
                   "processing_status ≠ delivery_status",
                   "delivery = FAILED"):
        assert phrase in src, f"缺 Fail-Closed 冻结：{phrase}"


def test_fail_closed_is_stage_ordered() -> None:
    """Fail Closed 随执行顺序冻结：session_creation 失败同样 STOP（先于 dispatch）。"""
    order = _order()
    assert order.index("session_creation") < order.index("agent_dispatch")
    src = _runtime_src()
    assert "Session Creation failure → STOP" in src
    assert "Dispatch failure → NO Delivery" in src


# ================================================================== 修正架构表述


def test_contract_graph_vs_execution_order_distinguished() -> None:
    """Runtime Contract Graph ≠ Runtime Execution Order（8.x 顺序不是执行顺序）。"""
    src = _runtime_src()
    assert "Runtime Contract Graph" in src
    assert "Runtime Execution Order" in src
    assert "不等同于执行顺序" in src
    assert "不是架构对称性" in src
