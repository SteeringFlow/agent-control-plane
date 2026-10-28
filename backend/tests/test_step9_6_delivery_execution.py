"""Step 9.6 —— Delivery Execution（8.6 语义的第一次真实执行，Runtime 闭环最后一段）。

    AgentResponse
        ↓ OutboundResponse（channel_id / external_conversation_id / content / metadata?）
        ↓ ChannelAdapter.deliver(outbound)
        ↓ Channel

**Delivery is transport, not routing.** 本阶段不接真实 WeCom：用 Test Channel
Adapter 注入，证明 Delivery Execution 成立。

锁定项：
A  成功：outbound 原样交给 Adapter（channel_id / external_conversation_id /
   content 原样）；
B  只消费 OutboundResponse：不重新 Routing / Identity / Session Resolution /
   找 Agent / 读 Connection 决定目的地（不 import 任何 Repository/UoW）；
C  Adapter transport/protocol 失败 → ChannelDeliveryFailedError（CHANNEL_DELIVERY
   _FAILED），不新造 ChannelNotFound / ChannelAuthFailed / ChannelTimeout /
   ChannelRateLimited / ChannelTransportFailed；
D  Delivery failure ≠ processing failure（processing_status ≠ delivery_status，
   4.2/5.3 冻结）；
E  不直接访问 ORM / SQLAlchemy；不创建 DeliveryService / ChannelManager /
   WeComAdapter；
F  Test Adapter 只存在于测试中（app/adapters 仍空壳）；
G  mutation guard。
"""

import ast
from pathlib import Path

import pytest

from app.application import (
    ChannelDeliveryFailedError,
    DeliveryExecutor,
)
from app.domain.outbound_response import OutboundResponse

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]
DELIVERY_PY = APP_DIR / "application" / "delivery.py"

FORBIDDEN_ROOTS = (
    "sqlalchemy", "fastapi", "starlette", "pydantic",
    "app.persistence", "app.adapters", "app.api", "app.schemas",
)


def _root_allowed(root: str) -> bool:
    # Delivery 只依赖 ChannelAdapter Port + 契约 + 错误——**不依赖任何 Repository/UoW**
    # P1 / C1：出站信封构建（纯函数 `build_outbound_response`）落在本模块，
    # 故新增两个 **domain 契约**依赖（AgentResponse / InboundInteraction）——
    # 仍**不**引入任何 Repository / UoW / Adapter 实现。
    return (root == "app.repositories.channel_adapter"
            or root == "app.application.errors"
            or root == "app.domain.outbound_response"
            or root == "app.domain.agent_response"
            # P2.1：恢复路径要把已落库的 artifact 引用快照还原成领域对象
            # （`Artifact` 值对象）——仍是**领域契约**，不引入 Repository/UoW。
            or root == "app.domain.artifact"
            or root == "app.domain.inbound_interaction"
            or root in {"collections", "dataclasses", "typing"})


# --------------------------------------------------------------- Test Channel Adapter


class _TestChannelAdapter:
    """满足 ChannelAdapter Port 的测试 Adapter：记录交还的 outbound。"""

    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


class _FailingChannelAdapter:
    """模拟 Channel transport 失败（→ CHANNEL_DELIVERY_FAILED）。"""

    def deliver(self, outbound: OutboundResponse) -> None:
        raise RuntimeError("channel transport down")


def _executor(adapter) -> DeliveryExecutor:
    return DeliveryExecutor(channel_adapter=adapter)


def _outbound(**overrides) -> OutboundResponse:
    defaults = dict(channel_id=1, external_conversation_id="conversation-123",
                    content="reply from agent")
    defaults.update(overrides)
    return OutboundResponse(**defaults)


# ------------------------------------------------------------------ AST helpers


def _roots(src: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(src, filename=str(DELIVERY_PY))):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def _classes(src: str) -> set[str]:
    return {n.name for n in ast.walk(ast.parse(src, filename=str(DELIVERY_PY)))
            if isinstance(n, ast.ClassDef)}


def _boundary_problems(src: str) -> list[str]:
    problems: list[str] = []
    for root in sorted(_roots(src)):
        if not _root_allowed(root):
            problems.append(f"越层依赖 {root}（Delivery 不应读任何 Repository/UoW）")
    tree = ast.parse(src, filename=str(DELIVERY_PY))
    forbidden_names = {"SqlAlchemyUnitOfWork", "Session", "sessionmaker",
                       "create_engine", "Engine", "ConnectionModel", "ChannelModel",
                       "HTTPException", "APIRouter", "WeComAdapter",
                       "ConnectionRepository", "IdentityRepository",
                       "SessionRepository"}
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
    extra = _classes(src) - {"DeliveryExecutor"}
    if extra:
        problems.append(f"出现非 Execution 组件类：{sorted(extra)}")
    return problems


# ================================================================== A 成功路径


def test_a_deliver_success_passes_outbound_to_adapter() -> None:
    adapter = _TestChannelAdapter()
    outbound = _outbound()
    _executor(adapter).execute(outbound)
    assert adapter.deliveries == [outbound]


def test_a_fields_passed_verbatim() -> None:
    """channel_id / external_conversation_id / content **原样**交给 Adapter。"""
    adapter = _TestChannelAdapter()
    _executor(adapter).execute(_outbound(
        channel_id=7, external_conversation_id="group-x", content="exact text",
        metadata={"k": "v"}))
    delivered = adapter.deliveries[-1]
    assert delivered.channel_id == 7
    assert delivered.external_conversation_id == "group-x"
    assert delivered.content == "exact text"
    assert delivered.metadata == {"k": "v"}


def test_a_optional_fields() -> None:
    adapter = _TestChannelAdapter()
    _executor(adapter).execute(_outbound())
    delivered = adapter.deliveries[-1]
    assert delivered.metadata is None


# ================================================================== B 只消费 OutboundResponse


def test_b_no_repository_or_uow_dependency() -> None:
    """Delivery 不读任何 Repository/UoW——不重新找 User/Identity/Connection/Agent。"""
    src = DELIVERY_PY.read_text(encoding="utf-8")
    assert not _boundary_problems(src), _boundary_problems(src)
    roots = _roots(src)
    assert "app.repositories.channel_adapter" in roots
    for r in roots:
        if r.startswith("app.repositories") and r != "app.repositories.channel_adapter":
            pytest.fail(f"Delivery 不应依赖 {r}")


def test_b_does_not_look_up_anything_for_destination() -> None:
    """目的地全在 OutboundResponse：无 find/get/query 调用。"""
    src = DELIVERY_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lookups = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"get_by_id", "find_active_by_channel",
                               "find_by_channel_and_agent"}
    }
    assert not lookups, f"Delivery 不应查目的地：{lookups}"


# ================================================================== C 失败映射


def test_c_invalid_outbound_fails_closed() -> None:
    bad_inputs = [
        "not an OutboundResponse",
        _outbound(channel_id=0),
        _outbound(channel_id=-1),
        _outbound(channel_id="1"),  # type: ignore[arg-type]
        _outbound(external_conversation_id=""),
        _outbound(external_conversation_id="   "),
        _outbound(content=""),
        _outbound(metadata="not-dict"),  # type: ignore[arg-type]
    ]
    for bad in bad_inputs:
        adapter = _TestChannelAdapter()
        with pytest.raises(ChannelDeliveryFailedError):
            _executor(adapter).execute(bad)
        assert adapter.deliveries == [], "Fail Closed：不应把无效 outbound 交给 Adapter"


def test_c_transport_failure_maps_to_channel_delivery_failed() -> None:
    with pytest.raises(ChannelDeliveryFailedError):
        _executor(_FailingChannelAdapter()).execute(_outbound())


def test_c_no_granular_channel_errors() -> None:
    """不新造底层 Channel 错误：errors.py 只新增一个 ChannelDeliveryFailedError。"""
    import app.application.errors as err_mod

    tree = ast.parse(Path(err_mod.__file__).read_text(encoding="utf-8"))
    class_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert "ChannelDeliveryFailedError" in class_names
    for banned in ("ChannelNotFound", "ChannelAuthFailed", "ChannelTimeout",
                   "ChannelRateLimited", "ChannelTransportFailed"):
        assert banned not in class_names, f"不应新造 {banned}"


# ================================================================== D processing ≠ delivery


def test_d_delivery_failure_not_processing_failure_frozen() -> None:
    src = DELIVERY_PY.read_text(encoding="utf-8")
    assert "delivery" in src.lower()
    # 语义冻结：Delivery failure 是 delivery 状态，不变成 processing failure
    assert "processing_status ≠ delivery_status" in src


# ================================================================== E Execution ≠ Service


def test_e_module_classes_only_delivery_executor() -> None:
    src = DELIVERY_PY.read_text(encoding="utf-8")
    assert _classes(src) == {"DeliveryExecutor"}, _classes(src)
    for banned in ("DeliveryService", "ChannelManager", "WeComAdapter",
                   "FeishuAdapter", "WeChatAdapter", "SlackAdapter"):
        assert f"class {banned}" not in src, f"不应出现 {banned}"


def test_e_adapters_still_shells() -> None:
    """15.2.3：agents 含 acp.py+local.py+registry.py；channels 含
    wecom.py+local.py+registry.py（无测试外的新协议实现）。"""
    for sub in ("adapters/channels", "adapters/agents"):
        files = sorted(p.name for p in (APP_DIR / sub).glob("*.py"))
        expected = ["__init__.py", "acp.py", "local.py", "registry.py", "workbuddy.py"] \
            if sub == "adapters/agents" \
            else ["__init__.py", "local.py", "registry.py", "wecom.py",
                  "wecom_bot.py"]
        assert files == expected, f"{sub} 实现面漂移：{files}"


# ================================================================== G mutation 探测


@pytest.mark.parametrize("inject", [
    "import sqlalchemy",
    "from app.persistence.models.channel import ChannelModel",
    "from app.repositories.connection import ConnectionRepository",
])
def test_g_forbidden_import_is_caught(inject: str) -> None:
    src = DELIVERY_PY.read_text(encoding="utf-8") + "\n" + inject + "\n"
    assert _boundary_problems(src), f"边界守卫未抓到 {inject}"


def test_g_delivery_service_injection_caught() -> None:
    """真实文件注入 DeliveryService → 本守卫 + 8.6 组件扫描必须报红。"""
    backup = DELIVERY_PY.read_text(encoding="utf-8")
    try:
        DELIVERY_PY.write_text(
            backup + "\n\nclass DeliveryService:\n    pass\n", encoding="utf-8")
        assert _boundary_problems(DELIVERY_PY.read_text(encoding="utf-8")), \
            "模块类面守卫未抓到 DeliveryService"
        # 8.6 组件扫描必须报红（token "delivery"）
        audit_py = BACKEND_DIR / "tests" / "test_step8_6_delivery_contract.py"
        ns: dict = {"__file__": str(audit_py), "__name__": "audit_under_9_6"}
        exec(compile(audit_py.read_text(encoding="utf-8"), str(audit_py), "exec"), ns)
        problems = ns["_component_violations"]()
        assert any("DeliveryService" in p for p in problems), \
            f"8.6 组件扫描未抓到 DeliveryService：{problems}"
    finally:
        DELIVERY_PY.write_text(backup, encoding="utf-8")
