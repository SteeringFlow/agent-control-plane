"""Step 8.1 —— InboundInteraction（Runtime 入口契约）Contract & Boundary 冻结。

8.1 只冻结一块地基：Runtime 内部的**标准 Interaction Contract**：

    External Channel
          │
          ▼
    Channel Adapter（协议解析 / 认证 / 归一化）
          │
          ▼
    InboundInteraction      ← 本文件锁定的契约
          │
          ▼
    [Runtime]（Identity / Session / Routing / Connection / Dispatch）
          │
          ▼
    Agent

锁定项：
A  契约本体：纯 dataclass（frozen），字段 = 恰好的 6 个，类型/必填性冻结；
B  值对象边界：≠ Identity / ≠ Session / ≠ Interaction ORM / ≠ AgentRequest
   ——不含任何内部解析 id（user_id / identity_id / agent_id / session_id /
   connection_id），不含 id/created_at/status（不落库、不是持久化实体）；
C  不落库：没有 model / mapper / repository / ApplicationService；
D  两世界分离：Adapter 仍空壳、Runtime 组件（Resolver/Router/Dispatcher…）
   不存在；InboundInteraction 是当前唯一被放行的 "interaction" 命名类；
E  domain 导出与反向审计一致性（见 7.3 白名单 mutation 探测）。
"""

import ast
import dataclasses
from pathlib import Path

import pytest

from app.persistence.database import Base

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"
CONTRACT_PY = APP_DIR / "domain" / "inbound_interaction.py"

# 冻结的字段面（顺序即契约顺序）
EXPECTED_FIELDS = [
    "channel_id",
    "external_interaction_id",
    "external_conversation_id",
    "external_identity_id",
    "content",
    "metadata",
]
# 类型面（AST 归一化文本）
EXPECTED_TYPES = {
    "channel_id": "int",
    "external_interaction_id": "str",
    "external_conversation_id": "str",
    "external_identity_id": "str",
    "content": "str",
    "metadata": "dict | None",
}
# 禁止出现在契约里的内部解析 / 持久化字段
FORBIDDEN_FIELDS = {
    "user_id", "identity_id", "agent_id", "session_id", "connection_id",
    "interaction_id", "id", "created_at", "updated_at", "status",
}
# Runtime 下一阶段组件令牌（本 Slice 不得提前出现）
NEXT_TOKENS = ("resolver", "router", "dispatcher", "authorization",
               "capability", "policy")


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


def _ann_text(stmt: ast.AnnAssign) -> str:
    a = stmt.annotation
    if isinstance(a, ast.Name):
        return a.id
    if isinstance(a, ast.BinOp) and isinstance(a.op, ast.BitOr):
        left = _ann_text(_wrap(a.left))
        right = "None" if isinstance(a.right, ast.Constant) and a.right.value is None \
            else _ann_text(_wrap(a.right))
        return f"{left} | {right}"
    raise AssertionError(f"无法识别的注解：{ast.dump(a)}")


def _wrap(node: ast.AST) -> ast.AST:
    """把注解树节点包装成 AnnAssign，复用 _ann_text 递归逻辑。"""
    class _A(ast.AnnAssign):
        pass

    wrapper = _A(target=ast.Name(id="_"), annotation=node, value=None, simple=1)
    return wrapper


def _dataclass_annassigns(cls: ast.ClassDef) -> list[ast.AnnAssign]:
    return [s for s in cls.body
            if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)]


# ================================================================== A 契约本体


def test_a_contract_module_is_pure_domain_value() -> None:
    """契约模块只 import dataclasses（纯 domain，不碰任何 app 层/ORM）。"""
    assert CONTRACT_PY.is_file(), "缺 app/domain/inbound_interaction.py"
    assert _roots(_tree(CONTRACT_PY)) == {"dataclasses"}, \
        f"契约模块越层导入：{_roots(_tree(CONTRACT_PY))}"


def test_a_contract_class_is_frozen_dataclass() -> None:
    """InboundInteraction = frozen dataclass（值对象，不可变）。"""
    cls = _classes(_tree(CONTRACT_PY))["InboundInteraction"]
    decorators = {
        d.func.id if isinstance(d, ast.Call) else getattr(d, "id", None)
        for d in cls.decorator_list if isinstance(d, (ast.Call, ast.Name))
    }
    assert "dataclass" in decorators
    frozen = any(
        isinstance(d, ast.Call) and isinstance(d.func, ast.Name)
        and d.func.id == "dataclass"
        and any(kw.arg == "frozen" and getattr(kw.value, "value", False)
                for kw in d.keywords)
        for d in cls.decorator_list
    )
    assert frozen, "InboundInteraction 应为 frozen dataclass"


# ================================================================== B 字段冻结


def test_b_fields_exactly_six_in_frozen_order() -> None:
    """字段面 = 恰好 6 个（顺序冻结），不得长出任何其他字段。"""
    cls = _classes(_tree(CONTRACT_PY))["InboundInteraction"]
    names = [s.target.id for s in _dataclass_annassigns(cls)]
    assert names == EXPECTED_FIELDS, f"字段面漂移：{names}"


def test_b_field_types_and_requiredness_frozen() -> None:
    """类型与必填性：前 5 个必填（无默认值），metadata 可选（默认 None）。"""
    cls = _classes(_tree(CONTRACT_PY))["InboundInteraction"]
    anns = {s.target.id: s for s in _dataclass_annassigns(cls)}

    for field, expected_type in EXPECTED_TYPES.items():
        assert _ann_text(anns[field]) == expected_type, \
            f"{field} 类型漂移：{_ann_text(anns[field])}"

    for field in EXPECTED_FIELDS[:5]:
        assert anns[field].value is None, f"{field} 应是必填字段（无默认值）"
    assert anns["metadata"].value is not None, "metadata 应是可选字段"


def test_b_contract_never_carries_internal_ids() -> None:
    """值对象 ≠ 解析结果：不得含 user_id/identity_id/session_id/... 等内部 id。"""
    cls = _classes(_tree(CONTRACT_PY))["InboundInteraction"]
    names = {s.target.id for s in _dataclass_annassigns(cls)}
    assert not (names & FORBIDDEN_FIELDS), \
        f"契约混入内部 id / 持久化字段：{names & FORBIDDEN_FIELDS}"
    # external_identity_id 只是渠道侧标识（字面前缀 external_），不是内部 id
    assert "external_identity_id" in names


def test_b_contract_behavior() -> None:
    """运行时行为：可构造、默认 metadata=None、frozen 不可变。"""
    from app.domain.inbound_interaction import InboundInteraction

    msg = InboundInteraction(
        channel_id=7,
        external_interaction_id="wecom_evt_0001",
        external_conversation_id="wecom_conv_42",
        external_identity_id="wecom_user_zhangsan",
        content="hello",
    )
    assert msg.channel_id == 7
    assert msg.external_interaction_id == "wecom_evt_0001"
    assert msg.external_conversation_id == "wecom_conv_42"
    assert msg.external_identity_id == "wecom_user_zhangsan"
    assert msg.content == "hello"
    assert msg.metadata is None

    msg2 = dataclasses.replace(msg, metadata={"raw_event_type": "text"})
    assert msg2.metadata == {"raw_event_type": "text"}
    assert msg.metadata is None  # 原对象不可变

    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.content = "overwrite"  # type: ignore[misc]


def test_b_domain_package_exports_contract() -> None:
    """domain 包对外导出 InboundInteraction（与 Connection 同待遇）。"""
    from app.domain import InboundInteraction as _E1
    from app.domain.inbound_interaction import InboundInteraction as _E2

    assert _E1 is _E2


# ================================================================== C 不落库


def test_c_contract_is_not_persisted() -> None:
    """InboundInteraction ≠ Interaction ORM：没有 model/mapper/repository/service。"""
    stray = [
        APP_DIR / "persistence" / "models" / "inbound_interaction.py",
        APP_DIR / "persistence" / "mappers" / "inbound_interaction.py",
        APP_DIR / "persistence" / "repositories" / "inbound_interaction.py",
        APP_DIR / "repositories" / "inbound_interaction.py",
        APP_DIR / "application" / "inbound_interactions.py",
    ]
    for py in stray:
        assert not py.exists(), f"InboundInteraction 不应有落库/管理实现：{py}"


def test_c_interactions_orm_table_stays_separate() -> None:
    """interactions 表（5.3 冻结的持久化记录）仍在，但与本契约是两回事。"""
    from app.persistence import models  # noqa: F401  # 注册全部表

    assert "interactions" in Base.metadata.tables
    # 没有任何 InboundInteractionModel 之类的 ORM 类
    src = (APP_DIR / "persistence" / "models" / "__init__.py").read_text(
        encoding="utf-8") + "\n" + (
        APP_DIR / "persistence" / "models" / "interaction.py").read_text(encoding="utf-8")
    assert "InboundInteraction" not in src
    # 契约模块也不 import ORM
    assert _roots(_tree(CONTRACT_PY)) == {"dataclasses"}


# ================================================================== D 两世界分离


def test_d_adapters_and_runtime_entry_remain_shells() -> None:
    """Channel 侧 Adapter world / Runtime entry 仍只有 __init__.py；
    adapters/agents 自 10.1.2 起含唯一真实实现 acp.py（ACPAdapter）。"""
    # 15.2.3：agents/channels 各含真实 Adapter + registry.py；api/runtime 含 channels.py
    for sub in ("adapters/channels", "adapters/agents", "api/runtime"):
        files = sorted(p.name for p in (APP_DIR / sub).glob("*.py"))
        expected = ["__init__.py", "acp.py", "local.py", "registry.py", "workbuddy.py"] \
            if sub == "adapters/agents" \
            else ["__init__.py", "local.py", "registry.py", "wecom.py",
                  "wecom_bot.py"] \
            if sub == "adapters/channels" \
            else ["__init__.py", "channels.py"] if sub == "api/runtime" \
            else ["__init__.py"]
        assert files == expected, f"{sub} 实现面漂移：{files}"


def test_d_no_runtime_component_classes_yet() -> None:
    """无 IdentityResolver / SessionResolver / Router / Dispatcher / Policy 等类。

    例外一律走**精确白名单**（文件 + 符号名），不放宽 token：
    - Cloud-0.1.1-B：`TenantResolver` 是入口租户解析**端口**；
    - Package 2 / Step 2：Access Authorization = **已交付阶段**（ADR-3/7/8）的
      结果契约 + 执行器 + 失败类型。含 "authorization" 词，但只有许可集合，
      无 effect / role / principal / 条件组合（ADR-9 NON-GOAL 不变）。
    """
    problems: list[str] = []
    EXACT_ALLOWLIST = {
        ("app/core/tenant.py", "TenantResolver"),
        ("app/domain/access_authorization.py", "AccessAuthorization"),
        ("app/application/access_authorization.py", "AccessAuthorizationExecutor"),
        ("app/application/errors.py", "AccessAuthorizationDeniedError"),
    }
    for py in sorted(APP_DIR.rglob("*.py")):
        if (APP_DIR / "persistence" / "models") in py.parents:
            continue
        rel = "/".join(py.relative_to(BACKEND_DIR).parts)
        tree = _tree(py)
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if (rel, node.name) in EXACT_ALLOWLIST:
                    continue
                lower = node.name.lower()
                if any(tok in lower for tok in NEXT_TOKENS):
                    problems.append(f"{py.relative_to(BACKEND_DIR)}::{node.name}")
    assert not problems, "Runtime 组件提前出现：\n" + "\n".join(problems)


def test_d_contract_is_only_allowed_interaction_named_class() -> None:
    """反向边界：interaction/session 命名的类只能出现在冻结契约白名单里。"""
    found: list[str] = []
    # Cloud-0.1.1-C：Browser Auth 的 "session" 是 **opaque 浏览器会话**（auth 边界），
    # 不是 Runtime Session 实体/契约——这几个模块整模块精确豁免。
    BROWSER_AUTH_EXEMPT = {
        "app/api/auth.py",
        "app/core/browser_auth.py",
        "app/application/browser_auth.py",
        "app/repositories/browser_auth.py",
        "app/persistence/repositories/browser_auth.py",
    }
    for py in sorted(APP_DIR.rglob("*.py")):
        if (APP_DIR / "persistence" / "models") in py.parents:
            continue
        if "/".join(py.relative_to(BACKEND_DIR).parts) in BROWSER_AUTH_EXEMPT:
            continue
        tree = _tree(py)
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if "interaction" in node.name.lower() or "session" in node.name.lower():
                    rel = "/".join(py.relative_to(BACKEND_DIR).parts)
                    found.append(f"{rel}::{node.name}")
    # 白名单（精确放行，随 Slice 落地逐项加入）：
    # - 8.1 InboundInteraction、8.3 SessionResolution：冻结契约值对象
    # - 9.3 Session 读路径：domain/session.py::Session、SessionRepository（Port）、
    #   SqlAlchemySessionRepository（Adapter）
    # - 9.3 Session Resolution Execution：SessionResolutionExecutor +
    #   SessionResolutionFailedError
    # - 9.7 Session Creation Execution：SessionCreationExecutor
    # - 10.4.2 Interaction 持久化：InteractionRecord（记录域镜像，≠ 8.1 契约）、
    #   InteractionRecordRepository（Port/Adapter）、wrapper 的 _find_session
    # - 10.8.4：bind_external_session（Session Port/Adapter 的会话映射写入点，
    #   方法名含 "session"——精确放行，不是新 Session 实体/契约）
    expected = [
        "app/api/management/interactions.py::build_interactions_api",
        "app/api/management/interactions.py::list_interactions",
        "app/api/management/interactions.py::get_interaction",
        "app/application/errors.py::SessionResolutionFailedError",
        "app/application/interaction_observability.py::InteractionObservabilityService",
        "app/application/interaction_observability.py::list_interactions",
        "app/application/interaction_observability.py::get_interaction",
        "app/application/interaction_recording.py::_find_session",
        "app/application/session_creation.py::SessionCreationExecutor",
        "app/application/session_resolution.py::SessionResolutionExecutor",
        f"{'/'.join(CONTRACT_PY.relative_to(BACKEND_DIR).parts)}::InboundInteraction",
        "app/domain/interaction_record.py::InteractionRecord",
        "app/domain/session.py::Session",
        "app/domain/session_resolution.py::SessionResolution",
        "app/persistence/repositories/activity_record.py::list_by_interaction",
        "app/persistence/repositories/interaction_record.py::SqlAlchemyInteractionRecordRepository",
        "app/persistence/repositories/session.py::SqlAlchemySessionRepository",
        "app/persistence/repositories/session.py::bind_external_session",
        "app/repositories/activity_record.py::list_by_interaction",
        "app/repositories/interaction_record.py::InteractionRecordRepository",
        "app/repositories/session.py::SessionRepository",
        "app/repositories/session.py::bind_external_session",
        "app/schemas/interactions.py::InteractionSummaryResponse",
        "app/schemas/interactions.py::InteractionDetailResponse",
        "app/schemas/interactions.py::InteractionListResponse",
    ]  # 10.4.2: + InteractionRecord 记录域镜像 / Port / Adapter / wrapper
    # 13.1: + Observability 只读面（Service 用例 / 读方法 / API 端点 / Schema 投影）
    assert found == expected, f"interaction/session 命名类面漂移：{found}"


# ================================================================== E 反向审计一致性


def test_e_step7_3_reverse_audit_still_green_with_contract() -> None:
    """7.3 反向审计在 8.1 契约加入后仍全绿（白名单精确放行 + mutation 防逃逸）。"""
    audit_py = BACKEND_DIR / "tests" / "test_step7_3_connection_management_audit.py"
    assert audit_py.is_file()
    ns: dict = {"__file__": str(audit_py), "__name__": "test_step7_3_audit_under_8_1"}
    exec(compile(audit_py.read_text(encoding="utf-8"), str(audit_py), "exec"), ns)
    problems = ns["_next_stage_problems"]()
    assert not problems, "7.3 反向审计报红：\n" + "\n".join(problems)
