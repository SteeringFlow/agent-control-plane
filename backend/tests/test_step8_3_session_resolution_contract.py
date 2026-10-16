"""Step 8.3 —— Session Resolution Contract 冻结（不实现 SessionResolver）。

8.3 只冻结契约：给定已解析成功的 Identity（8.2）+ 一个**确定的** Connection
+ Channel-side conversation，Runtime 应该得到哪个 Session（复用 active /
新建），并回答「Connection 归属」这一架构问题。

    Resolved Identity / User + Connection（已确定） + external_conversation_id
              │
              ▼
    SessionResolution（REUSE_ACTIVE / CREATE_NEW）
              │
              ▼
    Session

锁定项：
A  契约本体：纯 domain（只 import dataclasses），`SessionResolution` frozen
   dataclass（mode + identity_id + connection_id + external_conversation_id +
   existing_session_id?）+ 模式常量 / Fail-Closed 失败码；
B  边界：actor = identity_id（channel-scoped，不是 User.id）；conversation 隔离
   由 external_conversation_id 保持；Session 实体/Repository/Resolver 仍未建；
C  连接归属裁决：Connection 是**输入**（required、不由本契约选择）——无
   agent_id / user_id / 选择意图字段；
D  Fail Closed：SESSION_RESOLUTION_FAILED 失败码存在；决策 all-or-nothing；
E  mutation 探测：注入 SessionResolver 偷渡必须报红（白名单精确到文件+类名）。
"""

import ast
import dataclasses
from pathlib import Path

import pytest

from app.persistence.database import Base

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"
CONTRACT_PY = APP_DIR / "domain" / "session_resolution.py"

EXPECTED_FIELDS = [
    "mode", "identity_id", "connection_id", "external_conversation_id",
    "existing_session_id",
    "external_session_id",  # 15.5.2：Agent 侧引用只读投影（REUSE 时带出）
]
EXPECTED_TYPES = {
    "mode": "str",
    "identity_id": "int",
    "connection_id": "int",
    "external_conversation_id": "str",
    "existing_session_id": "int | None",
    "external_session_id": "str | None",
}
# Connection 归属裁决：本契约不允许出现任何「选 Connection / 选 Agent」字段
FORBIDDEN_FIELDS = {"agent_id", "user_id", "channel_id", "candidate",
                    "policy", "priority", "routing"}


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
    a = stmt.annotation
    if isinstance(a, ast.Name):
        return a.id
    if isinstance(a, ast.BinOp) and isinstance(a.op, ast.BitOr):
        left = a.left
        right = "None" if isinstance(a.right, ast.Constant) and a.right.value is None \
            else (a.right.id if isinstance(a.right, ast.Name) else "?")
        left_t = left.id if isinstance(left, ast.Name) else "?"
        return f"{left_t} | {right}"
    return "?"


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
    """契约模块只 import dataclasses（纯 domain，无 infra / 无外层 / 无 Session 实体依赖）。"""
    assert CONTRACT_PY.is_file(), "缺 app/domain/session_resolution.py"
    assert _roots(_tree(CONTRACT_PY)) == {"dataclasses"}, \
        f"契约模块越层导入：{_roots(_tree(CONTRACT_PY))}"


def test_a_resolution_decision_shape() -> None:
    """SessionResolution = frozen dataclass；字段面 = 恰好 5 个（顺序冻结）。"""
    cls = _classes(_tree(CONTRACT_PY))["SessionResolution"]
    assert _is_frozen_dataclass(cls), "SessionResolution 应为 frozen dataclass"
    anns = _annassigns(cls)
    names = [s.target.id for s in anns]
    assert names == EXPECTED_FIELDS, f"字段面漂移：{names}"

    for field, expected_type in EXPECTED_TYPES.items():
        ann = next(s for s in anns if s.target.id == field)
        assert _ann_text(ann) == expected_type, \
            f"{field} 类型漂移：{_ann_text(ann)}"

    # mode / identity_id / connection_id / external_conversation_id 必填；
    # existing_session_id 可选（默认 None，仅 REUSE_ACTIVE 时有值）
    for field in EXPECTED_FIELDS[:4]:
        ann = next(s for s in anns if s.target.id == field)
        assert ann.value is None, f"{field} 应是必填字段（无默认值）"
    existing = next(s for s in anns if s.target.id == "existing_session_id")
    assert existing.value is not None, "existing_session_id 应是可选字段"


def test_a_mode_and_failure_constants_frozen() -> None:
    """REUSE_ACTIVE / CREATE_NEW / SESSION_RESOLUTION_FAILED 已冻结。"""
    from app.domain.session_resolution import (
        CREATE_NEW,
        REUSE_ACTIVE,
        SESSION_RESOLUTION_FAILED,
    )

    assert REUSE_ACTIVE == "REUSE_ACTIVE"
    assert CREATE_NEW == "CREATE_NEW"
    assert SESSION_RESOLUTION_FAILED == "SESSION_RESOLUTION_FAILED"


def test_a_contract_behavior() -> None:
    """运行时行为：REUSE_ACTIVE 带 existing_session_id；CREATE_NEW 不带；frozen。"""
    from app.domain.session_resolution import (
        CREATE_NEW,
        REUSE_ACTIVE,
        SessionResolution,
    )

    reuse = SessionResolution(
        mode=REUSE_ACTIVE,
        identity_id=11,
        connection_id=7,
        external_conversation_id="conv-001",
        existing_session_id=42,
    )
    assert reuse.mode == REUSE_ACTIVE
    assert reuse.existing_session_id == 42

    create = SessionResolution(
        mode=CREATE_NEW,
        identity_id=11,
        connection_id=7,
        external_conversation_id="conv-002",
    )
    assert create.existing_session_id is None

    with pytest.raises(dataclasses.FrozenInstanceError):
        reuse.mode = CREATE_NEW  # type: ignore[misc]


def test_a_domain_package_exports() -> None:
    """domain 包导出 SessionResolution（与 InboundInteraction/IdentityResolution 同待遇）。"""
    from app.domain import SessionResolution as _E1
    from app.domain.session_resolution import SessionResolution as _E2

    assert _E1 is _E2


# ================================================================== B 边界


def test_b_actor_is_channel_scoped_identity_not_user() -> None:
    """actor 组件 = identity_id（不是 User.id）；本契约无 user_id 字段。"""
    names = {s.target.id for s in _annassigns(
        _classes(_tree(CONTRACT_PY))["SessionResolution"])}
    assert "identity_id" in names and "user_id" not in names
    # 8.2 解析结果 Identity 是 channel-scoped（Identity 域对象带 channel_id）
    from app.domain.identity import Identity
    from app.domain.connection import Connection

    assert "channel_id" in {f.name for f in dataclasses.fields(Identity)}
    assert "channel_id" in {f.name for f in dataclasses.fields(Connection)}
    # 契约决策不携带 channel_id：channel 由 identity/connection 共同锚定
    assert "channel_id" not in names


def test_b_conversation_boundary_kept_in_schema() -> None:
    """sessions 表以 identity_id × external_conversation_id 组织（无 user_id 列）。"""
    from app.persistence import models  # noqa: F401  # 注册全部表

    sessions = Base.metadata.tables["sessions"]
    cols = set(sessions.columns.keys())
    assert "identity_id" in cols and "external_conversation_id" in cols
    assert "user_id" not in cols, "Session 不应直接挂 User（Channel 边界由 identity 保持）"


def test_b_session_read_path_landed_minimally() -> None:
    """9.3：Session **读路径**已落地（实体/Port/Mapper/Adapter），但仍不做写路径。

    CREATE_NEW 只是**决策结果**——会话创建属后续 Slice，因此：
    - 实体 / Port / Mapper / Adapter 存在；
    - **没有** Session Application Service（application/sessions.py）。
    """
    landed = [
        APP_DIR / "domain" / "session.py",
        APP_DIR / "repositories" / "session.py",
        APP_DIR / "persistence" / "repositories" / "session.py",
        APP_DIR / "persistence" / "mappers" / "session.py",
    ]
    missing = [str(p) for p in landed if not p.exists()]
    assert not missing, "Session 读路径未落地：\n" + "\n".join(missing)
    assert not (APP_DIR / "application" / "sessions.py").exists(), \
        "不应有 Session Application Service（会话创建属后续 Slice）"


def test_b_find_active_implemented_exactly_in_port_and_adapter() -> None:
    """9.3：find_active 已实现——且只出现在 Session Port + Adapter 两处。

    隔离键三参数 (identity_id, connection_id, external_conversation_id) 缺一不可
    （external_conversation_id 参与查找，防群聊/会话上下文泄漏）。
    """
    found: list[str] = []
    for py in sorted(APP_DIR.rglob("*.py")):
        tree = _tree(py)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == "find_active":
                found.append("/".join(py.relative_to(BACKEND_DIR).parts))
                args = [a.arg for a in node.args.args if a.arg != "self"]
                assert args == ["identity_id", "connection_id",
                                "external_conversation_id"], \
                    f"{py.name}::find_active 参数漂移：{args}"
    assert sorted(set(found)) == [
        "app/persistence/repositories/session.py",
        "app/repositories/session.py",
    ], f"find_active 应只出现在 Port + Adapter：{sorted(set(found))}"


# ================================================================== C 连接归属裁决


def test_c_connection_is_input_not_selected_here() -> None:
    """Connection 是**输入**（required、必填）且不由本契约选择。

    架构裁决：Session 本身带 connection_id+agent_id，但选哪条 Connection 属于
    Routing（8.4）——本契约不含 agent_id / 候选 / 策略 / 路由字段。
    """
    names = {s.target.id for s in _annassigns(
        _classes(_tree(CONTRACT_PY))["SessionResolution"])}
    assert "connection_id" in names, "缺少 Connection 输入"
    assert not (names & FORBIDDEN_FIELDS), \
        f"契约混入 Connection/Agent 选择意图：{names & FORBIDDEN_FIELDS}"
    # connection_id 必填（无默认值）
    ann = next(s for s in _annassigns(_classes(_tree(CONTRACT_PY))["SessionResolution"])
               if s.target.id == "connection_id")
    assert ann.value is None, "connection_id 应是必填输入"


def test_c_decisions_cover_reuse_and_create() -> None:
    """决策枚举 = 恰好 REUSE_ACTIVE / CREATE_NEW（复用 or 新建，没有第三种模糊态）。"""
    src = CONTRACT_PY.read_text(encoding="utf-8")
    assert "REUSE_ACTIVE = \"REUSE_ACTIVE\"" in src
    assert "CREATE_NEW = \"CREATE_NEW\"" in src
    # docstring 明确 Connection 归属 Routing（8.4），本 Slice 不选 Connection
    assert "Routing" in src and "Connection 不是 Session Resolution 选出来的" in src


# ================================================================== D Fail Closed


def test_d_fail_closed_code_frozen() -> None:
    """渠道错配 / 无确定 Connection 时 Fail Closed：SESSION_RESOLUTION_FAILED。"""
    src = CONTRACT_PY.read_text(encoding="utf-8")
    assert 'SESSION_RESOLUTION_FAILED = "SESSION_RESOLUTION_FAILED"' in src
    assert "Fail Closed" in src


def test_d_all_or_nothing_decision() -> None:
    """决策不是「可空猜测」：必填字段无默认值；existing_session_id 仅随 REUSE 存在。"""
    cls = _classes(_tree(CONTRACT_PY))["SessionResolution"]
    anns = _annassigns(cls)
    by_name = {s.target.id: s for s in anns}
    for field in ("mode", "identity_id", "connection_id", "external_conversation_id"):
        assert by_name[field].value is None, f"{field} 不应有默认值"
    existing = by_name["existing_session_id"]
    assert existing.value is not None, "existing_session_id 默认 None（REUSE 才给值）"
    # 不允许出现任何「猜一个 session」字段（如 fallback_session_id）
    assert "fallback" not in {s.target.id for s in anns}


# ================================================================== E mutation 探测


def test_e_smuggled_session_resolver_is_caught() -> None:
    """变异探测：注入 SessionResolver 偷渡必须报红（白名单精确到文件+类名）。"""
    smuggled = APP_DIR / "domain" / "session_resolver.py"
    assert not smuggled.exists(), "session_resolver.py 不应已存在"
    try:
        smuggled.write_text(
            "class SessionResolver:\n"
            "    def resolve(self) -> None:\n"
            "        pass\n",
            encoding="utf-8",
        )
        # 1) 7.3 反向审计必须报红
        audit_py = BACKEND_DIR / "tests" / "test_step7_3_connection_management_audit.py"
        ns: dict = {"__file__": str(audit_py), "__name__": "audit_under_8_3"}
        exec(compile(audit_py.read_text(encoding="utf-8"), str(audit_py), "exec"), ns)
        problems = ns["_next_stage_problems"]()
        assert any("SessionResolver" in p for p in problems), \
            f"反向审计未抓到 SessionResolver 偷渡：{problems}"

        # 2) interaction/session 命名守卫（8.1 白名单）也会报红
        found = [
            "/".join(smuggled.relative_to(BACKEND_DIR).parts)
            for node in ast.walk(_tree(smuggled))
            if isinstance(node, (ast.ClassDef, ast.FunctionDef))
            and ("session" in node.name.lower() or "interaction" in node.name.lower())
        ]
        assert found, "session 命名守卫未抓到偷渡"
    finally:
        if smuggled.exists():
            smuggled.unlink()
