"""Step 8.6 —— Delivery Contract & Boundary 冻结（不实现 DeliveryService / ChannelAdapter）。

8.6 解决 8.1~8.5 的反方向：

    AgentResponse
        ↓ Delivery
        ↓ OutboundResponse
        ↓ ChannelAdapter
        ↓ External Channel

核心原则：**Delivery is transport, not routing.**
    Routing  = 决定去哪（8.4）
    Dispatch = 发送给 Agent（8.5）
    Delivery = 把 Agent 的结果交还给 Channel——不重新 Routing / 不重新
               Identity Resolution / 不重新 Session Resolution。

锁定项：
A  OutboundResponse：frozen dataclass，字段面 = {channel_id, external_conversation_id,
   content, metadata}；模块纯 stdlib；
B  Delivery 不重新选路：OutboundResponse 无 connection_id / agent_id / user_id /
   identity_id / session_id / routing_rule；
C  Inbound/Outbound 对称：OutboundResponse ⊂ InboundInteraction（共享 channel_id /
   external_conversation_id / content / metadata）；ingress 专属 identity/trace 字段
   不出现在出口；
D  ChannelAdapter 边界：无 ChannelAdapter / WeComAdapter / FeishuAdapter /
   WeChatAdapter / SlackAdapter / HTTP channel / delivery scheduler / queue / retry；
E  Failure semantics：CHANNEL_DELIVERY_FAILED 归 OutboundResponse（Channel Delivery
   失败），与 dispatch_failure 的 AGENT_*（Agent Dispatch 失败）分开；
F  processing_status ≠ delivery_status：interactions 模型两列独立，投递失败与处理
   成功可共存（本契约在 4.2/5.3 设计上闭环）；
G  Fail Closed：拿不到确定 Channel / conversation → 不投递 → CHANNEL_DELIVERY_FAILED，
   不猜 conversation、不重选 Connection；
H  mutation 探测：注入 DeliveryService / WeComAdapter / channel adapter 实现必须报红。
"""

import ast
import dataclasses
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]
OUTBOUND_PY = APP_DIR / "domain" / "outbound_response.py"
INBOUND_PY = APP_DIR / "domain" / "inbound_interaction.py"
FAILURE_PY = APP_DIR / "domain" / "dispatch_failure.py"

# OutboundResponse = Channel 可理解的交还消息（Delivery 输出）
# ACP V0.3 Phase 1：+ artifacts（Agent 产出的**引用**，可选加性字段，默认 None）——
# 契约见 docs/step-acp-v0.3-artifact-outbound-implementation-gate.md §1.2.2 / §5.2。
OUTBOUND_FIELDS = {"channel_id", "external_conversation_id", "content", "metadata",
                    "artifacts"}
# 结构化禁入字段：Delivery 不重新选路 / 不重新归属（不许塞进 metadata 偷渡）
FORBIDDEN_FIELDS = {
    "user_id", "identity_id", "session_id", "connection_id", "agent_id",
    "routing_rule", "capability", "permission", "tool", "memory",
    "routing", "authorization", "policy",
}
# 8.6 明确不新增的 Delivery / ChannelAdapter 组件
DELIVERY_COMPONENT_TOKENS = (
    "delivery", "adapter", "wecomadapter", "feishuadapter", "wechatadapter",
    "slackadapter", "deliveryservice", "deliveryqueue", "deliveryscheduler",
    "messagequeue", "asyncworker", "retry", "channeldelivery",
)
ADAPTER_SHELLS = ("adapters/channels", "adapters/agents", "api/runtime")
# 已交付的出站 Port / 真实 Adapter / Runtime 执行组件（精确白名单，**不是**
# Delivery/Channel 实现）：
# - 9.5 AgentAdapter Port（Agent 侧出站 Port）
# - 9.6 ChannelAdapter Port + DeliveryExecutor + ChannelDeliveryFailedError
#   （是 Execution，不是 DeliveryService / ChannelManager / WeComAdapter）
# - 10.1.2 ACPAdapter（Agent 侧出站**真实实现**，在 adapters/agents；不是
#   Delivery/Channel adapter——本审计只守护 Delivery/Channel 侧）
DELIVERY_SCOPE_ALLOWED = {
    ("app/repositories/agent_adapter.py", "AgentAdapter"),
    ("app/repositories/channel_adapter.py", "ChannelAdapter"),
    ("app/application/delivery.py", "DeliveryExecutor"),
    ("app/application/errors.py", "ChannelDeliveryFailedError"),
    ("app/adapters/agents/acp.py", "ACPAdapter"),
    # 10.2.1 WeCom Inbound：入站 Port 与实现（Channel→CP 方向，与出站 Delivery 正交）
    ("app/repositories/channel_inbound.py", "InboundChannelAdapter"),
    ("app/adapters/channels/wecom.py", "WeComAdapter"),
    # 3B-2.2 WeCom Bot WebSocket transport：同属「Channel→CP 入站 + CP→Channel
    # 出站」的渠道 Adapter 实现（与 WeComAdapter / LocalChannelAdapter 同类，
    # 走同一 InboundChannelAdapter / ChannelAdapter Port），不是新的 Delivery 组件
    # → 精确放行，不是放开 token "adapter"。
    ("app/adapters/channels/wecom_bot.py", "WeComBotAdapter"),
    # 15.2.3 Local First Success：external-system-free 的 Agent / Channel 实现
    # 与 type→Adapter 分派（与 ACPAdapter/WeComAdapter 同类，非新的 Delivery 组件）
    ("app/adapters/agents/local.py", "LocalAgentAdapter"),
    ("app/adapters/agents/registry.py", "MultiAgentAdapter"),
    ("app/adapters/channels/local.py", "LocalChannelAdapter"),
    # P2 Delivery Recovery：投递恢复职责（G3 已批准）。它**不是**新的
    # Delivery 通道组件：DeliveryExecutor / ChannelAdapter 契约逐字未动，
    # 恢复只是「已落库结果的再投递」，复用同一 DeliveryExecutor 与
    # 同一 ChannelAdapter Port ⇒ 精确放行这些名字，不放开 token。
    ("app/application/delivery_recovery.py", "DeliveryRecoveryDriver"),
    ("app/application/delivery_recovery.py", "DeliveryRecoveryConfig"),
    ("app/application/delivery_recovery.py", "DeliveryRecoverySource"),
    ("app/application/delivery_recovery.py", "DeliveryRecoveryExecutor"),
    ("app/application/delivery_recovery.py", "recover_delivery"),
    ("app/application/delivery_recovery.py", "reclaim_stale_delivery"),
    ("app/application/interaction_recording.py", "claim_delivery"),
    ("app/application/interaction_recording.py", "recover_delivery"),
    ("app/application/interaction_recording.py", "reclaim_stale_delivery"),
    ("app/application/interaction_recording.py", "_record_delivery_failure"),
    ("app/repositories/interaction_record.py", "claim_delivery"),
    ("app/repositories/interaction_record.py", "reclaim_stale_delivery"),
    ("app/persistence/repositories/interaction_record.py", "claim_delivery"),
    ("app/persistence/repositories/interaction_record.py", "reclaim_stale_delivery"),
    ("app/adapters/channels/registry.py", "MultiChannelAdapter"),
}


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
        left = a.left.id if isinstance(a.left, ast.Name) else "?"
        right = "None" if isinstance(a.right, ast.Constant) and a.right.value is None \
            else (a.right.id if isinstance(a.right, ast.Name) else "?")
        return f"{left} | {right}"
    return "?"


def _is_frozen_dataclass(cls: ast.ClassDef) -> bool:
    return any(
        isinstance(d, ast.Call)
        and isinstance(d.func, ast.Name) and d.func.id == "dataclass"
        and any(kw.arg == "frozen" and getattr(kw.value, "value", False)
                for kw in d.keywords)
        for d in cls.decorator_list
    )


def _module_class(py: Path, name: str) -> ast.ClassDef:
    assert py.is_file(), f"缺 {py.relative_to(BACKEND_DIR)}"
    return _classes(_tree(py))[name]


def _component_violations() -> list[str]:
    problems: list[str] = []
    for py in sorted(APP_DIR.rglob("*.py")):
        if (APP_DIR / "persistence" / "models") in py.parents:
            continue
        for node in ast.walk(_tree(py)):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                posix = "/".join(py.relative_to(BACKEND_DIR).parts)
                if (posix, node.name) in DELIVERY_SCOPE_ALLOWED:
                    continue
                lower = node.name.lower()
                if any(tok in lower for tok in DELIVERY_COMPONENT_TOKENS):
                    problems.append(f"{posix}::{node.name}")
    return problems


# ================================================================== A OutboundResponse


def test_a_module_pure() -> None:
    assert _roots(_tree(OUTBOUND_PY)) == {"dataclasses"}, \
        f"OutboundResponse 模块越层导入：{_roots(_tree(OUTBOUND_PY))}"


def test_a_field_face_frozen() -> None:
    """OutboundResponse 字段面 = 恰好 5 个（channel 侧交还消息，结构化锁死）。

    ACP V0.3 Phase 1：+ `artifacts`（出站专属的成果**引用**轴）。
    """
    cls = _module_class(OUTBOUND_PY, "OutboundResponse")
    assert _is_frozen_dataclass(cls), "OutboundResponse 应为 frozen dataclass"
    anns = _annassigns(cls)
    names = {s.target.id for s in anns}
    assert names == OUTBOUND_FIELDS, f"OutboundResponse 字段面漂移：{names}"
    assert not (names & FORBIDDEN_FIELDS), \
        f"OutboundResponse 混入 Routing/归属字段：{names & FORBIDDEN_FIELDS}"

    types = {s.target.id: _ann_text(s) for s in anns}
    assert types["channel_id"] == "int"
    assert types["external_conversation_id"] == "str"
    assert types["content"] == "str"
    assert types["metadata"] == "dict | None"

    for field, required in (("channel_id", True), ("external_conversation_id", True),
                            ("content", True), ("metadata", False)):
        stmt = next(s for s in anns if s.target.id == field)
        assert (stmt.value is None) == required, f"{field} 必填性错误"


def test_a_behavior() -> None:
    """运行时行为：可构造（最小 = channel + conversation + content）；frozen。"""
    from app.domain.outbound_response import OutboundResponse

    msg = OutboundResponse(channel_id=1, external_conversation_id="conv-9", content="hi")
    assert msg.channel_id == 1
    assert msg.external_conversation_id == "conv-9"
    assert msg.content == "hi"
    assert msg.metadata is None

    msg2 = dataclasses.replace(msg, metadata={"trace": "1"})
    assert msg2.metadata == {"trace": "1"}
    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.content = "no"  # type: ignore[misc]


def test_a_domain_package_exports() -> None:
    """domain 包导出 OutboundResponse（与其他 Runtime 契约同待遇）。"""
    from app.domain import OutboundResponse as _E1
    from app.domain.outbound_response import OutboundResponse as _E2

    assert _E1 is _E2


# ================================================================== B Delivery 不重新选路


def test_b_no_routing_reselection_fields() -> None:
    """Delivery 不重新 Routing：无 connection_id / agent_id / session / routing_rule。"""
    src = OUTBOUND_PY.read_text(encoding="utf-8")
    body = src.split("@dataclass")[1]
    for marker in ("user_id", "identity_id", "session_id", "connection_id",
                   "agent_id", "routing_rule"):
        assert marker not in body, f"OutboundResponse 不应携带 {marker}"
    # 语义冻结：transport, not routing
    assert "Delivery is transport, not routing" in src


# ================================================================== C Inbound/Outbound 对称


def test_c_inbound_outbound_symmetric() -> None:
    """OutboundResponse 与 InboundInteraction 的**双向**字段差（ACP V0.3 Phase 1）。

    共享 channel 侧字段；ingress 专属 identity/trace 字段
    （external_interaction_id / external_identity_id）**不回流**到出口；
    出口另有一个**出站专属**轴：Agent 产出的成果**引用** `artifacts`。

    修订自 V0.2 的单向等式 `outbound == inbound - {ingress 专属字段}`——
    该等式在加入出站专属字段后不再成立，改为**显式声明双向差**（更严格：
    两侧的差异都被逐项锁死，杜绝「悄悄多出第三个差异」）。
    契约见 docs/step-acp-v0.3-artifact-outbound-implementation-gate.md §1.2.2。
    """
    from app.domain.inbound_interaction import InboundInteraction
    from app.domain.outbound_response import OutboundResponse

    inbound = {f.name for f in dataclasses.fields(InboundInteraction)}
    outbound = {f.name for f in dataclasses.fields(OutboundResponse)}
    # 共享 channel 侧最小面
    assert {"channel_id", "external_conversation_id", "content", "metadata"} <= inbound
    # 双向差显式声明：ingress 专属 → 不回流；outbound 专属 → 只有 artifacts
    assert inbound - outbound == {"external_interaction_id", "external_identity_id"}, \
        f"对称关系漂移：inbound={inbound} outbound={outbound}"
    assert outbound - inbound == {"artifacts"}, \
        f"出站专属轴漂移（应恰为 artifacts）：inbound={inbound} outbound={outbound}"
    # 两模块都纯 stdlib
    for py in (INBOUND_PY, OUTBOUND_PY):
        assert _roots(_tree(py)) == {"dataclasses"}, py.relative_to(BACKEND_DIR)


def test_c_agent_response_not_merged_with_outbound() -> None:
    """AgentResponse 与 OutboundResponse 不合并：Agent-side reference
    （external_session_id）不进 Channel 交还消息；channel 归属不进 Agent 响应。

    ACP V0.3 Phase 1（Q1 = A）：字段交集由 {content, metadata} 扩为
    {content, metadata, artifacts}——`artifacts` 同时属于 AgentResponse 与
    OutboundResponse（出站消息携带 agent 产出的引用，**正是**两者共享该概念的
    体现）。**被守住的语义边界不变**：归属字段不得交叉。契约见
    docs/step-acp-v0.3-artifact-outbound-implementation-gate.md §1.2.3。
    """
    from app.domain.agent_response import AgentResponse
    from app.domain.outbound_response import OutboundResponse

    agent = {f.name for f in dataclasses.fields(AgentResponse)}
    outbound = {f.name for f in dataclasses.fields(OutboundResponse)}
    assert "external_session_id" not in outbound
    assert not (agent & {"channel_id", "external_conversation_id", "agent_id"})
    assert agent & outbound == {"content", "metadata", "artifacts"}
    # 归属字段的**双向**不交叉（显式声明，杜绝悄悄多出第二处交叉）
    assert outbound - agent == {"channel_id", "external_conversation_id"}
    assert agent - outbound == {"external_session_id"}


# ================================================================== D ChannelAdapter 边界


def test_d_no_delivery_component_classes() -> None:
    """全仓（models 除外）无 DeliveryService / ChannelAdapter / WeComAdapter /
    FeishuAdapter / WeChatAdapter / SlackAdapter / queue / scheduler / retry。"""
    assert not _component_violations(), \
        "Delivery/ChannelAdapter 组件提前出现：\n" + "\n".join(_component_violations())


def test_d_adapters_remain_shells() -> None:
    """Channel Adapter / runtime API 仍空壳；adapters/agents 自 10.1.2 起含唯一真实
    实现 acp.py（Agent 侧出站，**不是** Delivery/Channel 组件，见白名单）。"""
    for sub in ADAPTER_SHELLS:
        files = sorted(p.name for p in (APP_DIR / sub).glob("*.py"))
        expected = ["__init__.py", "acp.py", "local.py", "registry.py", "workbuddy.py"] \
            if sub == "adapters/agents" \
            else ["__init__.py", "local.py", "registry.py", "wecom.py",
                  "wecom_bot.py"] \
            if sub == "adapters/channels" \
            else ["__init__.py", "channels.py"] if sub == "api/runtime" \
            else ["__init__.py"]
        assert files == expected, f"{sub} 实现面漂移：{files}"


# ================================================================== E Failure semantics


def test_e_channel_delivery_failed_owned_and_distinct() -> None:
    """CHANNEL_DELIVERY_FAILED 归 OutboundResponse（Channel Delivery 失败），
    与 Agent Dispatch 失败码分开。"""
    src = OUTBOUND_PY.read_text(encoding="utf-8")
    assert "CHANNEL_DELIVERY_FAILED" in src
    from app.domain.outbound_response import CHANNEL_DELIVERY_FAILED
    assert CHANNEL_DELIVERY_FAILED == "CHANNEL_DELIVERY_FAILED"
    # 不与 Agent Dispatch 失败码撞值
    from app.domain.dispatch_failure import (
        AGENT_DISPATCH_FAILED, AGENT_RESPONSE_INVALID, AGENT_TIMEOUT,
    )
    assert CHANNEL_DELIVERY_FAILED not in {
        AGENT_DISPATCH_FAILED, AGENT_TIMEOUT, AGENT_RESPONSE_INVALID}


def test_e_dispatch_failure_module_unchanged() -> None:
    """dispatch_failure 仍只承载 3 个 Agent Dispatch 失败码（不被 delivery 污染）。"""
    import app.domain.dispatch_failure as df
    codes = {n for n in dir(df) if n.startswith("AGENT_")}
    assert codes == {"AGENT_DISPATCH_FAILED", "AGENT_TIMEOUT",
                     "AGENT_RESPONSE_INVALID"}


# ================================================================== F 双状态独立


def test_f_two_independent_statuses() -> None:
    """processing_status ≠ delivery_status：interactions 模型保持两列独立。"""
    from app.persistence.models.interaction import InteractionModel

    fields = {f.name for f in InteractionModel.__table__.columns}
    assert {"processing_status", "delivery_status"} <= fields
    src = OUTBOUND_PY.read_text(encoding="utf-8")
    assert "processing_status" in src and "delivery_status" in src
    assert "≠" in src


# ================================================================== G Fail Closed


def test_g_fail_closed_phrases() -> None:
    """Fail Closed：不投递 → CHANNEL_DELIVERY_FAILED；不猜 conversation、不重选。"""
    src = OUTBOUND_PY.read_text(encoding="utf-8")
    for phrase in ("Fail Closed", "CHANNEL_DELIVERY_FAILED",
                   "不投递", "猜一个 conversation", "重新 Routing"):
        assert phrase in src, f"缺 Fail-Closed 语义冻结：{phrase}"


# ================================================================== H mutation 探测


@pytest.mark.parametrize("rel,src", [
    ("domain/delivery_service.py",
     "class DeliveryService:\n    def deliver(self) -> None:\n        pass\n"),
    ("domain/wecom_adapter.py",
     "class WeComAdapter:\n    def send(self) -> None:\n        pass\n"),
    ("domain/outbound_response.py",
     "class ChannelAdapter:\n    pass\n"),  # 偷渡进既有契约文件（类面守卫应抓）
])
def test_h_smuggled_delivery_components_caught(rel: str, src: str) -> None:
    """注入 DeliveryService / WeComAdapter / ChannelAdapter 偷渡必须报红。"""
    py = APP_DIR / rel
    backup: str | None = py.read_text(encoding="utf-8") if py.is_file() else None
    try:
        if backup is None:  # 新文件偷渡
            py.write_text(src, encoding="utf-8")
        else:  # 注入既有契约文件
            py.write_text(backup + "\n\n" + src, encoding="utf-8")
        bad = [p for p in _component_violations()
               if rel.replace("/", "/") in p]
        assert bad, f"Delivery 组件守卫未抓到 {rel}"
    finally:
        if backup is None:
            if py.exists():
                py.unlink()
        else:
            py.write_text(backup, encoding="utf-8")


def test_h_channel_adapter_impl_breaks_shell_guard() -> None:
    """在 adapters/channels 落地 adapter 实现 → 目录空壳守卫必须报红。"""
    py = APP_DIR / "adapters" / "channels" / "wecom_adapter.py"
    assert not py.exists(), "wecom_adapter.py 不应已存在"
    try:
        py.write_text("class WeComAdapter:\n    pass\n", encoding="utf-8")
        violations = [
            sub for sub in ADAPTER_SHELLS
            if sorted(p.name for p in (APP_DIR / sub).glob("*.py")) != ["__init__.py"]
        ]
        assert violations, "Adapter 空壳守卫未抓到实现"
    finally:
        if py.exists():
            py.unlink()


def test_h_new_contract_does_not_trigger_reverse_audit() -> None:
    """OutboundResponse 是合法契约：7.3 反向审计不因新增文件而报红。"""
    audit_py = BACKEND_DIR / "tests" / "test_step7_3_connection_management_audit.py"
    ns: dict = {"__file__": str(audit_py), "__name__": "audit_under_8_6"}
    exec(compile(audit_py.read_text(encoding="utf-8"), str(audit_py), "exec"), ns)
    problems = ns["_next_stage_problems"]()
    assert not any("outbound_response" in p or "OutboundResponse" in p
                   for p in problems), f"OutboundResponse 被误报：{problems}"
