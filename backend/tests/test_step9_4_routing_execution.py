"""Step 9.4 —— Routing Execution（8.4 语义的第一次真实执行）。

    channel_id + granted_connection_ids（Package 2 / Step 2 起）
        ↓ ConnectionRepository.find_active_by_channel（9.4 新增最小读路径）
        ↓ active Connections
        ↓ eligible = active ∩ granted（ADR-8：Router 只消费已授权候选）
        ↓ 数量判定（Fail Closed）
    0 条 → RoutingFailedError
    1 条 → RoutingResult(connection_id=selected.id, agent_id=selected.agent_id)
    >1 条 → RoutingFailedError

锁定项：
A  0 / 1 / 多条 三态判定（Fail Closed）；**含「active 但未被授权」= 0 条**；
B  成功时 connection_id / agent_id 取自**被选中的那条 Connection**——agent_id 是
   Connection 的**派生结果**，不是 Routing 重新选择的对象；
C  只有 status == "active" 的 Connection 参与；
D  MVP 选择输入 = channel_id + **已授权候选集**（Package 2 / Step 2，ADR-8）：
   不 import Identity/Session Resolution、不带 identity/conversation/content 参数
   ——无隐式耦合。授权只**收窄候选**；存在性 / status / 唯一性判定仍归本组件。
   候选集参数**无默认值**——省略即等于「候选不受限」，是被禁止的占位授权；
E  Repository 最小扩边：只加 find_active_by_channel（无 find_all / find_by_status /
   list_connections / find_candidates / 规则表查询）；
F  Execution ≠ Router：不创建 Router / RoutingRule / RoutingPolicy / RoutingTable，
   无 fallback / priority / weight / load_balance；
G  mutation guard。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.application import RoutingExecutor, RoutingFailedError
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.routing_result import RoutingResult
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]
ROUTING_PY = APP_DIR / "application" / "routing.py"

# 8.4 明确不做的规则引擎 / 决策权重类组件（类/函数名 token）
RULES_ENGINE_TOKENS = (
    "router", "routingrule", "routingpolicy", "routingtable", "roundrobin",
    "loadbalance", "fallback", "priority", "weight",
)
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


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _executor(db_engine) -> RoutingExecutor:
    return RoutingExecutor(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


def _graph(db_engine) -> dict[str, int]:
    """1 Channel + 2 Agents（可落多条 Connection）。"""
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        a1 = uow.agents.create(Agent(name="agent-1", type="acp"))
        a2 = uow.agents.create(Agent(name="agent-2", type="acp"))
        uow.commit()
        return {"channel": channel.id, "agent1": a1.id, "agent2": a2.id}


def _add_connection(db_engine, *, channel_id: int, agent_id: int,
                    status: str = "active") -> int:
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        conn = uow.connections.create(Connection(
            channel_id=channel_id, agent_id=agent_id, name=f"path-{agent_id}",
            status=status))
        uow.commit()
        return conn.id


# ------------------------------------------------------------------ AST helpers


def _roots(src: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(src, filename=str(ROUTING_PY))):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def _classes(src: str) -> set[str]:
    return {n.name for n in ast.walk(ast.parse(src, filename=str(ROUTING_PY)))
            if isinstance(n, ast.ClassDef)}


def _boundary_problems(src: str) -> list[str]:
    problems: list[str] = []
    for root in sorted(_roots(src)):
        if not _root_allowed(root):
            problems.append(f"越层 import {root}")
        if root.startswith("app.domain.identity_resolution") \
                or root.startswith("app.repositories.identity") \
                or root.startswith("app.repositories.session"):
            problems.append(f"不应 import 解析/会话组件：{root}")
    tree = ast.parse(src, filename=str(ROUTING_PY))
    forbidden_names = {"SqlAlchemyUnitOfWork", "Session", "sessionmaker",
                       "create_engine", "Engine", "ConnectionModel", "HTTPException",
                       "APIRouter", "IdentityRepository", "SessionRepository"}
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
    extra = _classes(src) - {"RoutingExecutor"}
    if extra:
        problems.append(f"出现非 Execution 组件类：{sorted(extra)}")
    for name in _classes(src):
        if any(tok in name.lower() for tok in RULES_ENGINE_TOKENS):
            problems.append(f"规则引擎组件偷渡：{name}")
    return problems


# ================================================================== A 三态判定


def test_a_zero_active_connections_fails(db_engine) -> None:
    g = _graph(db_engine)
    with pytest.raises(RoutingFailedError):
        _executor(db_engine).execute(g["channel"], frozenset())


def test_a_inactive_only_fails(db_engine) -> None:
    """只有 inactive Connection → 找不到 active → Fail Closed。"""
    g = _graph(db_engine)
    cid = _add_connection(db_engine, channel_id=g["channel"],
                          agent_id=g["agent1"], status="inactive")
    with pytest.raises(RoutingFailedError):
        _executor(db_engine).execute(g["channel"], frozenset({cid}))


def test_a_one_active_connection_succeeds(db_engine) -> None:
    g = _graph(db_engine)
    cid = _add_connection(db_engine, channel_id=g["channel"], agent_id=g["agent1"])
    result = _executor(db_engine).execute(g["channel"], frozenset({cid}))

    assert isinstance(result, RoutingResult)
    assert result.connection_id == cid
    assert result.agent_id == g["agent1"]


def test_a_multiple_active_connections_fail(db_engine) -> None:
    g = _graph(db_engine)
    c1 = _add_connection(db_engine, channel_id=g["channel"], agent_id=g["agent1"])
    c2 = _add_connection(db_engine, channel_id=g["channel"], agent_id=g["agent2"])
    with pytest.raises(RoutingFailedError):
        _executor(db_engine).execute(g["channel"], frozenset({c1, c2}))


def test_a_inactive_connection_not_counted(db_engine) -> None:
    """1 active + 1 inactive → success（inactive 不参与计数）。"""
    g = _graph(db_engine)
    active_id = _add_connection(db_engine, channel_id=g["channel"],
                                agent_id=g["agent1"])
    _add_connection(db_engine, channel_id=g["channel"], agent_id=g["agent2"],
                    status="inactive")
    result = _executor(db_engine).execute(g["channel"], frozenset({active_id}))
    assert result.connection_id == active_id
    assert result.agent_id == g["agent1"]


def test_a_active_but_not_granted_fails(db_engine) -> None:
    """★ Package 2 / Step 2（ADR-8）：active 但**未被授权** → 候选 0 → Fail Closed。

    这是「授权只收窄候选、不扩大」的最小反例：Connection 完全 active，
    唯一缺的是许可。
    """
    g = _graph(db_engine)
    other = _add_connection(db_engine, channel_id=g["channel"], agent_id=g["agent1"])
    with pytest.raises(RoutingFailedError):
        _executor(db_engine).execute(g["channel"], frozenset({other + 1000}))


def test_a_grant_for_another_channel_connection_is_inert(db_engine) -> None:
    """候选集里的 id 必须**同时**属于本 Channel 的 active 集合——跨 Channel 的
    授权 id 不会凭空创造候选。"""
    g = _graph(db_engine)
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        other_channel = uow.channels.create(Channel(name="wecom-2", type="wecom"))
        uow.commit()
    foreign = _add_connection(db_engine, channel_id=other_channel.id,
                              agent_id=g["agent1"])
    with pytest.raises(RoutingFailedError):
        _executor(db_engine).execute(g["channel"], frozenset({foreign}))


def test_a_invalid_channel_id_fails_closed(db_engine) -> None:
    for bad in (0, -1, "x", None):
        with pytest.raises(RoutingFailedError):
            _executor(db_engine).execute(bad, frozenset({1}))


# ================================================================== B agent_id 派生


def test_b_agent_id_derives_from_selected_connection(db_engine) -> None:
    """选了 Connection（agent_id=agent2）就不能得出别的 Agent。"""
    g = _graph(db_engine)
    cid = _add_connection(db_engine, channel_id=g["channel"], agent_id=g["agent2"])
    result = _executor(db_engine).execute(g["channel"], frozenset({cid}))
    assert result.connection_id == cid
    assert result.agent_id == g["agent2"]  # 派生自 Connection.agent_id
    assert result.agent_id != g["agent1"]  # 不会出现 Connection=10 / Agent=9


# ============ D 选择输入 = channel_id + 已授权候选集（无隐式耦合，无占位授权）


def test_d_execute_takes_channel_and_granted_candidates() -> None:
    """选择输入 = channel_id + granted_connection_ids；无 identity/session/content 参数。

    Package 2 / Step 2（ADR-8）：第二条参数是**授权产出**，且**不得有默认值**——
    有默认值就等于允许「候选不受限」的占位授权从签名后门回来。
    """
    src = ROUTING_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "RoutingExecutor")
    method = next(n for n in cls.body
                  if isinstance(n, ast.FunctionDef) and n.name == "execute")
    params = [a.arg for a in method.args.args if a.arg != "self"]
    assert params == ["channel_id", "granted_connection_ids"], f"execute 参数漂移：{params}"
    assert method.args.defaults == [], "候选集不得有默认值（占位授权）"
    assert method.args.kwonlyargs == [], f"不新增关键字参数：{method.args.kwonlyargs}"


def test_d_no_identity_session_coupling() -> None:
    """Routing 不 import Identity/Session Resolution——IdentityResolution 与 Routing
    之间无隐式耦合（不因 identity/conversation/content 改变选择）。"""
    src = ROUTING_PY.read_text(encoding="utf-8")
    roots = _roots(src)
    assert not any(r.startswith("app.repositories.identity") for r in roots)
    assert not any(r.startswith("app.repositories.session") for r in roots)
    assert "app.domain.routing_result" in roots
    # 模块类面只有 RoutingExecutor
    assert _classes(src) == {"RoutingExecutor"}


# ================================================================== E Repository 最小扩边


def test_e_connection_repository_adds_only_one_read_method() -> None:
    """Port + Adapter 方法面：runtime 读路径 = find_active_by_channel（9.4）；
    11.11 再 + list_connections（Management 陈列读，与 runtime 读互不替代）；
    仍无 find_all / 规则表查询等 Routing 扩张。"""
    import app.persistence.repositories.connection as adapter
    import app.repositories.connection as port

    def _methods(module) -> set[str]:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        return {n.name for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name != "__init__"}

    expected = {"get_by_id", "create", "update", "find_by_channel_and_agent",
                "find_active_by_channel", "list_connections"}
    assert _methods(port) == expected, _methods(port)
    assert _methods(adapter) == expected, _methods(adapter)
    for banned in ("find_by_status", "find_all", "find_candidates",
                   "find_routing_rules", "find_by_agent"):
        assert banned not in _methods(port), f"不应扩张 {banned}"


# ================================================================== F Execution ≠ Router


def test_f_no_rules_engine_classes() -> None:
    """模块只有 RoutingExecutor：无 Router / RoutingRule / Policy / Table / 权重。"""
    src = ROUTING_PY.read_text(encoding="utf-8")
    assert not _boundary_problems(src), _boundary_problems(src)
    for banned in ("Router", "RoutingRule", "RoutingPolicy", "RoutingTable"):
        assert f"class {banned}" not in src, f"不应出现 {banned}"


def test_f_module_imports_only_ports_and_domain() -> None:
    src = ROUTING_PY.read_text(encoding="utf-8")
    roots = _roots(src)
    for r in roots:
        assert not any(r == f or r.startswith(f + ".") for f in FORBIDDEN_ROOTS), \
            f"越层 import {r}"
    assert any(r == "app.repositories" or r.startswith("app.repositories.")
               for r in roots), "应依赖 repositories 端口"
    read_calls = {
        node.func.attr for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
    }
    assert read_calls == {"find_active_by_channel"}, read_calls


# ================================================================== G mutation 探测


@pytest.mark.parametrize("inject", [
    "import sqlalchemy",
    "from app.persistence.models.connection import ConnectionModel",
    "from app.repositories.identity import IdentityRepository",
])
def test_g_forbidden_import_is_caught(inject: str) -> None:
    src = ROUTING_PY.read_text(encoding="utf-8") + "\n" + inject + "\n"
    assert _boundary_problems(src), f"边界守卫未抓到 {inject}"


def test_g_write_call_is_caught() -> None:
    src = ROUTING_PY.read_text(encoding="utf-8").replace(
        "active = uow.connections.find_active_by_channel(channel_id)",
        "active = uow.connections.create(Connection(channel_id=1, agent_id=2, name='x'))",
    )
    assert any("不应写" in p for p in _boundary_problems(src)), "写调用未被守卫抓住"


def test_g_router_injection_caught_by_reverse_audit() -> None:
    """真实文件注入 Router → 7.3 反向审计必须报红（Execution ≠ Router）。"""
    backup = ROUTING_PY.read_text(encoding="utf-8")
    try:
        ROUTING_PY.write_text(backup + "\n\nclass Router:\n    pass\n", encoding="utf-8")
        audit_py = BACKEND_DIR / "tests" / "test_step7_3_connection_management_audit.py"
        ns: dict = {"__file__": str(audit_py), "__name__": "audit_under_9_4"}
        exec(compile(audit_py.read_text(encoding="utf-8"), str(audit_py), "exec"), ns)
        problems = ns["_next_stage_problems"]()
        assert any("Router" in p for p in problems), \
            f"反向审计未抓到 Router 偷渡：{problems}"
        assert _boundary_problems(ROUTING_PY.read_text(encoding="utf-8")), \
            "模块类面守卫未抓到偷渡"
    finally:
        ROUTING_PY.write_text(backup, encoding="utf-8")
