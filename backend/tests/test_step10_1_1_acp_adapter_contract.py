"""Step 10.1.1 —— ACP Adapter Contract Discovery（第一刀：真实 Agent Adapter 前哨）。

10.1 选择 ACPAdapter 作为第一个真实 Agent Adapter。10.1.1 **先不实现 ACP**，只做
一件事：验证 Frozen `AgentAdapter` Port 是否足以承载一个真实 Agent Protocol。

    Runtime Kernel → AgentAdapter Port → [ACPAdapter(未来)] → ACP Agent

核心裁决（Protocol-specific complexity belongs inside Adapter）：

    Agent 选择（Routing 做）           / Connection（CP 持有）
    credential_ref（CP 持有）          / Control Plane Session（Runtime）
    → CP 概念，AgentRequest/Connection 承载
    ACP transport / auth / serialization / session protocol / streaming /
    response normalization（Adapter 做）→ 协议内部细节，绝不漏进 CP 契约字段

本步发现：仓库内 ACP = 对下游 type="acp" Agent 的 HTTP 通信协议，无具体 wire spec
→ 协议细节以「问题清单」记录，10.1.2 连真实 ACP Agent 再确认。本步不新增生产文件
（acp.py 推迟到 10.1.2），Frozen Contract 全部未改动。

锁定项：
A  Frozen 契约形状未变（AgentRequest / AgentResponse / OutboundResponse / Connection）；
B  AgentAdapter Port 仍是 send(request, connection)，无新方法；
C  协议内部概念（transport/auth/capability/negotiation/skill/stream/endpoint/…）
   没有泄漏进 CP 契约字段；
D  Responsibility Map 自洽（每个 concern 恰好一个 owner；owner ∈ 白名单）；
E  AgentRequest + Connection 两个输入足以承载映射（credential/config/external
   session 回流通道都存在）；
F  10.1.1 阶段不实现；ACPAdapter 由 10.1.2 落地在唯一归属
   app/adapters/agents/acp.py（channels 仍空壳；F 测试即守护该归属）。
"""

import ast
import dataclasses
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]

# 用户冻结的 Responsibility Map（10.1.1 的交付物，机器化）
RESPONSIBILITY_MAP = {
    "agent_selection": "routing",                    # Routing 决定去哪个 Agent
    "connection": "control_plane",                  # Access Path 由 CP 持有/读取
    "credential_ref": "control_plane",              # 凭据引用由 CP 持有（不解析）
    "acp_authentication": "acp_adapter",            # 协议鉴权留在 Adapter
    "acp_transport": "acp_adapter",                 # HTTP/连接管理留在 Adapter
    "acp_serialization": "acp_adapter",             # 协议序列化留在 Adapter
    "acp_session_protocol": "acp_adapter",          # ACP 侧会话协议留在 Adapter
    "control_plane_session": "runtime",             # CP Session 由 Runtime 管理
    "agent_external_session": "acp_adapter_or_agent",  # Agent 侧 session 引用
    "agent_response_normalization": "acp_adapter",  # 归一化为 AgentResponse
    "delivery": "delivery_executor",                # 交还 Channel
}
ALLOWED_OWNERS = {"routing", "control_plane", "acp_adapter",
                  "acp_adapter_or_agent", "runtime", "delivery_executor"}

# Adapter 内部协议概念：若出现在 CP 契约字段面 = 泄漏（原则被破坏）
PROTOCOL_INTERNAL_BAN = (
    "transport", "headers", "auth", "capability", "negotiation", "skill",
    "stream", "endpoint", "protocol", "websocket", "initialize", "connect",
)


def _field_names(cls) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


def _module_roots(rel: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse((BACKEND_DIR / rel).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


# ================================================================== A Frozen 契约未变


def test_frozen_contract_shapes_unchanged() -> None:
    from app.domain.agent_request import AgentRequest
    from app.domain.agent_response import AgentResponse
    from app.domain.outbound_response import OutboundResponse
    from app.domain.connection import Connection

    assert _field_names(AgentRequest) == {
        "agent_id", "control_plane_session_id", "content",
        "external_session_id", "metadata"}
    assert _field_names(AgentResponse) == {
        "content", "external_session_id", "metadata", "artifacts"}
    assert _field_names(OutboundResponse) == {
        "channel_id", "external_conversation_id", "content", "metadata",
        "artifacts"}
    assert {"channel_id", "agent_id", "credential_ref", "config"} <= \
        _field_names(Connection)


# ================================================================== B Port 未变


def test_agent_adapter_port_unchanged() -> None:
    import app.repositories.agent_adapter as port

    tree = ast.parse(Path(port.__file__).read_text(encoding="utf-8"))
    methods = {n.name for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name != "__init__"}
    assert methods == {"send"}, methods
    assert "Protocol" in Path(port.__file__).read_text(encoding="utf-8")
    # Port 只依赖 domain（agent_request/agent_response/connection），无协议细节
    roots = _module_roots("app/repositories/agent_adapter.py")
    assert roots <= {"app.domain.agent_request", "app.domain.agent_response",
                     "app.domain.connection", "typing"}, roots


def test_send_signature_still_request_plus_connection() -> None:
    from app.repositories.agent_adapter import AgentAdapter

    sig = AgentAdapter.send
    assert list(sig.__annotations__) == ["request", "connection", "return"]
    assert sig.__annotations__["return"].__name__ == "AgentResponse"


# ================================================================== C 协议细节不泄漏


def test_protocol_internals_not_leaked_into_cp_fields() -> None:
    from app.domain.agent_request import AgentRequest
    from app.domain.agent_response import AgentResponse
    from app.domain.connection import Connection
    from app.domain.inbound_interaction import InboundInteraction
    from app.domain.outbound_response import OutboundResponse

    for contract in (AgentRequest, AgentResponse, OutboundResponse,
                     Connection, InboundInteraction):
        names = {f.name.lower() for f in dataclasses.fields(contract)}
        leaked = {b for b in PROTOCOL_INTERNAL_BAN
                  if any(b in n for n in names)}
        assert not leaked, f"{contract.__name__} 泄漏协议概念：{leaked}"


def test_adapter_owned_concepts_not_in_request_or_response() -> None:
    """认证/传输/序列化/能力协商属于 Adapter——AgentRequest/Response 不得携带。"""
    from app.domain.agent_request import AgentRequest
    from app.domain.agent_response import AgentResponse

    def _has_concept(cls, token: str) -> bool:
        names = {f.name.lower() for f in dataclasses.fields(cls)}
        return any(token in n for n in names)

    for token in ("auth", "credential", "capabilit", "negotiat", "stream",
                  "transport", "header", "protocol"):
        assert not _has_concept(AgentRequest, token), \
            f"AgentRequest 不应携带 {token}"
        assert not _has_concept(AgentResponse, token), \
            f"AgentResponse 不应携带 {token}"


# ================================================================== D Responsibility Map 自洽


def test_responsibility_map_consistent() -> None:
    assert set(RESPONSIBILITY_MAP) == {
        "agent_selection", "connection", "credential_ref", "acp_authentication",
        "acp_transport", "acp_serialization", "acp_session_protocol",
        "control_plane_session", "agent_external_session",
        "agent_response_normalization", "delivery"}
    owners = set(RESPONSIBILITY_MAP.values())
    assert owners <= ALLOWED_OWNERS, owners


def test_protocol_complexity_stays_in_adapter() -> None:
    """原则机器化：5 项 ACP 协议内部细节全归 acp_adapter。"""
    adapter_owned = {c for c, o in RESPONSIBILITY_MAP.items()
                     if o == "acp_adapter"}
    assert adapter_owned == {"acp_authentication", "acp_transport",
                             "acp_serialization", "acp_session_protocol",
                             "agent_response_normalization"}


# ================================================================== E Port 形状足以承载映射


def test_port_inputs_carry_required_mapping_data() -> None:
    """AgentRequest + Connection 两个输入已含映射所需：routed agent / CP session /
    凭据引用 / 配置；AgentResponse 提供 external_session_id 回流通道。"""
    from app.domain.agent_request import AgentRequest
    from app.domain.agent_response import AgentResponse
    from app.domain.connection import Connection

    request_fields = _field_names(AgentRequest)
    assert "agent_id" in request_fields          # 来自 Routing（agent_selection）
    assert "control_plane_session_id" in request_fields  # CP Session（runtime）
    assert "external_session_id" in request_fields        # Agent 侧已有引用可继续传
    conn_fields = _field_names(Connection)
    assert "credential_ref" in conn_fields       # CP 持有、Adapter 用于鉴权
    assert "config" in conn_fields               # CP 持有、Adapter 用于连接
    assert "external_session_id" in _field_names(AgentResponse)  # 新 session 回流


def test_external_session_round_trip_channel_exists() -> None:
    """external_session_id 回流通道：request 可带已有引用；response 可带回新引用。"""
    from app.domain.agent_request import AgentRequest
    from app.domain.agent_response import AgentResponse

    req = AgentRequest(agent_id=1, control_plane_session_id=2,
                       content="hi", external_session_id="agent-s-prev")
    resp = AgentResponse(content="ok", external_session_id="agent-s-new")
    assert req.external_session_id == "agent-s-prev"
    assert resp.external_session_id == "agent-s-new"
    assert req.external_session_id != resp.external_session_id


# ================================================================== F 实现归属（10.1.2 起）


def test_acp_adapter_lives_only_in_designated_home() -> None:
    """10.1.1 不实现；10.1.2 落地 ACPAdapter；全仓只在 acp.py 定义 ACPAdapter。

    15.2.3 起 adapters/agents 追加 local.py（LocalAgentAdapter）+ registry.py
    （MultiAgentAdapter）；adapters/channels 追加 local.py（LocalChannelAdapter）
    + registry.py（MultiChannelAdapter）——ACPAdapter 的归属不变。
    """
    files = sorted(p.name for p in (APP_DIR / "adapters" / "agents").glob("*.py"))
    assert files == ["__init__.py", "acp.py", "local.py", "registry.py", "workbuddy.py"], files
    for sub in ("adapters/channels",):
        files = sorted(p.name for p in (APP_DIR / sub).glob("*.py"))
        assert files == ["__init__.py", "local.py", "registry.py", "wecom.py",
                         "wecom_bot.py"], \
            f"{sub} 实现面漂移：{files}"
    found = []
    for py in sorted(APP_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef) and "ACPAdapter" in node.name:
                found.append(f"{py.relative_to(BACKEND_DIR).as_posix()}::{node.name}")
    assert found == ["app/adapters/agents/acp.py::ACPAdapter"], found


def test_discovery_touched_no_runtime_files() -> None:
    """本步只增 tests/docs：dispatch/coordinator 类面与依赖面未变。"""
    import app.application.agent_dispatch as dispatch_mod
    import app.application.coordinator as coord_mod

    def _classes(module) -> set[str]:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        return {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}

    assert _classes(dispatch_mod) == {"AgentDispatchExecutor"}
    assert _classes(coord_mod) == {"RuntimeCoordinator"}
    for rel in ("app/application/agent_dispatch.py",
                "app/application/delivery.py"):
        roots = _module_roots(rel)
        assert not any(r.startswith("app.adapters") for r in roots), rel
