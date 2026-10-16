"""Cloud-0.1.3 —— Connect Channel Architecture Gate（机器化护栏）.

设计 + 影响审计，不实现。静态 + 行为护栏，当前即可绿灯，锁死 9 个裁决（G1–G9）：

    G1  Channel 是「入口身份/配置」实体，不是 Connection
    G2  WeCom 凭据 ownership 裁决：Channel↔WeCom 平台凭据 → Channel.config
        （不是 Connection、不是 Agent、不是第二套 credential_ref）
    G3  Channel ↔ Connection ↔ Agent 三实体关系裁决
    G4  Webhook / Long Connection 是 transport，不污染 Channel.status
    G5  Channel 三轴：status（纯管理态）/ health（派生不持久）/ interaction（在 Interaction）
    G6  WeCom Authentication 与 Browser Authentication 两个信任边界绝不合并
    G7  Webhook 路由 channel_id 仅是定位；tenant 取自「签名校验后的 Channel.tenant_id」
    G8  Connect Channel 复用既有端点 / Adapter，不新增
    G9  禁止第二套 Integration / Channel / Tenant / Health 模型

与 Cloud-0.1.2.2 护栏同一纪律：只读审计 + 机器锁定；不启动 DB、不做实现。
护栏采用 tokenize 后扫描真实标识符，避免拒斥性 docstring 产生假阳性。
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.adapters.channels.wecom import __file__ as _WECOM_FILE  # 仅取路径
from app.application.channels import (
    ALLOWED_CHANNEL_TYPES,
    ChannelApplicationService,
)
from app.application.channel_inbound import ChannelInboundService
from app.application.connections import ConnectionApplicationService
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.interaction_record import InteractionRecord
from app.persistence.models.channel import ChannelModel
from app.persistence.models.connection import ConnectionModel
from app.schemas.channels import ChannelResponse, sanitize_channel_config
from app.schemas.connections import ConnectionResponse

_BACKEND_APP = Path(__file__).resolve().parents[1] / "app"

# 第二套模型 / 凭据平台的禁止类（类名精确匹配；docstring 误报已被 AST 排除）。
_FORBIDDEN_CLASSES = frozenset({
    "WeComIntegration", "ChannelIntegration", "WeComConnector", "CloudChannel",
    "WeComChannel", "ChannelHealthState", "OnboardingState", "WeComConnection",
    "CloudConnector", "Integration", "Connector", "SecretManager",
    "CredentialVault", "OAuthCredential", "CredentialProvider",
})

_APP_CLASS_NAMES: frozenset[str] | None = None


def _app_class_names() -> frozenset[str]:
    global _APP_CLASS_NAMES
    if _APP_CLASS_NAMES is not None:
        return _APP_CLASS_NAMES
    names: set[str] = set()
    for path in _BACKEND_APP.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
    _APP_CLASS_NAMES = frozenset(names)
    return _APP_CLASS_NAMES


# ====================================================================== G1

def test_channel_domain_is_identity_config_not_connection() -> None:
    """G1：Channel 是「入口身份/配置」实体：name/type/status/config，无 endpoint、
    无 channel_id/agent_id 绑定（那是 Connection 的事）。"""
    fields = set(Channel.__dataclass_fields__)
    assert fields == {"name", "type", "id", "status", "config"}
    # 入口实体不携带 Access Path 绑定，也不携带 endpoint
    assert "channel_id" not in fields
    assert "agent_id" not in fields
    assert "endpoint" not in fields
    assert "credential_ref" not in fields  # G2：凭据走 config，不造第二引用字段


def test_channel_model_has_no_plaintext_secret_column() -> None:
    """G1/G2：channels 表无明文凭据列（凭据只在 config JSON 内，读时脱敏）。"""
    plaintext = ("secret", "password", "token", "api_key")
    cols = [c.name for c in ChannelModel.__table__.columns]
    flagged = [c for c in cols if any(m in c for m in plaintext)]
    assert flagged == [], f"明文凭据列出现：{flagged}"
    # config 是唯一的凭据容器；tenant_id 是隔离根（不是凭据）
    assert "config" in cols
    assert "tenant_id" in cols


# ====================================================================== G2

def test_wecom_credentials_live_in_channel_config_not_connection() -> None:
    """G2（核心裁决）：WeCom 平台凭据只从 Channel.config 取，绝不从 Connection。"""
    src = Path(_WECOM_FILE).read_text(encoding="utf-8")
    # 入站材料（token/encoding_aes_key/corp_id）来自 channel.config
    assert "_material" in src and "channel.config" in src
    assert "token" in src and "encoding_aes_key" in src and "corp_id" in src
    # 出站材料（corpsecret/agentid/api_base/timeout_s）同样来自 channel.config
    assert "_outbound_material" in src
    assert "corpsecret" in src and "agentid" in src
    # 关键红线：WeCom Adapter 不得从 Connection 取平台凭据
    assert "connection.credential_ref" not in src
    assert "connection.config" not in src
    # 凭据确实从 channel.config 取出（_material 经 _require_str 读取 token；
    # _outbound_material 经内部 req("corpsecret") 读取出站平台凭据）
    assert '_require_str(config, "token"' in src
    assert 'req("corpsecret")' in src


def test_wecom_credentials_are_redacted_on_channel_config_read() -> None:
    """G2：Channel.config 敏感键读时脱敏（token/encoding_aes_key/corpsecret），
    非敏感键（corp_id/agentid/api_base/timeout_s）原样可见，与 Connection 同一纪律。"""
    out = sanitize_channel_config({
        "token": "t0k", "encoding_aes_key": "k3y", "corpsecret": "sec",
        "corp_id": "cid", "agentid": 1000002, "api_base": "https://x",
        "timeout_s": 10,
    })
    assert out is not None
    assert out["token"] == "********"
    assert out["encoding_aes_key"] == "********"
    assert out["corpsecret"] == "********"
    assert out["corp_id"] == "cid"          # 非敏感：管理可见
    assert out["agentid"] == 1000002
    assert out["api_base"] == "https://x"
    assert out["timeout_s"] == 10
    assert sanitize_channel_config(None) is None


def test_channel_has_no_credential_ref_field() -> None:
    """G2：Channel 不引入 credential_ref 第二引用字段（与 Agent 侧不同，
    WeCom 平台凭据直接住 Channel.config）。"""
    fields = set(Channel.__dataclass_fields__)
    assert "credential_ref" not in fields
    # 响应面同样不含 credential_ref（凭据不出）
    assert "credential_ref" not in ChannelResponse.model_fields


def test_connection_holds_acp_path_credential_not_wecom_platform_credential() -> None:
    """G2/G3：Connection 承载的是 Channel↔Agent 访问路径凭据（bearer_token 等），
    WeCom 平台凭据（corpsecret/token/encoding_aes_key）在 Channel.config，
    二者语义正交，不互相污染。"""
    conn_fields = set(Connection.__dataclass_fields__)
    assert "channel_id" in conn_fields and "agent_id" in conn_fields
    # Connection 自身不重复持有平台级 WeCom 凭据字段名
    assert "corpsecret" not in conn_fields
    assert "encoding_aes_key" not in conn_fields
    # 响应面：Connection 仍只暴露 access-path 字段，不含平台凭据明文
    assert "bearer_token" not in ConnectionResponse.model_fields


# ====================================================================== G3

def test_channel_and_connection_are_separate_entities() -> None:
    """G3：Channel 与 Connection 独立域类；Channel 不含绑定，Connection 含绑定。"""
    assert ChannelApplicationService is not None
    assert ConnectionApplicationService is not None
    channel_fields = set(Channel.__dataclass_fields__)
    conn_fields = set(Connection.__dataclass_fields__)
    # Channel 不是 Access Path
    assert "channel_id" not in channel_fields
    assert "agent_id" not in channel_fields
    # Connection 是 Access Path（绑定），且绑定是非身份字段
    assert "channel_id" in conn_fields
    assert "agent_id" in conn_fields
    assert "name" in channel_fields  # Channel 是入口身份


def test_connection_create_requires_pre_existing_channel_and_agent() -> None:
    """G3：Connection 创建依赖已存在的 Channel 与 Agent（两事实顺序依赖，静态确认）。"""
    src = (Path(__file__).resolve().parents[1] / "app" / "application"
           / "connections.py").read_text(encoding="utf-8")
    assert "ChannelNotFoundError" in src
    assert "AgentNotFoundError" in src
    assert "find_by_channel_and_agent" in src
    assert "uow.channels.get_by_id" in src
    assert "uow.agents.get_by_id" in src


# ====================================================================== G4

def test_webhook_transport_is_not_a_channel_status_axis() -> None:
    """G4：Webhook / Long Connection 是 transport，绝不污染 Channel.status。
    Channel 域无 webhook/longconn/transport 连接态字段。"""
    fields = set(Channel.__dataclass_fields__)
    transport_axes = {
        "webhook_status", "longconn_status", "transport_status",
        "connected", "disconnected", "webhook_connected",
    }
    assert not (transport_axes & fields)
    # webhook 端点是 runtime intake/deployment concern，不在 Channel 域状态里
    assert "endpoint" not in fields


def test_channel_status_default_is_active_no_runtime_connectivity() -> None:
    """G4/G5：Channel.status 默认 "active"，表达「该入口是否启用」，不含任何
    运行态连接语义（connected/disconnected/healthy/unreachable）。"""
    assert Channel.__dataclass_fields__["status"].default == "active"
    inbound_src = (Path(__file__).resolve().parents[1] / "app" / "application"
                   / "channel_inbound.py").read_text(encoding="utf-8")
    # 入站只把 status 当「是否启用」开关（!= active → 拒绝），不表达连接态
    assert 'channel.status != "active"' in inbound_src


# ====================================================================== G5

def test_channel_has_no_health_subsystem() -> None:
    """G5：V0 不需要新的 Channel Health subsystem。Channel 域无 health 字段，
    全 app 无 ChannelHealthState 类。"""
    fields = set(Channel.__dataclass_fields__)
    assert "health" not in fields
    assert "runtime_health" not in fields
    assert "last_health" not in fields
    classes = _app_class_names()
    assert "ChannelHealthState" not in classes


def test_interaction_outcome_lives_on_interaction_not_channel() -> None:
    """G5：Channel 不吸收交互结果；processing_status/delivery_status 在 Interaction。"""
    interaction_fields = set(InteractionRecord.__dataclass_fields__)
    assert "processing_status" in interaction_fields
    assert "delivery_status" in interaction_fields
    channel_fields = set(Channel.__dataclass_fields__)
    assert "processing_status" not in channel_fields
    assert "delivery_status" not in channel_fields


# ====================================================================== G6

def test_wecom_auth_isolated_from_browser_auth() -> None:
    """G6：WeCom Authentication 与 Browser Authentication 是两个信任边界，
    WeComAdapter 绝不 import browser auth / cookie / session 体系。"""
    src = Path(_WECOM_FILE).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imported_modules.add(mod)
    # 红线：WeCom 验签/解密不得依赖浏览器认证模块
    assert not any("browser_auth" in m for m in imported_modules)
    assert not any("app.api.auth" == m for m in imported_modules)
    # WeCom 认证事实只来自 channel + 入站参数（adapter 内部），不来自用户会话
    assert "signature" in src and "verify_handshake" in src


# ====================================================================== G7

def test_webhook_channel_id_is_routing_only_not_auth_or_tenant() -> None:
    """G7：URL 中的 channel_id 仅是路由定位；真正的认证是 WeCom 签名校验，
    绝不从 URL 推出 tenant/authentication。入站服务只把 channel_id 当 locator。"""
    inbound_src = (Path(__file__).resolve().parents[1] / "app" / "application"
                   / "channel_inbound.py").read_text(encoding="utf-8")
    # _load_channel 只用 channel_id 做 get_by_id + status 校验（定位），不推 tenant/认证
    assert "uow.channels.get_by_id(channel_id)" in inbound_src
    assert 'channel.status != "active"' in inbound_src
    # 认证委托给 Adapter（签名校验），不是 URL/tenant
    assert "self._adapter.verify_handshake" in inbound_src
    assert "self._adapter.parse_message" in inbound_src


def test_tenant_resolution_derived_from_validated_channel_not_url() -> None:
    """G7（设计原则锁死）：tenant 取自「签名校验后的 Channel.tenant_id」，
    绝不取自 URL（TenantResolver 端口契约）。"""
    tenant_src = (Path(__file__).resolve().parents[1] / "app" / "core"
                  / "tenant.py").read_text(encoding="utf-8")
    assert "绝不取自 URL" in tenant_src
    assert "Channel.tenant_id" in tenant_src
    # 反向红线：tenant 只能来自已认证 Channel 对象（find_tenant_id_by_id(channel.id)），
    # 不得来自 URL/路径参数——channel_inbound 里没有把 URL 参数直接当 tenant 的逻辑。
    inbound_src = (Path(__file__).resolve().parents[1] / "app" / "application"
                   / "channel_inbound.py").read_text(encoding="utf-8")
    assert "find_tenant_id_by_id(channel.id)" in inbound_src


# ====================================================================== G8

def test_connect_channel_reuses_existing_endpoints_and_adapter() -> None:
    """G8：Connect Channel 复用既有端点 / Adapter，不新增第二套。
    - type="wecom" 已在受控集合；
    - 既有 POST /api/channels 接受 {name,type,config}（config 含 WeCom 平台凭据）；
    - WeComAdapter 已实现双端口（入站 + 出站）；
    - 无 WeComChannel / CloudWeCom 专属类。"""
    assert "wecom" in ALLOWED_CHANNEL_TYPES
    classes = _app_class_names()
    assert "WeComChannel" not in classes
    assert "CloudWeCom" not in classes
    # 既有 Webhook 端点前缀（runtime intake）—— 复用，不新建
    runtime_src = (Path(__file__).resolve().parents[1] / "app" / "api" / "runtime"
                   / "channels.py").read_text(encoding="utf-8")
    assert "/{channel_id}/wecom/webhook" in runtime_src


# ====================================================================== G9

def test_no_second_channel_or_integration_model() -> None:
    """G9：backend 真实代码标识符中无第二套 Integration / Channel / Health 模型类。"""
    classes = _app_class_names()
    forbidden = {
        "WeComIntegration", "ChannelIntegration", "WeComConnector",
        "CloudChannel", "WeComChannel", "ChannelHealthState",
        "OnboardingState", "WeComConnection", "CloudConnector",
        "Integration", "Connector", "SecretManager", "CredentialVault",
        "OAuthCredential", "CredentialProvider",
    }
    assert not (forbidden & classes)
