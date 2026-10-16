"""ADR — Authorization & Access Model（机器化护栏）.

只读审计 + 机器锁定；**不启动 DB、不做实现**。

本护栏锁定 `docs/architecture/authorization-and-access-model.md`（FROZEN）中的
ADR-1 ~ ADR-10：

    ADR-1  ACP User ≠ External Identity（渠道参与者身份不要求拥有管理账号）
    ADR-2  Connection 是 Agent Access Boundary（不重定义 Agent 权限体系）
    ADR-3  Authorization 的 MVP 作用域 = Conversation（G1 → C1 = ALLOW）
    ADR-4  Channel Native Authorization 不由 ACP 重建（不建组织镜像）
    ADR-5  Authorization 不自动创建 ACP User（Resolve → Access Check，不 Create）
    ADR-6  Discovery ≠ Authorization（收到消息 ≠ 允许处理）
    ADR-7  Authorization 位于 Session Resolution 之前（但 Interaction 仍记录）
    ADR-8  Authorization 先于最终 Routing（且不顺便开发 Multi-Agent Routing）
    ADR-9  V0 不做复杂权限系统（RBAC/ABAC/Policy Engine/... 全部 NON-GOAL）
    ADR-10 UX 约束：Simple by Default. Powerful When Needed.

与既有 Gate 护栏同一纪律：AST 扫描**真实标识符**（类名 / 字段名 / 方法名 / 调用名 /
表名），避免拒斥性 docstring 产生假阳性（例如 `core/browser_auth.py` 明确写着
「不做 OAuth Platform / SSO / RBAC / Organization」，那是纪律声明而非实现）。

本文件**不修改任何生产代码**——它是纯审计守卫。
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.domain.connection import Connection
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.user import User
from app.persistence.models.connection import ConnectionModel
from app.persistence.models.identity import IdentityModel
from app.persistence.models.session import SessionModel

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_APP = _BACKEND_ROOT / "app"
_REPO_ROOT = _BACKEND_ROOT.parent
_ADR_DOC = _REPO_ROOT / "docs" / "architecture" / "authorization-and-access-model.md"


# --------------------------------------------------------------------- helpers

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _app_files() -> list[Path]:
    return sorted(_BACKEND_APP.rglob("*.py"))


def _app_class_names() -> frozenset[str]:
    names: set[str] = set()
    for path in _app_files():
        try:
            tree = ast.parse(_read(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
    return frozenset(names)


def _app_table_names() -> frozenset[str]:
    """扫 persistence/models 的 __tablename__（禁止新增权限/组织表）。"""
    tables: set[str] = set()
    for path in (_BACKEND_APP / "persistence" / "models").rglob("*.py"):
        try:
            tree = ast.parse(_read(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == "__tablename__"
                for t in node.targets
            ):
                continue
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                tables.add(node.value.value)
    return frozenset(tables)


def _identifiers(path: Path) -> set[str]:
    """模块内所有 Name / Attribute 标识符（docstring 字符串不计入）。"""
    found: set[str] = set()
    tree = ast.parse(_read(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def _call_names(path: Path) -> set[str]:
    found: set[str] = set()
    tree = ast.parse(_read(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            found.add(func.id)
        elif isinstance(func, ast.Attribute):
            found.add(func.attr)
    return found


def _class_method_names(path: Path, class_name: str) -> list[str]:
    tree = ast.parse(_read(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return [
                b.name for b in node.body if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
    return []


def _constraint_names(model) -> frozenset[str]:  # noqa: ANN001 - SQLAlchemy model
    return frozenset(c.name for c in model.__table__.constraints if c.name)


# ============================================================ ADP-0（文档成文）

_ADR_MARKERS = (
    "# ADR — Authorization & Access Model",
    "状态：**FROZEN**",
    "ADR-1：ACP User ≠ External Identity",
    "ADR-2：Connection 是 Agent Access Boundary",
    "ADR-3：Authorization 的 MVP 作用域",
    "ADR-4：Channel Native Authorization 不由 ACP 重建",
    "ADR-5：Authorization 不自动创建 ACP User",
    "ADR-6：Discovery ≠ Authorization",
    "ADR-7：Authorization 位于 Session Resolution 之前",
    "ADR-8：Authorization 先于最终 Routing",
    "ADR-9：V0 不做复杂权限系统",
    "Simple by Default. Powerful When Needed.",
    "Progressive Disclosure",
    "Discovery ≠ Authorization",
    "未授权请求不应该进入正常 Agent Session",
    "不应该顺便开发 Multi-Agent Routing",
    "External Identity 不要求拥有 ACP User",
    "Conversation 是 MVP 默认的 Authorization Scope",
    "NON-GOAL",
    "backend/tests/test_adr_authorization_and_access_model.py",
)


def test_adr_document_is_frozen_and_complete() -> None:
    """ADR-0：裁决文档存在，且 ADR-1 ~ ADR-10 与 UX 约束成文（防漂移）。"""
    assert _ADR_DOC.exists(), f"ADR 文档缺失：{_ADR_DOC}"
    text = _read(_ADR_DOC)
    missing = [m for m in _ADR_MARKERS if m not in text]
    assert not missing, f"ADR 文档缺少冻结标记：{missing}"


def test_adr_document_is_doc_only_not_implementation() -> None:
    """ADR-0：本文件是裁决，不是实现步——不得宣称已实现授权能力。"""
    text = _read(_ADR_DOC)
    assert "仅裁决，不实现" in text
    assert "❌ RBAC" in text and "❌ ABAC" in text and "❌ Policy Engine" in text


# ==================================================================== ADR-1

def test_adr1_user_and_external_identity_are_separate_entities() -> None:
    """ADR-1：ACP User（管理账号）与 External Identity（渠道参与者）是两个实体。"""
    user_fields = set(User.__dataclass_fields__)
    identity_fields = set(Identity.__dataclass_fields__)

    # External Identity 携带渠道侧标识（channel_id + external_id 唯一键）
    assert {"channel_id", "external_id"} <= identity_fields
    # 管理账号不承载渠道身份字段
    assert not ({"channel_id", "external_id", "external_identity_id"} & user_fields)
    # 二者不是同一个类
    assert User is not Identity


def test_adr1_identity_unique_key_is_channel_scoped() -> None:
    """ADR-1：外部身份在 Channel Scope 内解释（uq_identities_tenant_channel_external_id）。"""
    assert "uq_identities_tenant_channel_external_id" in _constraint_names(IdentityModel)


# ==================================================================== ADR-2

_AUTH_FIELD_FRAGMENTS = (
    "permission", "permissions", "role", "roles", "policy", "policies",
    "principal", "scope", "effect", "acl", "authoriz", "authorization",
    "allowed", "grant", "capability",
)


def test_adr2_connection_did_not_become_an_authorization_object() -> None:
    """ADR-2：Connection 仍是 Access Path，没有长成授权对象。"""
    fields = set(Connection.__dataclass_fields__)
    offending = [
        f for f in fields
        if any(fragment in f for fragment in _AUTH_FIELD_FRAGMENTS)
    ]
    assert not offending, f"Connection 域新增了授权字段：{offending}"

    methods = _class_method_names(_BACKEND_APP / "domain" / "connection.py", "Connection")
    bad_methods = [
        m for m in methods
        if any(fragment in m.lower() for fragment in _AUTH_FIELD_FRAGMENTS)
    ]
    assert not bad_methods, f"Connection 域新增了授权方法：{bad_methods}"


def test_adr2_channel_connection_agent_binding_is_unchanged() -> None:
    """ADR-2：Channel 1:N Connection N:1 Agent 的绑定约束仍在（无第二套绑定实体）。"""
    assert "uq_connections_tenant_channel_agent_id" in _constraint_names(ConnectionModel)
    cols = {c.name for c in ConnectionModel.__table__.columns}
    assert {"channel_id", "agent_id"} <= cols


# ==================================================================== ADR-3

def test_adr3_conversation_is_the_authorization_scope_key() -> None:
    """ADR-3：授权作用域键 external_conversation_id 在入站契约与 sessions 均为一等字段。"""
    assert "external_conversation_id" in InboundInteraction.__dataclass_fields__
    session_cols = {c.name for c in SessionModel.__table__.columns}
    assert "external_conversation_id" in session_cols
    assert "uq_sessions_tenant_identity_conn_conv" in _constraint_names(SessionModel)


def test_adr3_group_chat_conversation_key_is_still_mapped() -> None:
    """ADR-3：群聊会话键映射仍在（`chatid`）——「G1 → C1 = ALLOW」的承载前提。"""
    src = _read(_BACKEND_APP / "adapters" / "channels" / "wecom_bot.py")
    assert '_CHAT_TYPE_GROUP = "group"' in src
    assert 'body.get("chatid")' in src
    assert "external_conversation_id=conversation" in src


# ==================================================================== ADR-4

# 组织镜像 / 渠道原生授权（类名精确匹配；AST 已排除 docstring 误报）。
_FORBIDDEN_ORG_CLASSES = frozenset({
    "WeComDepartment", "WeComGroup", "WeComRole", "WeComPermission",
    "SlackGroup", "SlackTeam", "SlackRole",
    "FeishuDepartment", "FeishuGroup", "FeishuRole",
    "DingTalkDepartment", "DingTalkGroup",
    "Department", "Group", "OrgUnit", "OrganizationUnit", "Organization",
    "ChannelGroup", "ChannelDepartment", "ChannelRole", "ChannelMember",
    "GroupMember", "DepartmentMember", "WorkspaceMember", "TenantMember",
    "MemberRoster", "OrgTree",
})

_FORBIDDEN_ORG_TABLES = frozenset({
    "departments", "organizations", "org_units", "groups", "group_members",
    "channel_groups", "channel_members", "department_members", "org_trees",
})


def test_adr4_channel_native_authorization_is_not_rebuilt() -> None:
    """ADR-4：ACP 不创建 / 不复制渠道的组织权限模型。"""
    classes = _app_class_names()
    assert not (classes & _FORBIDDEN_ORG_CLASSES), (
        f"出现了渠道组织镜像实体：{sorted(classes & _FORBIDDEN_ORG_CLASSES)}"
    )
    tables = _app_table_names()
    assert not (tables & _FORBIDDEN_ORG_TABLES), (
        f"出现了渠道组织镜像表：{sorted(tables & _FORBIDDEN_ORG_TABLES)}"
    )


# ==================================================================== ADR-5

# 运行时链路：任何「自动开户 / 自动绑定」的标识符都禁止出现。
_PROVISIONING_IDENTIFIERS = frozenset({
    "create_user", "create_users", "create_identity", "create_identities",
    "ensure_user", "ensure_identity", "upsert_user", "upsert_identity",
    "provision_user", "provision_identity", "auto_provision",
    "UserApplicationService", "IdentityApplicationService",
    "IdentityLinker", "IdentityMatcher", "IdentityResolver", "IdentityMerger",
})

_RUNTIME_CHAIN_FILES = (
    _BACKEND_APP / "application" / "identity_resolution.py",
    _BACKEND_APP / "application" / "routing.py",
    _BACKEND_APP / "application" / "coordinator.py",
    _BACKEND_APP / "application" / "runtime.py",
)


#: M6.6-C3 / ADR-12 §11（E-1）—— **G-D 语义升级**。
#:
#: 原形态「扫描固定 4 个 runtime 文件」只能证明「这 4 个文件里没有自动开户」，
#: **不能**证明「整个 runtime 只有被允许的位置能创建 Identity」：
#: 把创建放进清单外的 `bootstrap.py` 会**通过守卫而实质违反 ADR-5**
#: （CP-1 §9.4 已点名的守卫规避）。
#:
#: 因此改为**全仓扫描 + 显式允许清单**：清单外出现任何创建调用即失败。
#: 形态复用既有先例 `test_package_2_step_4_…::test_g9_runtime_path_never_auto_creates_grants`
#: （它已经是「全仓扫 `connection_conversations.create` → creators == [...]」）。
#:
#: 允许清单**恰好**两个入口；新增第三个运行时创建点必须回到 ADR Gate，
#: 不得就地扩充白名单。
_IDENTITY_CREATION_FILES = frozenset({
    "identities.py",        # 管理面 IdentityApplicationService（6.6，既有）
    "bootstrap.py",         # M6.6-C3 / ADR-12：唯一的 Bootstrap 建立点
})
_GRANT_CREATION_FILES = frozenset({
    "connection_conversations.py",   # 管理面 Grant Management（Package 2 Step 3）
    "bootstrap.py",                  # M6.6-C3 / ADR-12：唯一的 Bootstrap 建立点
})
#: 扫描面排除：管理面 API（它们的写入由各自 Service 承担）与测试。
_SCAN_EXCLUDED_PARTS = ("api", "tests")

#: 幂等建立（find-or-create）标识符不在「自动开户」禁止集合内——
#: 它复用既有行，绝不产生第二行（与 `create_identity` 语义不同）。
_FIND_OR_CREATE_MARKERS = (
    "identities.find_or_create",
    "connection_conversations.find_or_create",
)


def _scan_files() -> list:
    files: list = []
    for path in sorted(_BACKEND_APP.rglob("*.py")):
        parts = set(path.relative_to(_BACKEND_APP).parts)
        if parts & set(_SCAN_EXCLUDED_PARTS):
            continue
        files.append(path)
    return files


def test_adr5_runtime_never_creates_acp_user_or_identity() -> None:
    """ADR-5：运行时链路零创建 Identity / User（Resolve → Check，不 Create）。

    保留原断言（4 个 runtime 文件的零标识符/零调用）作为**基线**，
    再由下面的全仓允许清单守卫给出**充分性**。
    """
    for path in _RUNTIME_CHAIN_FILES:
        offenders = _identifiers(path) & _PROVISIONING_IDENTIFIERS
        assert not offenders, f"{path.name} 出现自动开户标识：{sorted(offenders)}"
        calls = _call_names(path) & _PROVISIONING_IDENTIFIERS
        assert not calls, f"{path.name} 调用了自动开户：{sorted(calls)}"


def test_adr5_identity_creation_points_are_allowlisted() -> None:
    """G-D：全仓扫描 ⇒ Identity 的创建点**恰好**是允许清单（ADR-12 E-1）。"""
    creators = sorted({
        path.name for path in _scan_files()
        if "identities.find_or_create" in path.read_text(encoding="utf-8")
        or "uow.identities.create" in path.read_text(encoding="utf-8")
    })
    assert set(creators) <= _IDENTITY_CREATION_FILES, (
        f"出现了允许清单外的 Identity 创建点："
        f"{sorted(set(creators) - _IDENTITY_CREATION_FILES)}")


def test_adr5_grant_creation_points_are_allowlisted() -> None:
    """G-D：全仓扫描 ⇒ Grant 的创建点**恰好**是允许清单（ADR-12 E-1）。"""
    creators = sorted({
        path.name for path in _scan_files()
        if any(marker in path.read_text(encoding="utf-8")
               for marker in _FIND_OR_CREATE_MARKERS if "connection" in marker)
        or "uow.connection_conversations.create" in path.read_text(encoding="utf-8")
    })
    assert set(creators) <= _GRANT_CREATION_FILES, (
        f"出现了允许清单外的 Grant 创建点："
        f"{sorted(set(creators) - _GRANT_CREATION_FILES)}")


def test_adr5_bootstrap_never_creates_acp_user() -> None:
    """ADR-12 §6：Bootstrap 组件**绝不**创建 ACP User（只建 user_id=NULL 的 Identity）。"""
    path = _BACKEND_APP / "application" / "bootstrap.py"
    assert path.exists(), "缺少 Bootstrap 组件（唯一新增运行时创建点）"
    src = path.read_text(encoding="utf-8")

    offenders = _identifiers(path) & _PROVISIONING_IDENTIFIERS
    assert not offenders, f"bootstrap.py 出现自动开户标识：{sorted(offenders)}"

    # user_id 恒 NULL：绝不把外部参与者提升/绑定到发起人
    assert "user_id=None" in src
    assert "users.create" not in src


def test_adr5_identity_resolution_still_fails_closed() -> None:
    """ADR-5：未知外部身份仍 Fail Closed（不自动建号；也不静默放行）。"""
    path = _BACKEND_APP / "application" / "identity_resolution.py"
    src = _read(path)
    assert "IdentityResolutionFailedError" in src
    assert "raise IdentityResolutionFailedError" in src
    assert not (_call_names(path) & {"create", "add", "save", "link", "upsert"})


# ==================================================================== ADR-6

def test_adr6_inbound_contract_carries_no_authorization_verdict() -> None:
    """ADR-6：入站契约只表达「外部世界说了什么」——不携带任何内部归属/授权结论。

    这也是 Discovery ≠ Authorization 的机器化表达：收到消息的那一刻，
    Connection / Session / Agent / 授权结论一律还不存在。
    """
    fields = set(InboundInteraction.__dataclass_fields__)
    forbidden = {
        "user_id", "identity_id", "session_id", "connection_id", "agent_id",
        "authorized", "authorization", "authorized_connections", "allowed",
        "allowed_connections", "decision", "policy", "permission",
    }
    assert not (fields & forbidden), f"入站契约被塞入内部归属/授权字段：{sorted(fields & forbidden)}"


def test_adr6_no_implicit_grant_on_first_message() -> None:
    """ADR-6：不存在「首次收到消息即授权」的模块/组件。

    **修订说明（Package 2 / Step 4）**：授权现已实现（Step 1 持久化 / Step 2
    Runtime / Step 3 管理面），但本条**一个断言都没有放宽**——

    - `AuthorizationGrant` / `AutoGrant` 仍不存在：授权在 ACP 里不是「决策实体」，
      而是一行 `ConnectionConversation`（= 一条显式的「会话 → 连接」记录）；
    - `AuthorizationDecision` 仍不存在：授权没有 decision 对象 / effect / 条件组合
      （ADR-9 NON-GOAL）——只有「许可集合」。
    """
    assert "AuthorizationGrant" not in _app_class_names()
    assert "AutoGrant" not in _app_class_names()
    assert "AuthorizationDecision" not in _app_class_names()


# ==================================================================== ADR-7

def test_adr7_authorization_must_precede_session_resolution() -> None:
    """ADR-7：执行顺序 = Identity → **Authorization** → Routing → Session Resolution。

    **修订说明（Package 2 / Step 4）**：Step 2 已把 Authorization 落进运行时；
    本守卫从「锁定顺序基座」升级为**直接锁定 ADR-7 的实现位置**：

        authorization 必须**早于** session_resolution（拒绝绝不产生 Session），
        且**早于** routing（ADR-8：先授权，后选择）。
    """
    src = _read(_BACKEND_APP / "application" / "coordinator.py")
    i_identity = src.index("self._identity.execute")
    i_authz = src.index("self._authorization.execute")
    i_routing = src.index("self._routing.execute")
    i_session = src.index("self._session_resolution.execute")
    assert i_identity < i_authz < i_routing < i_session

    text = _read(_ADR_DOC)
    assert "Authorization **在** Session Resolution **之前**" in text
    assert "不产生 Session" in text


def test_adr7_rejection_is_recordable_without_new_status_column() -> None:
    """ADR-7：拒绝仍可记录，但必须映射到既有双状态 + 失败三元组（不得新增 status 列）。"""
    cols = {c.name for c in SessionModel.__table__.columns}  # 反向确认：无授权列
    assert "authorized" not in cols and "authorization" not in cols

    interaction_model = _read(_BACKEND_APP / "persistence" / "models" / "interaction.py")
    assert "processing_status" in interaction_model
    assert "delivery_status" in interaction_model
    assert "failure_stage" in interaction_model
    assert "failure_code" in interaction_model

    text = _read(_ADR_DOC)
    assert "UNAUTHORIZED" in text
    assert "**不得**新增 `status` 列" in text


# ==================================================================== ADR-8

_FORBIDDEN_ROUTING_CLASSES = frozenset({
    "RoutingRule", "RoutingRules", "RoutingPolicy", "RoutingResolver",
    "RoutingTable", "RoutingStrategy", "LoadBalancer", "RoundRobin",
    "RoutingWeight", "RoutingPriority", "RoutingFallback", "AgentRouter",
    "MultiAgentRouter",
})


def test_adr8_router_consumes_eligible_connections_only() -> None:
    """ADR-8：Authorization 先于最终 Routing（Router 只消费已授权集合）。

    **修订说明（Package 2 / Step 4）**：Step 2 已把「只消费已授权集合」从文档
    变成**代码事实**，故本条从纯文档断言升级为**实现断言**：

    - Router 的候选集来源 = 授权产出的 `granted_connection_ids`（**必填、无默认值**
      ——缺席即等于「候选不受限」，占位授权进不来）；
    - 候选 = `active ∩ granted`（授权只**收窄**，存在性 / 唯一性判定仍归 Router）。
    """
    text = _read(_ADR_DOC)
    assert "Eligible Connections" in text
    assert "Router 的输入集合 = 已授权 eligible connections" in text

    routing = _read(_BACKEND_APP / "application" / "routing.py")
    assert "granted_connection_ids" in routing
    assert "if len(connections) != 1" in routing          # 唯一性判定未被放宽
    assert "RoutingFailedError" in routing


def test_adr8_no_multi_agent_routing_expansion() -> None:
    """ADR-8：不引入路由规则引擎；V0 仍是「唯一 active Connection，否则 Fail Closed」。"""
    assert not (_app_class_names() & _FORBIDDEN_ROUTING_CLASSES), (
        f"出现了路由规则引擎：{sorted(_app_class_names() & _FORBIDDEN_ROUTING_CLASSES)}"
    )
    src = _read(_BACKEND_APP / "application" / "routing.py")
    assert "if len(connections) != 1" in src
    assert "RoutingFailedError" in src


# ==================================================================== ADR-9

_FORBIDDEN_PERMISSION_CLASSES = frozenset({
    "RBAC", "RBACModel", "ABAC", "ABACModel", "PolicyEngine", "PolicyModel",
    "PolicyRule", "PolicyDocument", "PolicyDecisionPoint", "PolicyEnforcementPoint",
    "Role", "RoleModel", "RoleHierarchy", "RoleAssignment", "RoleBinding",
    "Permission", "PermissionModel", "PermissionGrant", "PermissionInheritance",
    "Principal", "PrincipalModel", "Resource", "ResourceModel", "Effect", "EffectModel",
    "AccessControlList", "ACL", "ACLEntry", "CustomPolicyDSL", "PolicyDSL",
    "IdentityOverride", "IdentityOverrideRule", "AttributeBasedAccessControl",
    "ScopeModel", "GrantModel", "AccessGrant",
})

_FORBIDDEN_PERMISSION_TABLES = frozenset({
    "roles", "role_permissions", "role_assignments", "permissions", "policies",
    "policy_rules", "policy_documents", "principals", "resources", "effects",
    "access_grants", "acl_entries", "authorization_decisions", "identity_overrides",
})


def test_adr9_v0_has_no_complex_permission_system() -> None:
    """ADR-9：RBAC / ABAC / Policy Engine / 角色层级 / 权限继承 / 策略 DSL 全部不存在。"""
    classes = _app_class_names()
    assert not (classes & _FORBIDDEN_PERMISSION_CLASSES), (
        f"出现了权限系统实体：{sorted(classes & _FORBIDDEN_PERMISSION_CLASSES)}"
    )
    tables = _app_table_names()
    assert not (tables & _FORBIDDEN_PERMISSION_TABLES), (
        f"出现了权限系统表：{sorted(tables & _FORBIDDEN_PERMISSION_TABLES)}"
    )


def test_adr9_no_authorization_module_in_domain_layer() -> None:
    """ADR-9：领域层不得出现**权限体系**模块（policy / role / permission / acl）。

    **修订说明（Package 2 / Step 4，显式白名单而非改名规避）**：V0 确实引入了
    一个 conversation-scoped 的 Access Authorization 阶段（ADR-3 / ADR-7 / ADR-8），
    其结果契约落在 `domain/access_authorization.py`。因此本守卫的语义从
    「禁 authorization 模块」收窄为「**禁权限体系模块**」，并把例外写成**精确、
    可审计的单点**：

    - 唯一允许：`access_authorization.py`（且只承载「许可集合」结果契约）；
    - 仍然禁止：policy / role / permission / acl / rbac / abac / principal 等
      任何权限体系模块 stem ——新增一个即报红；
    - 结果契约自身不得长出 effect / priority / role / principal / condition 字段。
    """
    domain_files = {p.stem for p in (_BACKEND_APP / "domain").glob("*.py")}
    forbidden = {"policy", "policies", "role", "roles", "permission", "permissions",
                 "acl", "rbac", "abac", "principal", "principals", "policy_engine"}
    assert not (domain_files & forbidden), \
        f"领域层出现权限体系模块：{sorted(domain_files & forbidden)}"
    # 唯一例外（精确白名单）：Step 2 的 Access Authorization 结果契约
    assert domain_files & {"authorization", "access_authorization"} == {
        "access_authorization"}

    from app.domain.access_authorization import AccessAuthorization
    fields = set(AccessAuthorization.__dataclass_fields__)
    assert fields == {"granted_connection_ids"}, f"结果契约长出越权字段：{fields}"


# =================================================================== ADR-10

def test_adr10_ux_constraint_is_frozen() -> None:
    """ADR-10：UX 也是设计约束——默认路径是「在哪工作」，不是授权配置面板。"""
    text = _read(_ADR_DOC)
    assert "Simple by Default. Powerful When Needed." in text
    assert "Where should it work?" in text
    assert "默认引导流程中不得出现 Policy / Principal / Resource / Scope / Effect / ACL" in text


# ===================================================================== 总闸

def test_repository_has_no_forbidden_conversation_entity() -> None:
    """总闸：Conversation 只是 channel-side 会话键，不是 ACP 实体（ADR-3 红线）。"""
    classes = _app_class_names()
    assert not (classes & {"Conversation", "ConversationModel", "ConversationEntity"})
    assert "conversations" not in _app_table_names()
