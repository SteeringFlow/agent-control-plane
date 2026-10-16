"""Cloud-0.1.2 Workspace & Golden Path —— Architecture Gate（静态护栏）。

中心约束（见 docs/step-cloud-0.1.2-workspace-golden-path-gate.md）：

  Browser
    ↓  Browser Authentication (C, L1+L2) → AuthenticatedUser(user_id, tenant_id)
    ↓  TenantScope(tenant_id)            ← 复用 B，唯一 tenant 原语
    ↓  Workspace (UX only)
    ↓  tenant-scoped Resources (Agent / Channel / Connection)
    ↓  Runtime → Interaction → First Success

本 Gate 不是功能测试，而是**架构护栏**：机器化锁死 0.1.2 的红线——

  G1  Workspace ≡ Tenant（无第二套 Workspace ownership model）
  G2  Sign-up Bootstrap（User → Tenant → Session → AuthenticatedUser）
  G3  TenantScope Continuity（AuthenticatedUser.tenant_id → TenantScope → 既有 UoW）
  G4  Agent/Channel/Connection 复用（无 Cloud 专属实体）
  G5  WorkBuddy=Agent / WeCom=Channel / Connection=binding（无 Integration 抽象）
  G6  First Success 可推导（无新持久化模型）
  G7  Architecture Pollution 锁死（no workspace/org/team/membership/auth-engine/
      cloud-runtime-fork/second-tenant-abstraction）

当前即可绿灯；进入实现期时本文件保持绿，并按 Gate §10 追加「实现后不变量」。
"""

import ast
import dataclasses
import io
import inspect
import tokenize
from pathlib import Path

from app.application.browser_auth import BrowserAuthService
from app.core.browser_auth import AuthenticatedUser
from app.core.tenant import TENANT_LOCAL, TenantScope, default_tenant_scope
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.user import User

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"


# ---------------------------------------------------------------- helpers


def _app_sources() -> list[Path]:
    out: list[Path] = []
    for py in APP.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        out.append(py)
    return out


def _code_without_prose(path: Path) -> str:
    """只返回真实代码标识符，剥离注释与字符串字面量。

    关键：docstring/注释里大量出现『不引入 Organization / 不是 Integration』之类的
    拒斥性表述——这些恰恰是 Gate 纪律的证据，不应触发护栏。只有作为**实际代码
    标识符**出现的越界符号才应被拦截。
    """
    src = path.read_text(encoding="utf-8")
    try:
        toks = tokenize.generate_tokens(io.StringIO(src).readline)
    except Exception:
        return src  # 解析失败则回退为原文（宁可误报而非漏报）
    names = [tok.string for tok in toks if tok.type == tokenize.NAME]
    return " ".join(names)


def _all_code_text() -> str:
    return "\n".join(_code_without_prose(p) for p in _app_sources())


def _code_contains_word(word: str) -> bool:
    """在剥离 prose 后的真实代码标识符中做整词匹配（大小写不敏感）。"""
    text = _all_code_text().lower()
    return f" {word.lower()} " in f" {text} "


def _class_names() -> set[str]:
    """收集 app 下所有顶层 class 名（用于类级护栏）。"""
    names: set[str] = set()
    for p in _app_sources():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
    return names


def _table_names() -> set[str]:
    from app.persistence import models as _models_pkg  # noqa: F401  注册表到 Base.metadata
    from app.persistence.database import Base

    return set(Base.metadata.tables.keys())


# ---------------------------------------------------------------- G1 Workspace ≡ Tenant


def test_no_workspace_domain_entity_or_table() -> None:
    """G1：禁止 Workspace 成为第二套 ownership model。

    - 无 Workspace / Organization / Team / Membership 域类；
    - 无 workspaces / workspace_members / organizations / teams / memberships 表。
    """
    classes = _class_names()
    for forbidden_class in ("Workspace", "Organization", "Team", "Membership"):
        assert forbidden_class not in classes, f"禁止引入归属实体：{forbidden_class}"

    tables = _table_names()
    for forbidden_table in (
        "workspaces", "workspace_members", "organizations",
        "teams", "memberships", "workspace_tenants",
    ):
        assert forbidden_table not in tables, f"禁止引入表 {forbidden_table}"


def test_single_tenant_primitive_only() -> None:
    """G1：tenant 身份原语唯一 = TenantScope；无 browser_tenant / workspace_tenant 第二套。"""
    assert [f.name for f in dataclasses.fields(TenantScope)] == ["tenant_id"]
    assert not _code_contains_word("browsertenant")
    assert not _code_contains_word("browser_tenant")
    assert not _code_contains_word("workspacetenant")
    assert not _code_contains_word("workspace_tenant")


# ---------------------------------------------------------------- G2 Sign-up Bootstrap


def test_authenticated_user_carries_tenant_id() -> None:
    """G2：L2 输出 AuthenticatedUser 恰为 {user_id, tenant_id}（tenant 是唯一归属维度）。"""
    fields = {f.name for f in dataclasses.fields(AuthenticatedUser)}
    assert fields == {"user_id", "tenant_id"}, (
        f"AuthenticatedUser 不应含 workspace 字段：{fields}"
    )


def test_register_binds_to_tenant_not_workspace() -> None:
    """G2：User 域无 workspace_id；register 绑定 tenant，不创建第二套归属。"""
    user_fields = {f.name for f in dataclasses.fields(User)}
    assert "workspace_id" not in user_fields, "User 不应有 workspace_id（tenant 已是归属根）"
    # BrowserAuthService 必须提供 register/login/verify 三段闭环。
    for method in ("register", "login", "authenticate_session", "verify"):
        assert hasattr(BrowserAuthService, method), f"BrowserAuthService 缺少 {method}"


def test_tenant_provisioner_is_only_tenant_creator() -> None:
    """G2：唯一被允许写 tenants 的入口是 TenantProvisioner（bootstrap 端口）；
    不引入 WorkspaceProvisioner / OrganizationProvisioner 第二套归属创建器。"""
    from app.core.tenant import TenantProvisioner

    assert TenantProvisioner is not None
    assert not _code_contains_word("workspaceprovisioner")
    assert not _code_contains_word("organizationprovisioner")


# ---------------------------------------------------------------- G3 TenantScope Continuity


def test_no_implicit_global_tenant_context() -> None:
    """G3（B 红线延续）：无隐式全局 tenant 上下文。"""
    for sym in ("current_tenant", "global_tenant", "set_current_tenant", "get_current_tenant"):
        assert not _code_contains_word(sym), f"禁止隐式全局 tenant 上下文：{sym}"


def test_auth_flow_reuses_tenant_scope_not_second_injection() -> None:
    """G3：AuthenticatedUser.tenant_id → TenantScope（B 唯一原语）→ 既有 tenant-scoped UoW。
    api/auth.py 的 tenant 解析依赖产出 TenantScope，而非新建 tenant 解析机制。"""
    from app.api.auth import build_tenant_scope_provider

    assert not _code_contains_word("browsertenantscope")
    assert not _code_contains_word("workspacescope")
    assert callable(build_tenant_scope_provider)
    sig = inspect.signature(build_tenant_scope_provider)
    assert "auth_required" in sig.parameters, (
        "tenant 解析依赖应保留 auth_required 开关（OSS 回退）"
    )


# ---------------------------------------------------------------- G4 Agent/Channel/Connection


def test_core_three_entities_exist_and_reused() -> None:
    """G4：Cloud onboarding 复用 Core 三实体（Agent/Channel/Connection），无 Cloud 专属实体。"""
    assert {"name", "type"}.issubset({f.name for f in dataclasses.fields(Agent)})
    assert {"name", "type"}.issubset({f.name for f in dataclasses.fields(Channel)})
    assert {"channel_id", "agent_id"}.issubset(
        {f.name for f in dataclasses.fields(Connection)}
    )


def test_no_cloud_specific_resource_entities() -> None:
    """G4：禁止 CloudAgent / CloudChannel / CloudConnection / CloudResource / CloudIntegration
    第二套业务模型。"""
    classes = _class_names()
    for forbidden in (
        "CloudAgent", "CloudChannel", "CloudConnection", "CloudResource", "CloudIntegration"
    ):
        assert forbidden not in classes, f"禁止 Cloud 专属实体：{forbidden}"


# ---------------------------------------------------------------- G5 WorkBuddy / WeCom


def test_no_integration_abstraction() -> None:
    """G5：Golden Path = WorkBuddy(Agent) / WeCom(Channel) / Connection(binding)；
    禁止 Integration / Connector / IntegrationModel 抽象层。"""
    classes = _class_names()
    for forbidden in ("Integration", "Connector", "IntegrationModel", "IntegrationAbstraction"):
        assert forbidden not in classes, f"禁止 Integration 抽象：{forbidden}"
    # Connection 域已是 Channel↔Agent 绑定，无额外 Integration 实体。
    assert {"channel_id", "agent_id"}.issubset(
        {f.name for f in dataclasses.fields(Connection)}
    )


def test_no_second_channel_or_agent_fork_for_wecom_workbuddy() -> None:
    """G5：WeCom / WorkBuddy 落在既有 Channel / Agent（type 维度），不新增 Cloud 专属类。"""
    assert not _code_contains_word("cloudwecom")
    assert not _code_contains_word("cloudworkbuddy")
    assert not _code_contains_word("wecomintegration")
    assert not _code_contains_word("workbuddyintegration")


# ---------------------------------------------------------------- G6 First Success


def test_first_success_is_derived_not_persisted() -> None:
    """G6：First Success 是派生状态，非持久化模型。
    禁止 first_successes / onboarding_states / activation_events / user_journeys 表或域类。"""
    classes = _class_names()
    for forbidden in ("FirstSuccess", "OnboardingState", "ActivationEvent", "UserJourney"):
        assert forbidden not in classes, f"禁止 First Success 持久化模型：{forbidden}"
    tables = _table_names()
    for forbidden_table in (
        "first_successes", "onboarding_states", "activation_events", "user_journeys"
    ):
        assert forbidden_table not in tables, f"禁止引入表 {forbidden_table}"


# ---------------------------------------------------------------- G7 Architecture Pollution


def test_no_workspace_or_second_tenant_abstraction() -> None:
    """G7：no workspace table / no second tenant abstraction（与 G1 互补，显式锁死）。"""
    assert not _code_contains_word("workspace")
    assert not _code_contains_word("workspacedb")
    # tenant 是唯一的 ownership / isolation root（基线正断言）。
    assert _code_contains_word("tenant")


def test_no_organization_team_membership_authorization_engine() -> None:
    """G7：no organization / membership / authorization engine / rbac / scim / oauth / sso / jwt。"""
    forbidden = [
        "organization", "organisation",
        "authorizationengine", "authz",
        "rbac",
        "scim",
        "oauth", "sso",
        "jwt", "jwks", "refresh_token",
    ]
    hits = [tok for tok in forbidden if _code_contains_word(tok)]
    assert not hits, f"backend 出现越界架构标识符：{hits}"


def test_no_cloud_runtime_fork() -> None:
    """G7：no cloud runtime fork——Runtime 仍经既有 Core 执行，不另造 Cloud Runtime。"""
    assert not _code_contains_word("cloudruntime")
    assert not _code_contains_word("cloudworker")
    assert not _code_contains_word("clouddriver")
    # Runtime 面 main.py 仍绑定 TENANT_LOCAL（C 冻结），未为 Cloud 另造一套。
    assert TENANT_LOCAL == 1
    assert default_tenant_scope().tenant_id == TENANT_LOCAL


def test_workspace_is_ux_abstraction_only() -> None:
    """G7 收口：Workspace 仅作为 UX 抽象，不进入 backend 域/持久层。
    全 backend 真实代码标识符中不得出现 `workspace`（仅 docstring 里的『Workspace/Tenant』
    表述，已被 _code_without_prose 剥离，不触发）。"""
    text = _all_code_text().lower()
    assert " workspace " not in f" {text} "
