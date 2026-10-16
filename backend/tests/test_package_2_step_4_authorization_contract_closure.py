"""Package 2 / Step 4 — Authorization Contract Closure（架构守卫最终收口）。

本文件把 Package 2 落地后**散落在各步**的授权契约收敛为**一处可执行的收口清单**。
它不新增能力，只做一件事：把「授权到底是什么」的十条边界写成机器断言，并自检
守卫清单本身不会静默消失。

十条契约（全部来自 ADR-1/2/3/6/7/8/9 + Package 2-A/B/C 的冻结裁决）：

    g1  Runtime Authorization 结果契约 = 只有许可集合
    g2  阶段接线冻结（stage / code / retryable + 执行顺序）
    g3  default-deny 是结构性的（空集必然 DENY，无兜底放行）
    g4  不存在 blanket authorization（无通配 / 无 bulk / 无 autouse 注入）
    g5  known 永不作为授权输入（Discovery ≠ Authorization）
    g6  授权指向 Connection，不指向 Agent（Conversation → Connection）
    g7  Router 只消费已授权候选（授权收窄，不替代唯一性判定）
    g8  Authorization 不读 Connection 状态
    g9  Runtime 路径零自动创建
    g10 无 Identity / User 耦合
    g11 无 Update / bulk / policy engine 面
    g12  存储仍是「一张普通授权表」
    g13  守卫清单自检（本收口清单不会静默消失）

与既有护栏同一纪律：AST 扫描**真实标识符**，避免拒斥性 docstring 产生假阳性。
"""

import ast
from pathlib import Path

import pytest

from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.interaction_recording import STAGE_FAILURE_MAP
from app.application.runtime import RUNTIME_EXECUTION_ORDER
from app.domain.access_authorization import AccessAuthorization
from app.domain.activity_record import STAGE_AUTHORIZATION
from app.application.errors import AccessAuthorizationDeniedError
from app.persistence.models.connection_conversation import ConnectionConversationModel
from app.schemas.connection_conversations import ConnectionConversationCreate

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"
TESTS_DIR = BACKEND_DIR / "tests"

AUTHZ_EXECUTOR_PY = APP_DIR / "application" / "access_authorization.py"
AUTHZ_CONTRACT_PY = APP_DIR / "domain" / "access_authorization.py"
GRANT_PORT_PY = APP_DIR / "repositories" / "connection_conversation.py"
GRANT_ADAPTER_PY = APP_DIR / "persistence" / "repositories" / "connection_conversation.py"
GRANT_SERVICE_PY = APP_DIR / "application" / "connection_conversations.py"
GRANT_API_PY = APP_DIR / "api" / "management" / "connection_conversations.py"
ROUTING_PY = APP_DIR / "application" / "routing.py"
COORDINATOR_PY = APP_DIR / "application" / "coordinator.py"
CONFTEST_PY = TESTS_DIR / "conftest.py"

GUARD_FILES = (
    TESTS_DIR / "test_adr_authorization_and_access_model.py",
    TESTS_DIR / "test_package_2_step_2_runtime_authorization.py",
    TESTS_DIR / "test_package_2_step_3_grant_management_api.py",
    TESTS_DIR / "test_package_2_step_4_authorization_contract_closure.py",
)


# ------------------------------------------------------------------ AST helpers


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _identifiers(path: Path) -> set[str]:
    """Name / Attribute 标识符（docstring 不计入）——与 ADR 护栏同一纪律。"""
    found: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def _call_attrs(path: Path) -> set[str]:
    return {
        node.func.attr for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _function(path: Path, name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 未找到 {name}")


def _function_identifiers(path: Path, name: str) -> set[str]:
    fn = _function(path, name)
    found: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def _test_names(path: Path) -> set[str]:
    return {
        node.name for node in ast.walk(_tree(path))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


# ============================================= g1 结果契约 = 只有许可集合


def test_g1_runtime_authorization_contract_is_grants_only() -> None:
    """契约面 = `granted_connection_ids` 一个字段；无 effect / 条件 / 身份维度。"""
    assert set(AccessAuthorization.__dataclass_fields__) == {"granted_connection_ids"}
    # 按既有护栏纪律扫**真实标识符**（docstring 里写着「没有 effect / 优先级 …」
    # 是纪律声明，不是实现——纯文本匹配会把它误判为违约）
    contract_ids = {i.lower() for i in _identifiers(AUTHZ_CONTRACT_PY)}
    for banned in ("effect", "priority", "condition", "role", "principal",
                   "identity_id", "user_id", "agent_id", "session_id",
                   "connection_status", "is_allowed", "decision"):
        assert banned not in contract_ids, f"结果契约出现 {banned}"

    # 执行器面：只有 __init__ / execute（是 Execution，不是 Service/Policy）
    methods = [n.name for n in ast.walk(_tree(AUTHZ_EXECUTOR_PY))
               if isinstance(n, ast.FunctionDef)]
    assert methods == ["__init__", "execute"], methods
    # 判定输入恰好两个键（不含 identity / user / content）
    args = [a.arg for a in _function(AUTHZ_EXECUTOR_PY, "execute").args.args[1:]]
    assert args == ["channel_id", "external_conversation_id"], args


# ============================================= g2 阶段接线冻结


def test_g2_stage_wiring_is_frozen() -> None:
    """failure 三元组 + 执行顺序 + 失败码语义（各 stage 用自己的错误）。"""
    assert STAGE_FAILURE_MAP[AccessAuthorizationDeniedError] == (
        STAGE_AUTHORIZATION, "UNAUTHORIZED", False)
    assert STAGE_AUTHORIZATION == "authorization"
    order = list(RUNTIME_EXECUTION_ORDER)
    assert order.index("identity_resolution") < order.index("authorization")
    assert order.index("authorization") < order.index("routing")
    assert order.index("authorization") < order.index("session_resolution")

    # 授权**只**用自己那一个 typed error（不借用 routing / delivery 的错误）
    foreign = {"IdentityResolutionFailedError", "RoutingFailedError",
               "SessionResolutionFailedError", "AgentDispatchFailedError",
               "AgentResponseInvalidError", "ChannelDeliveryFailedError"}
    assert not (_identifiers(AUTHZ_EXECUTOR_PY) & foreign)


# ============================================= g3 default-deny 的结构性


def _default_deny_problems(src: str) -> list[str]:
    """default-deny 的结构性问题清单（可对**任意源码字符串**运行 → 可被 mutation 验证）。"""
    problems: list[str] = []
    if "raise AccessAuthorizationDeniedError" not in src:
        problems.append("缺 DENY 抛出")
    if "if not granted:\n            raise AccessAuthorizationDeniedError" not in src:
        problems.append("空集未 DENY（判定分支形态变了）")
    if "return AccessAuthorization" not in src:
        problems.append("缺 ALLOW 返回")
    for banned in ("granted or ", "if granted is None", "default_granted",
                   "granted_connection_ids=frozenset())"):
        if banned in src:
            problems.append(f"兜底放行：{banned}")
    return problems


def test_g3_default_deny_is_structural() -> None:
    """空集必然 DENY：源码里「无许可 → raise」，且没有任何兜底放行分支。"""
    problems = _default_deny_problems(AUTHZ_EXECUTOR_PY.read_text(encoding="utf-8"))
    assert not problems, problems


@pytest.mark.parametrize("inject", [
    # 1) 把「空集 DENY」改成兜底放行
    "\n\ndef _fallback(granted):\n    return AccessAuthorization("
    "granted_connection_ids=frozenset())\n",
    # 2) 用 `granted or …` 把空集当假值静默放过
    "\n\ndef _loose(granted):\n    return granted or None\n",
])
def test_g3b_default_deny_mutation_is_caught(inject: str) -> None:
    """mutation：往执行器源码注入兜底放行 → g3 必须报红（守卫不是空转）。"""
    src = AUTHZ_EXECUTOR_PY.read_text(encoding="utf-8") + inject
    assert _default_deny_problems(src), f"mutation 未被抓到：{inject!r}"


# ============================================= g4 无 blanket authorization


_BLANKET_TOKENS = ("grant_all", "grantall", "all_conversations", "wildcard",
                   "bulk_", "create_many", "grant_everything")


def _blanket_problems(label: str, src: str) -> list[str]:
    """blanket 机制问题清单（任意源码字符串 → 可被 mutation 验证）。"""
    low = src.lower()
    return [f"{label} 出现 blanket 机制：{t}" for t in _BLANKET_TOKENS if t in low]


def test_g4_no_blanket_authorization() -> None:
    """不存在通配 / bulk / 全量授权机制；测试侧也不得有 autouse 授权注入。

    Step 0 §D 的 G-5 红线：夹具必须**显式**授权每个会话键，不得用「授权全部会话」
    或 autouse fixture 让 default-deny 形同虚设。
    """
    for path in sorted(APP_DIR.rglob("*.py")):
        assert not _blanket_problems(
            path.name, path.read_text(encoding="utf-8"))

    # schema 字段面恰好三键（无法塞入 all / * / effect）
    assert set(ConnectionConversationCreate.model_fields) == {
        "channel_id", "external_conversation_id", "connection_id"}
    assert ConnectionConversationCreate.model_config.get("extra") == "forbid"

    # conftest 不得预置授权（autouse 或全局 fixture 都不行）
    # M6.6-C3：同样不得预置 Bootstrap Arm（否则「无 Arm ⇒ Fail Closed」会被静默绕过）
    conftest = CONFTEST_PY.read_text(encoding="utf-8")
    assert "connection_conversations" not in conftest, "conftest 预置了授权"
    assert "bootstrap_arms" not in conftest, "conftest 预置了 Bootstrap Arm"

    # 任何 autouse fixture 都不得创建授权
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.FunctionDef):
                continue
            decorated = {
                ast.unparse(d) for d in node.decorator_list
            }
            if not any("autouse" in d for d in decorated):
                continue
            body = ast.unparse(node)
            assert "connection_conversations" not in body, (
                f"{path.name}::{node.name} 是 autouse fixture 且创建了授权")
            # M6.6-C3：autouse 也不得预置 Bootstrap Arm（Arm 夹具必须显式、非 autouse）
            assert "bootstrap_arms" not in body, (
                f"{path.name}::{node.name} 是 autouse fixture 且创建了 Bootstrap Arm")


def test_g4b_blanket_mutation_is_caught() -> None:
    """mutation：注入一个「授权全部会话」的入口 → g4 必须报红。"""
    injected = ("def grant_all(uow, channel_id):\n"
                "    return all_conversations(channel_id)\n")
    assert _blanket_problems("mutant.py", injected), "mutation 未被抓到"


# ============================================= g5 known 永不作为授权输入


def test_g5_known_conversations_is_never_an_authorization_input() -> None:
    """授权路径不读 sessions / Discovery；Discovery 路径不产生授权（双向隔离）。"""
    executor_ids = _identifiers(AUTHZ_EXECUTOR_PY)
    for banned in ("sessions", "list_known_conversation_ids",
                   "find_active_by_identity_and_conversation", "discovery"):
        assert banned not in executor_ids, f"授权执行器读取了 {banned}"

    # 授权仓储的判定查询只读授权表
    granted_ids = _function_identifiers(GRANT_ADAPTER_PY, "find_granted_connection_ids")
    assert "sessions" not in granted_ids

    # Discovery 的 HTTP 端点不调用任何创建方法
    route_calls = _function_identifiers(GRANT_API_PY, "list_known_conversations")
    assert "create_connection_conversation" not in route_calls
    assert "delete_connection_conversation" not in route_calls


# ============================================= g6 Conversation → Connection


def test_g6_grant_targets_connection_not_agent() -> None:
    """授权把会话连到 **Connection**（Access Path），绝不连到 Agent。"""
    assert "agent_id" not in set(AccessAuthorization.__dataclass_fields__)
    cols = {c.name for c in ConnectionConversationModel.__table__.columns}
    assert "agent_id" not in cols, "授权表出现 agent_id（授权对象被上移）"

    # 授权执行器不出现任何 agent 派生逻辑
    executor_ids = {i.lower() for i in _identifiers(AUTHZ_EXECUTOR_PY)}
    assert not [i for i in executor_ids if "agent" in i], executor_ids

    # 授权结果里只有连接 id（agent 由 Routing 从 Connection 派生）
    assert "agent_id" not in _function_identifiers(GRANT_ADAPTER_PY,
                                                   "find_granted_connection_ids")


# ============================================= g7 Router 只消费 eligible


def test_g7_router_consumes_only_eligible_candidates() -> None:
    """候选 = active ∩ granted；授权集必填无默认；唯一性判定未被放宽。"""
    fn = _function(ROUTING_PY, "execute")
    args = [a.arg for a in fn.args.args[1:]]
    assert args == ["channel_id", "granted_connection_ids"], args
    assert fn.args.defaults == [] and fn.args.kwonlyargs == []

    src = ROUTING_PY.read_text(encoding="utf-8")
    assert "if len(connections) != 1" in src
    assert "c.id in granted_connection_ids" in src
    # Router 不自己查授权（授权由 Coordinator 传入）
    assert "connection_conversations" not in src

    coordinator = COORDINATOR_PY.read_text(encoding="utf-8")
    assert "authorized.granted_connection_ids" in coordinator


# ============================================= g8 不读 Connection 状态


def test_g8_authorization_never_reads_connection_state() -> None:
    """授权判定不看连接是否存在 / 是否 active（那是 Routing 的裁决）。"""
    banned = {"status", "connections", "find_active_by_channel", "ConnectionNotFoundError",
              "connection_status", "inactive", "active"}
    executor_ids = _identifiers(AUTHZ_EXECUTOR_PY)
    assert not (executor_ids & banned), sorted(executor_ids & banned)

    granted_ids = _function_identifiers(GRANT_ADAPTER_PY, "find_granted_connection_ids")
    assert not (granted_ids & {"connections", "status", "ConnectionModel"}), granted_ids


# ============================================= g9 Runtime 零自动创建


def test_g9_runtime_path_never_auto_creates_grants() -> None:
    """授权行的创建点**恰好**是允许清单：管理面 Service + **唯一**的 Bootstrap 组件。

    M6.6-C3 / ADR-12 §11（G-E）：`bootstrap.py` 是 ADR-12 显式授权的第 2 个
    （也是最后一个）运行时建立点；出现第 3 个必须回到 ADR Gate。
    """
    creators: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if ("connection_conversations.create" in text
                or "connection_conversations.find_or_create" in text):
            creators.append(path.name)
    assert sorted(set(creators)) == ["bootstrap.py", "connection_conversations.py"], \
        creators

    # 运行时链路不 import 管理面 Service（授权不可被运行时「顺手」补写）
    for rel in ("application/coordinator.py", "application/routing.py",
                "application/access_authorization.py",
                "application/identity_resolution.py"):
        src = (APP_DIR / rel).read_text(encoding="utf-8")
        assert "ConnectionConversationApplicationService" not in src, rel
        assert "create_connection_conversation" not in src, rel


# ============================================= g10 无 Identity / User 耦合


def test_g10_authorization_has_no_identity_or_user_coupling() -> None:
    """Conversation-scoped（ADR-3）：身份是否绑定 ACP User **不影响**授权判定。"""
    executor_ids = {i.lower() for i in _identifiers(AUTHZ_EXECUTOR_PY)}
    for banned in ("identity", "user", "external_identity_id"):
        assert not [i for i in executor_ids if banned in i], banned

    src = AUTHZ_EXECUTOR_PY.read_text(encoding="utf-8")
    assert "identities" not in src and "users" not in src

    # 判定查询的参数面只有会话键（Channel Scope + Conversation）
    port_fn = _function(GRANT_PORT_PY, "find_granted_connection_ids")
    assert [a.arg for a in port_fn.args.args[1:]] == [
        "channel_id", "external_conversation_id"]


# ============================================= g11 无 Update / bulk / policy


def test_g11_no_update_bulk_or_policy_engine_surface() -> None:
    """三层都无 Update；无 bulk；授权面不出现策略引擎词汇（ADR-9）。"""
    port_methods = {n.name for n in ast.walk(_tree(GRANT_PORT_PY))
                    if isinstance(n, ast.FunctionDef) and n.name != "__init__"}
    assert "update" not in port_methods, port_methods

    service_methods = {n.name for n in ast.walk(_tree(GRANT_SERVICE_PY))
                       if isinstance(n, ast.FunctionDef)}
    assert "update_connection_conversation" not in service_methods
    assert not [m for m in service_methods if "bulk" in m or "import" in m], service_methods

    api_src = GRANT_API_PY.read_text(encoding="utf-8")
    for banned in ("def put", "def patch", "@router.put", "@router.patch"):
        assert banned not in api_src, banned

    # 授权面不出现策略引擎词汇（类名 / 标识符层面）
    for path in (AUTHZ_CONTRACT_PY, AUTHZ_EXECUTOR_PY, GRANT_SERVICE_PY):
        ids = {i.lower() for i in _identifiers(path)}
        for banned in ("policy", "effect", "principal", "priority", "role"):
            assert not [i for i in ids if banned in i], f"{path.name}:{banned}"


# ============================================= g12 存储仍是一张普通表


def test_g12_storage_is_still_one_plain_grant_table() -> None:
    """授权表不携带状态 / 效果 / 优先级 / 身份：deny 由缺行表达（ADR-6）。"""
    cols = {c.name for c in ConnectionConversationModel.__table__.columns}
    for banned in ("status", "effect", "priority", "role", "principal",
                   "identity_id", "user_id", "agent_id", "decision"):
        assert banned not in cols, f"授权表长出 {banned}（模型被复杂化）"
    assert {"channel_id", "external_conversation_id", "connection_id"} <= cols
    assert ConnectionConversationModel.__tablename__ == "connection_conversations"


# ============================================= g13 守卫清单自检


def test_g13_architecture_guard_inventory_is_complete() -> None:
    """收口清单自检：各步的架构守卫**逐条仍在**（改名 / 删除即报红）。

    这不是「测试数量」检查，而是「契约条目」检查——每一条都对应一个
    冻结裁决；任一条目消失都意味着某条契约失去了机器约束。
    """
    required: dict[Path, set[str]] = {
        TESTS_DIR / "test_adr_authorization_and_access_model.py": {
            "test_adr_document_is_frozen_and_complete",
            "test_adr1_user_and_external_identity_are_separate_entities",
            "test_adr2_connection_did_not_become_an_authorization_object",
            "test_adr3_conversation_is_the_authorization_scope_key",
            "test_adr4_channel_native_authorization_is_not_rebuilt",
            "test_adr5_runtime_never_creates_acp_user_or_identity",
            "test_adr6_inbound_contract_carries_no_authorization_verdict",
            "test_adr6_no_implicit_grant_on_first_message",
            "test_adr7_authorization_must_precede_session_resolution",
            "test_adr8_router_consumes_eligible_connections_only",
            "test_adr8_no_multi_agent_routing_expansion",
            "test_adr9_v0_has_no_complex_permission_system",
            "test_adr9_no_authorization_module_in_domain_layer",
            "test_adr10_ux_constraint_is_frozen",
            "test_repository_has_no_forbidden_conversation_entity",
        },
        TESTS_DIR / "test_package_2_step_2_runtime_authorization.py": {
            "test_c1_no_grant_active_connection_is_unauthorized",
            "test_c2_grant_with_inactive_connection_is_routing_failed",
            "test_c3_one_grant_one_active_connection_continues",
            "test_c4_two_grants_two_active_connections_is_routing_failed",
            "test_d1_denied_interaction_and_activity_frozen_shape",
            "test_e1_unbound_external_identity_with_grant_is_allowed",
            "test_f3_routing_requires_the_granted_candidate_set",
            "test_f4_router_does_not_query_authorization_records",
        },
        TESTS_DIR / "test_package_2_step_3_grant_management_api.py": {
            "test_a6_cross_tenant_is_rejected",
            "test_c3_cross_tenant_delete_is_404",
            "test_d3_discovery_never_creates_grant",
            "test_d4_known_conversation_is_not_authorized",
            "test_e1_no_update_endpoint",
            "test_e2_independent_resource_not_nested_under_connections",
            "test_e3_discovery_is_read_only_at_source_level",
        },
        TESTS_DIR / "test_external_identity_decoupling_package_1.py": {
            "test_p2_1_unbound_identity_resolves_successfully",
            "test_p2_2_identity_not_found_still_fails_closed",
        },
    }
    missing: list[str] = []
    for path, names in required.items():
        if not path.exists():          # pragma: no cover - 结构性失败
            missing.append(f"{path.name}（文件缺失）")
            continue
        absent = names - _test_names(path)
        missing.extend(f"{path.name}::{n}" for n in sorted(absent))
    assert not missing, "架构守卫条目消失：\n" + "\n".join(missing)

    # 本文件自身的十条契约也必须在场（自检自洽）
    self_names = _test_names(GUARD_FILES[3])
    for index in range(1, 14):
        assert any(n.startswith(f"test_g{index}_") for n in self_names), f"g{index} 缺失"
