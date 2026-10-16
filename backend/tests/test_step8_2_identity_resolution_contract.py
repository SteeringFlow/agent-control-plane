"""Step 8.2 —— Identity Resolution Contract & Application Boundary 冻结。

8.2 **不实现 IdentityResolver**（Runtime 组件），只冻结「问题 / 成功结果 /
Fail-Closed 语义 / 与 6.6 Management 的边界」：

    InboundInteraction
          │  channel_id / external_identity_id   ← 最小输入（恰好两个字段）
          ▼
    IdentityResolver（未来 Runtime 组件，本 Slice 不实现）
          │
          ▼
    Identity → User

锁定项：
A  契约本体：`IdentityResolution`（frozen dataclass = identity + user）+
   `IDENTITY_RESOLUTION_FAILED` 失败码；
B  复用 6.6 冻结唯一键：Identity Repository 方法面不增不减，
   唯一查找仍是 `find_by_channel_and_external_id`（不重定义身份唯一性）；
C  Management ≠ Resolution：application 文件面不变（6 个），identity 管理
   Service 方法面不变（create/get/update，无 resolve_identity）；
D  Fail Closed：成功结果 all-or-nothing（无 None/无默认 user），
   全仓无任何"猜一个 User"的能力（无 fuzzy/guess/match/link 等）；
E  mutation 探测：注入 `IdentityResolver` 偷渡必须报红（契约不是开后门）。
"""

import ast
import dataclasses
from pathlib import Path

import pytest

from app.persistence.database import Base

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"
CONTRACT_PY = APP_DIR / "domain" / "identity_resolution.py"

EXPECTED_IDENTITY_PORT_METHODS = {
    "get_by_id", "create", "update", "find_by_channel_and_external_id",
    "list_identities",  # 12.4：Identity Management API 的 List 用例
    # M6.6-C3 / ADR-12 §11：Bootstrap 事务内的幂等建立（冲突以 SAVEPOINT 消化后回读
    # 复用）；它**复用**既有行，不是新的「自动开户」路径。
    "find_or_create",
}
# Management 与 Resolution 的边界：application 文件面 = 6 个管理文件
# + runtime.py（9.1 Runtime Execution Boundary）
# + identity_resolution.py（9.2 Identity Resolution Execution，非 Resolver）
# + session_resolution.py（9.3 Session Resolution Execution，非 Resolver）
EXPECTED_APPLICATION_FILES = [
    "access_authorization",
    "agent_dispatch",
    # M7-AUI-10.5：WorkBuddy External Agent Identity 语义/查重（非 Service）
    "agent_identity",
    "agents",
    # M6.6-C3 / ADR-12：Bootstrap 组件（执行边界上的独立组件，非 Runtime 阶段）。
    "bootstrap",
    # M6.6-C3 / ADR-12：Bootstrap Arm 管理面用例（arm / supersede / 读）。
    "bootstrap_arms",
    "browser_auth", "channel_inbound", "channels",
    "connection_conversations", "connections",
    "coordinator", "delivery", "driver", "errors", "identities",
    "identity_resolution",
    "interaction_observability", "interaction_recording",
    "routing", "runtime", "session_creation", "session_resolution",
    "tenant_fanout", "tenant_provisioning", "users", "worker",
]  # Package 2 / Step 2: + access_authorization（Access Authorization Execution）
# 10.4.2: + interaction_recording（Interaction 持久化 Wrapper）
# **边界不变**：新增文件是 Execution 执行器（AccessAuthorizationExecutor），
# 不是 Resolver / Policy / 权限体系——本守卫的语义（Management ≠ Resolution）
# 一字未放。
# Cloud-0.1.1-C: + browser_auth（Browser Auth Application Service）
# Cloud-0.1.2.1: + tenant_provisioning（TenantProvisioner 实现）
# 12.9.4.5: + worker（单次执行 primitive，非后台服务）
# 12.9.4.16: + driver（最小 Production Driver / trigger）
# 13.1: + interaction_observability（Observability 只读面）
# Fail-Closed：禁止任何「猜一个 User」或提前 Resolver 的能力令牌
GUESSING_TOKENS = ("fuzzy", "guess", "fallback", "guest",
                   "link", "merge", "match", "dedup")
RESOLVER_TOKEN = "resolve"


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


def _methods(cls: ast.ClassDef) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in cls.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


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


def _identity_port_methods() -> set[str]:
    tree = _tree(APP_DIR / "repositories" / "identity.py")
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name != "__init__"}


# ================================================================== A 契约本体


def test_a_contract_module_boundaries() -> None:
    """契约模块只依赖 dataclasses + 兄弟 domain 实体，绝不碰任何外层/基础设施。"""
    assert CONTRACT_PY.is_file(), "缺 app/domain/identity_resolution.py"
    roots = _roots(_tree(CONTRACT_PY))
    forbidden = ("sqlalchemy", "fastapi", "starlette", "pydantic",
                 "app.repositories", "app.persistence", "app.application",
                 "app.api", "app.schemas", "app.adapters")
    leaked = [r for r in roots if any(r == f or r.startswith(f + ".")
                                      for f in forbidden)]
    assert not leaked, f"契约模块越层导入：{leaked}"
    assert "dataclasses" in roots
    assert "app.domain.identity" in roots and "app.domain.user" in roots


def _annotation_atoms(annotation: ast.expr) -> list[str]:
    """拆注解原子名：`User` → ["User"]；`User | None` → ["User", "None"]。"""
    if isinstance(annotation, ast.Name):
        return [annotation.id]
    if isinstance(annotation, ast.Constant):  # `None` 在 AST 里是 Constant
        return [str(annotation.value)]
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_atoms(annotation.left) + _annotation_atoms(annotation.right)
    raise AssertionError(f"不支持的注解形状：{ast.dump(annotation)}")


def test_a_resolution_outcome_shape() -> None:
    """IdentityResolution = frozen dataclass，字段恰好 (identity, user)。

    Implementation Package 1 / Gate 2.6 D3：`user` 放宽为 **`User | None`**
    （未绑定 ACP User 是合法成功结果）。本守卫同步放宽**注解形状**，
    但**字段名 / 顺序 / all-or-nothing 纪律一律不变**——
    放宽的是可空性，不是「结果可以半成品」。
    """
    cls = _classes(_tree(CONTRACT_PY))["IdentityResolution"]
    assert _is_frozen_dataclass(cls), "IdentityResolution 应为 frozen dataclass"
    anns = _annassigns(cls)
    names = [s.target.id for s in anns]
    assert names == ["identity", "user"], f"结果形状漂移：{names}"
    assert _annotation_atoms(anns[0].annotation) == ["Identity"], \
        "identity 仍是不可选的 Identity"
    assert sorted(_annotation_atoms(anns[1].annotation)) == ["None", "User"], \
        "user 应为 User | None（Gate 2.6 D3）"
    for stmt in anns:
        assert stmt.value is None, f"{stmt.target.id} 不应有默认值（all-or-nothing）"


def test_a_failure_code_frozen() -> None:
    """Fail-Closed 失败码 IDENTITY_RESOLUTION_FAILED 已冻结。"""
    from app.domain.identity_resolution import IDENTITY_RESOLUTION_FAILED
    assert IDENTITY_RESOLUTION_FAILED == "IDENTITY_RESOLUTION_FAILED"
    src = CONTRACT_PY.read_text(encoding="utf-8")
    assert 'IDENTITY_RESOLUTION_FAILED = "IDENTITY_RESOLUTION_FAILED"' in src


def test_a_contract_behavior() -> None:
    """运行时行为：成功结果 = identity + user 一起出现；不可变。"""
    from app.domain.identity import Identity
    from app.domain.identity_resolution import IdentityResolution
    from app.domain.user import User

    identity = Identity(user_id=1, channel_id=2, external_id="wx_zhangsan")
    user = User(name="zhangsan", id=1)
    res = IdentityResolution(identity=identity, user=user)

    assert res.identity is identity
    assert res.user is user
    assert res.identity.user_id == 1
    assert res.identity.external_id == "wx_zhangsan"

    with pytest.raises(dataclasses.FrozenInstanceError):
        res.identity = identity  # type: ignore[misc]

    # Gate 2.6 D3 / Implementation Package 1：**未绑定 ACP User 的 Identity**
    # 同样产生合法成功结果（user=None），不是匿名 / 访客 / 系统用户。
    unbound = Identity(user_id=None, channel_id=2, external_id="wx_lisi")
    res_unbound = IdentityResolution(identity=unbound, user=None)

    assert res_unbound.user is None
    assert res_unbound.identity.user_id is None
    assert res_unbound.identity.external_id == "wx_lisi"


def test_a_domain_package_exports() -> None:
    """domain 包导出 IdentityResolution（与 InboundInteraction 同待遇）。"""
    from app.domain import IdentityResolution as _E1
    from app.domain.identity_resolution import IdentityResolution as _E2

    assert _E1 is _E2


# ================================================================== B 复用 6.6 冻结唯一键


def test_b_identity_port_face_unchanged() -> None:
    """Identity Repository 方法面不增不减——Runtime 复用，不重定义唯一性。"""
    assert _identity_port_methods() == EXPECTED_IDENTITY_PORT_METHODS, \
        f"Identity Port 方法面漂移：{_identity_port_methods()}"


def test_b_lookup_key_matches_db_unique_constraint() -> None:
    """解析查找键 = DB 冻结的 UNIQUE(channel_id, external_id)。"""
    from app.persistence import models  # noqa: F401  # 注册全部表

    identities = Base.metadata.tables["identities"]
    assert any(u.name == "uq_identities_tenant_channel_external_id"
               for u in identities.constraints), "6.6 唯一键漂移"
    # Runtime 契约不引入第二个唯一键（Cloud-0.1.1-B：唯一键仅前导 tenant_id，
    # 仍只有 (tenant_id, channel_id, external_id) 这一个，不新增第二个键）
    unique_names = sorted(u.name for u in identities.constraints
                          if isinstance(u, __import__("sqlalchemy").UniqueConstraint))
    assert unique_names == ["uq_identities_tenant_channel_external_id"]


def test_b_minimal_input_comes_from_inbound_interaction() -> None:
    """最小输入 (channel_id, external_identity_id) = InboundInteraction 的两个字段。"""
    from dataclasses import fields

    from app.domain.inbound_interaction import InboundInteraction

    names = {f.name for f in fields(InboundInteraction)}
    assert {"channel_id", "external_identity_id"} <= names
    # 契约 docstring 明确只吃这两个字段（不出现额外查询意图字段）
    src = CONTRACT_PY.read_text(encoding="utf-8")
    assert "channel_id" in src and "external_identity_id" in src
    assert "find_by_channel_and_external_id" in src


# ================================================================== C Management ≠ Resolution


def test_c_application_boundary_unchanged() -> None:
    """Application 文件面不变：没有新的 Identity 解析 Service / 文件混入。"""
    app_files = sorted(p.stem for p in (APP_DIR / "application").glob("*.py")
                       if p.stem != "__init__")
    assert app_files == EXPECTED_APPLICATION_FILES, \
        f"application 文件面漂移：{app_files}"


def test_c_identity_management_service_has_no_resolution_method() -> None:
    """6.6 Identity Management Service 仍是纯管理：create/get/list/update，无 resolve。

    12.4：+ list_identities（Identity Management API 的 List 用例，
    与 11.3 list_agents 同一演进先例）。
    """
    tree = _tree(APP_DIR / "application" / "identities.py")
    classes = _classes(tree)
    assert "IdentityApplicationService" in classes
    use_cases = {m for m in _methods(classes["IdentityApplicationService"])
                 if not m.startswith("_")}
    assert use_cases == {"create_identity", "get_identity", "list_identities",
                         "update_identity"}, \
        f"Identity 管理 Service 用例面漂移：{use_cases}"
    assert not any("resolv" in m.lower() for m in use_cases), \
        "管理 Service 不应出现解析方法（职责污染）"


# ================================================================== D Fail Closed


def test_d_no_guessing_or_partial_resolution_capability() -> None:
    """全仓（models 除外）无 fuzzy/guess/match/link/merge 等『猜 User』能力。"""
    problems: list[str] = []
    for py in sorted(APP_DIR.rglob("*.py")):
        if (APP_DIR / "persistence" / "models") in py.parents:
            continue
        tree = _tree(py)
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                lower = node.name.lower()
                if any(tok in lower for tok in GUESSING_TOKENS):
                    problems.append(f"{py.relative_to(BACKEND_DIR)}::{node.name}")
    assert not problems, "出现『猜 User / linking』能力：\n" + "\n".join(problems)


def test_d_no_resolver_implementation_yet() -> None:
    """IdentityResolver 尚未实现：全仓无含 'resolve' 的方法/类名。

    Cloud-0.1.1-B 例外（精确白名单）：`app/core/tenant.py::TenantResolver` 是
    入口租户解析**端口**（Protocol，非 Runtime 决策实现），类名含 'resolve' 属
    ingress 边界契约，不在本守卫范围内（见实现边界 §12）。
    """
    problems: list[str] = []
    TENANT_INGRESS_ALLOWLIST = {("app/core/tenant.py", "TenantResolver"),
                                ("app/core/tenant.py", "resolve")}
    for py in sorted(APP_DIR.rglob("*.py")):
        if (APP_DIR / "persistence" / "models") in py.parents:
            continue
        rel = "/".join(py.relative_to(BACKEND_DIR).parts)
        tree = _tree(py)
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if (rel, node.name) in TENANT_INGRESS_ALLOWLIST:
                    continue
                if RESOLVER_TOKEN in node.name.lower():
                    problems.append(f"{py.relative_to(BACKEND_DIR)}::{node.name}")
    assert not problems, "Resolver 已提前实现：\n" + "\n".join(problems)


def test_d_user_port_has_no_matching_finders() -> None:
    """User Repository 无 email/phone/name 查找（不能拿它们去猜 User）。"""
    tree = _tree(APP_DIR / "repositories" / "user.py")
    methods = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert not any("email" in m or "phone" in m for m in methods), \
        f"User Port 出现匹配抓手：{methods}"


# ================================================================== E mutation 探测


def test_e_smuggled_identity_resolver_is_caught() -> None:
    """变异探测：注入 IdentityResolver 偷渡必须报红（契约白名单不是开后门）。"""
    smuggled = APP_DIR / "domain" / "identity_resolver.py"
    assert not smuggled.exists(), "identity_resolver.py 不应已存在"
    try:
        smuggled.write_text(
            "class IdentityResolver:\n"
            "    def resolve_identity(self) -> None:\n"
            "        pass\n",
            encoding="utf-8",
        )
        # 1) 8.1/7.3 反向审计必须报红（resolver token）
        audit_py = BACKEND_DIR / "tests" / "test_step7_3_connection_management_audit.py"
        ns: dict = {"__file__": str(audit_py), "__name__": "audit_under_8_2"}
        exec(compile(audit_py.read_text(encoding="utf-8"), str(audit_py), "exec"), ns)
        problems = ns["_next_stage_problems"]()
        assert any("IdentityResolver" in p for p in problems), \
            f"反向审计未抓到 IdentityResolver 偷渡：{problems}"

        # 2) 本文件 D 组守卫必须报红（resolve 命名）
        found = [
            "/".join(smuggled.relative_to(BACKEND_DIR).parts)
            for node in ast.walk(_tree(smuggled))
            if isinstance(node, (ast.ClassDef, ast.FunctionDef))
            and RESOLVER_TOKEN in node.name.lower()
        ]
        assert found, "本地 resolve 命名守卫未抓到偷渡"
    finally:
        if smuggled.exists():
            smuggled.unlink()
