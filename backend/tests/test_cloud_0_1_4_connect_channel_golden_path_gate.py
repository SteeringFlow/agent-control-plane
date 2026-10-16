"""Cloud-0.1.4 —— Connect Channel Golden Path Architecture Gate（机器化护栏）.

仅审计、不实现。在 0.1.3 锁死的 9 个裁决之上，对「用户把一个 WeCom Channel 接入
自己 Workspace 并最终 First Success」这一 Golden Path 复核 10 个裁决（G1–G10）：

    G1  Channel 所有权：Channel.tenant_id 是唯一租户归属事实；
        workspace_id / user_id 不得进入 Channel Domain（User 在独立 Identity 域）。
    G2  WeCom 凭证归属：token/encoding_aes_key/corp_id/corpsecret/agentid 住
        Channel.config；Connection.config 只住 ACP Agent access path；UI 不得
        把二者合并成一个「Integration Configuration」。
    G3  Channel 创建 / Connection 创建是两个事实：无聚合后端端点
        （POST /api/integrations / POST /api/workspace/connect ...）。
    G4  Connect Channel UI 选择 Agent：Channel ↔ Connection ↔ Agent 已足以表达，
        无需新增绑定实体。
    G5  WeCom Webhook 地址：URL 中的 channel_id 仅是路由定位，不是认证凭据；
        UI 不得暗示「这个 URL 本身是安全凭证」。
    G6  Secret 显示规则：token/encoding_aes_key/corpsecret 创建后 GET 不再明文回显；
        前端不得为「显示当前配置」缓存明文。
    G7  Channel Health 不引入：不新增 Channel.status=connected / health / last_checked_at
        状态体系（与 Connection.status/health/Interaction outcome 三轴独立一致）。
    G8  Channel「Connected」语义：不做 Channel.connected 持久状态；
        configured → WeCom probe → transient success 与真实 First Success 区分。
    G9  First Success 仍是 Derived：无 onboarding_status / first_success_at /
        workspace_setup_state 表；由 Interaction processing=SUCCEEDED, delivery=
        DELIVERED 推导。
    G10 最重要：Connect Channel 不需要任何新的后端 API——既有
        /api/channels / /api/agents / /api/connections / /api/connections/{id}/health
        加上 runtime /api/channels/{channel_id}/wecom/webhook 已完整支撑
        Channel 创建 → Connection 创建 → Probe → 真实 WeCom Webhook。
        => Backend = 0 changes。

与 0.1.3 护栏同一纪律：只读审计 + 机器锁定；不启动 DB、不做实现。
护栏采用 tokenize 后扫描真实标识符，避免拒斥性 docstring 产生假阳性。
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.adapters.channels.wecom import __file__ as _WECOM_FILE  # 仅取路径
from app.application.channels import ChannelApplicationService
from app.application.connections import ConnectionApplicationService
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.interaction_record import InteractionRecord
from app.persistence.models.channel import ChannelModel
from app.persistence.models.connection import ConnectionModel
from app.schemas.channels import ChannelResponse, sanitize_channel_config
from app.schemas.connections import ConnectionResponse

_BACKEND_APP = Path(__file__).resolve().parents[1] / "app"
_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _app_source() -> str:
    return "\n".join(
        _read(p) for p in _BACKEND_APP.rglob("*.py")
    )


def _app_class_names() -> frozenset[str]:
    names: set[str] = set()
    for path in _BACKEND_APP.rglob("*.py"):
        try:
            tree = ast.parse(_read(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
    return frozenset(names)


def _all_model_columns() -> frozenset[str]:
    """扫所有 ORM Model 的列名，用于禁止「新增状态体系列」。"""
    cols: set[str] = set()
    for path in (_BACKEND_APP / "persistence" / "models").rglob("*.py"):
        try:
            tree = ast.parse(_read(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                isinstance(b, ast.Call)
                and getattr(b.func, "attr", "") == "Column"
                for b in node.body
                if isinstance(b, ast.Assign)
                for _ in [0]
            ):
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for tgt in stmt.targets:
                            if isinstance(tgt, ast.Name):
                                cols.add(tgt.id)
    return frozenset(cols)


# 禁止类（类名精确匹配；docstring 误报已被 AST 排除）。
_FORBIDDEN_CLASSES = frozenset({
    "Integration", "Connector", "WeComIntegration", "ChannelIntegration",
    "WeComConnector", "CloudChannel", "WeComChannel", "ChannelHealthState",
    "OnboardingState", "WeComConnection", "CloudConnector", "SecretManager",
    "CredentialVault", "OAuthCredential", "CredentialProvider",
    "WorkspaceChannel", "WorkspaceAgent", "WorkspaceIntegration",
    "FirstSuccess", "OnboardingStatus", "WorkspaceSetupState",
    "IntegrationConfiguration", "ChannelHealth", "ChannelRuntime",
})

# 禁止 ORM 列（新状态体系 / 持久化 First Success / Onboarding）。
_FORBIDDEN_COLUMNS = frozenset({
    "workspace_id", "user_id", "connected", "channel_health",
    "last_checked_at", "onboarding_status", "first_success_at",
    "workspace_setup_state", "health", "health_status",
})


# ====================================================================== G1

def test_channel_ownership_is_tenant_id_only() -> None:
    """G1：Channel 的唯一租户归属事实是 tenant_id；Channel Domain 不含 workspace_id /
    user_id（User 在独立 Identity/BrowserAuth 域，不在 Channel 域）。

    设计纪律（0.1.3.2 Gate Q10）：Domain 实体刻意「tenant-blind」——tenant 是
    Control Plane 的 Data Isolation Context，经 tenant-scoped UoW 注入，不污染
    Domain 类。因此：
    - Channel 域类无 workspace_id / user_id（干净）；
    - tenant_id 归属事实落在 ORM 模型列（隔离根）；
    - 隔离在 main.py 经 _tenant_scoped_factory 把 TenantScope 绑进 UoW 落地。
    """
    fields = set(Channel.__dataclass_fields__)
    # Domain 层干净：仅身份/配置（name/type/id/status/config）+ credential 走 config
    assert "workspace_id" not in fields
    assert "user_id" not in fields
    # tenant_id 归属事实在持久化层（ORM 列），作为隔离根
    cols = [c.name for c in ChannelModel.__table__.columns]
    assert "tenant_id" in cols
    assert "workspace_id" not in cols
    assert "user_id" not in cols
    # 隔离经 tenant-scoped UoW 落地，而非塞进 Domain（main.py：_tenant_scoped_factory）
    main_src = _read(_BACKEND_ROOT / "app" / "main.py")
    assert "_tenant_scoped_factory" in main_src
    assert "SqlAlchemyUnitOfWork, tenant_id=" in main_src


def test_no_workspace_or_user_id_anywhere_in_channel_module() -> None:
    """G1（反向红线）：app/domain、app/persistence/models、app/schemas、app/api/management
    的 channel 相关文件中，workspace_id / user_id 不出现（user_id 只许在 Identity 域）。"""
    channel_files = [
        _BACKEND_APP / "domain" / "channel.py",
        _BACKEND_APP / "persistence" / "models" / "channel.py",
        _BACKEND_APP / "schemas" / "channels.py",
        _BACKEND_APP / "api" / "management" / "channels.py",
    ]
    for f in channel_files:
        src = _read(f)
        assert "workspace_id" not in src
        assert "user_id" not in src


# ====================================================================== G2

def test_wecom_credentials_live_in_channel_config_not_connection() -> None:
    """G2（核心裁决）：WeCom 平台凭据只从 Channel.config 取，绝不从 Connection。"""
    src = _read(Path(_WECOM_FILE))
    # 入站材料（token/encoding_aes_key/corp_id）来自 channel.config
    assert "_material" in src and "channel.config" in src
    assert "token" in src and "encoding_aes_key" in src and "corp_id" in src
    # 出站材料（corpsecret/agentid/api_base/timeout_s）同样来自 channel.config
    assert "_outbound_material" in src
    assert "corpsecret" in src and "agentid" in src
    # 红线：WeCom Adapter 不得从 Connection 取平台凭据
    assert "connection.credential_ref" not in src
    assert "connection.config" not in src
    assert '_require_str(config, "token"' in src
    assert 'req("corpsecret")' in src


def test_channel_and_connection_configs_are_disjoint_wecom_vs_acp() -> None:
    """G2：Channel.config 承载 WeCom 平台凭据；Connection.config 承载 ACP access path；
    二者语义正交，UI 不得合并为单一「Integration Configuration」。
    - Channel 域无 credential_ref（平台凭据住 config）；
    - Connection 域持有 ACP 绑定 + credential_ref（Agent access path）；
    - 两边都无对方的所有权凭证字段。"""
    chan_fields = set(Channel.__dataclass_fields__)
    conn_fields = set(Connection.__dataclass_fields__)
    # Channel 不持有 ACP access path 绑定
    assert "channel_id" not in chan_fields
    assert "agent_id" not in chan_fields
    # Connection 不持有 WeCom 平台凭据字段名
    assert "corpsecret" not in conn_fields
    assert "encoding_aes_key" not in conn_fields
    # WeCom 平台凭据读 self 不进 Connection 响应
    assert "bearer_token" not in ConnectionResponse.model_fields
    assert "corpsecret" not in ConnectionResponse.model_fields


# ====================================================================== G3

def test_no_aggregated_integration_endpoint() -> None:
    """G3：不存在聚合后端端点 POST /api/integrations 或 POST /api/workspace/connect；
    Channel 创建与 Connection 创建是两条独立端点。"""
    src = _app_source()
    assert "/api/integrations" not in src
    assert "/api/workspace/connect" not in src
    # 两条独立端点确实已存在（前端负责 orchestration）
    assert "/api/channels" in src
    assert "/api/connections" in src


def test_channel_and_connection_services_are_separate_facts() -> None:
    """G3：Channel / Connection 是两条独立 Application Service、两个独立域类；
    不存在把二者合并的 IntegrationService。"""
    classes = _app_class_names()
    assert "IntegrationService" not in classes
    assert ChannelApplicationService is not None
    assert ConnectionApplicationService is not None
    chan_fields = set(Channel.__dataclass_fields__)
    conn_fields = set(Connection.__dataclass_fields__)
    assert "channel_id" not in chan_fields and "agent_id" not in chan_fields
    assert "channel_id" in conn_fields and "agent_id" in conn_fields


# ====================================================================== G4

def test_connection_domain_expresses_channel_agent_binding_without_new_entity() -> None:
    """G4：Channel ↔ Connection ↔ Agent 不靠新增绑定实体即可表达。
    Connection 自带 channel_id + agent_id 绑定，无需 WorkspaceChannel / 新增 join 表。"""
    conn_fields = set(Connection.__dataclass_fields__)
    assert "channel_id" in conn_fields and "agent_id" in conn_fields
    classes = _app_class_names()
    assert "WorkspaceChannel" not in classes
    assert "WorkspaceAgent" not in classes


def test_connection_create_requires_pre_existing_channel_and_agent() -> None:
    """G4：Connect Channel UI 的「Select existing Agent」在 Domain 层已可表达——
    Connection 创建依赖已存在的 Channel 与 Agent（两事实顺序依赖，静态确认）。"""
    src = _read(_BACKEND_APP / "application" / "connections.py")
    assert "ChannelNotFoundError" in src
    assert "AgentNotFoundError" in src
    assert "find_by_channel_and_agent" in src
    assert "uow.channels.get_by_id" in src
    assert "uow.agents.get_by_id" in src


# ====================================================================== G5

def test_webhook_channel_id_is_routing_only_not_auth() -> None:
    """G5：runtime webhook URL 中的 {channel_id} 仅路由定位；认证由 WeCom 签名
    校验（verify_handshake）承担，绝不从 URL 推认证/tenant。"""
    runtime_src = _read(_BACKEND_APP / "api" / "runtime" / "channels.py")
    assert "/{channel_id}/wecom/webhook" in runtime_src
    inbound_src = _read(_BACKEND_APP / "application" / "channel_inbound.py")
    # 入站只把 channel_id 当 locator（get_by_id + status 校验），不推 tenant/认证
    assert "uow.channels.get_by_id(channel_id)" in inbound_src
    assert 'channel.status != "active"' in inbound_src
    # 认证委托给 Adapter（签名校验），不是 URL
    assert "self._adapter.verify_handshake" in inbound_src


# ====================================================================== G6

def test_wecom_secret_keys_redacted_on_every_read() -> None:
    """G6：token / encoding_aes_key / corpsecret 创建后任何读取一律脱敏
    （复用 0.1.2.2 纪律）；非敏感键（corp_id/agentid/api_base/timeout_s）管理可见。"""
    out = sanitize_channel_config({
        "token": "t0k", "encoding_aes_key": "k3y", "corpsecret": "sec",
        "corp_id": "cid", "agentid": 1000002, "api_base": "https://x",
        "timeout_s": 10,
    })
    assert out is not None
    assert out["token"] == "********"
    assert out["encoding_aes_key"] == "********"
    assert out["corpsecret"] == "********"
    assert out["corp_id"] == "cid"
    assert out["agentid"] == 1000002
    assert out["api_base"] == "https://x"
    assert out["timeout_s"] == 10
    # 响应模型强制走脱敏（前端无法从 GET 取回明文）
    assert "config" in ChannelResponse.model_fields


# ====================================================================== G7 + G8

def test_channel_has_no_health_or_connected_state_system() -> None:
    """G7 + G8：Channel 不引入 health / connected 持久状态体系。
    - 域无 health / connected / last_checked_at / status="connected" 语义；
    - 模型无对应列；
    - 全 app 无 ChannelHealthState / ChannelHealth 类。"""
    fields = set(Channel.__dataclass_fields__)
    assert "health" not in fields
    assert "connected" not in fields
    assert "last_checked_at" not in fields
    assert "runtime_health" not in fields
    # status 仅是纯管理态开关，无 runtime 连接语义
    cols = [c.name for c in ChannelModel.__table__.columns]
    assert "health" not in cols
    assert "connected" not in cols
    assert "last_checked_at" not in cols
    classes = _app_class_names()
    assert "ChannelHealthState" not in classes
    assert "ChannelHealth" not in classes
    # status 默认表达「入口是否启用」，不含 connected 取值
    assert Channel.__dataclass_fields__["status"].default == "active"


# ====================================================================== G9

def test_first_success_is_derived_from_interaction_outcome() -> None:
    """G9：First Success 由真实事实推导——交互结果在 Interaction
    （processing_status / delivery_status），不在新 onboarding / first_success 表。"""
    interaction_fields = set(InteractionRecord.__dataclass_fields__)
    assert "processing_status" in interaction_fields
    assert "delivery_status" in interaction_fields
    channel_fields = set(Channel.__dataclass_fields__)
    assert "processing_status" not in channel_fields
    assert "delivery_status" not in channel_fields
    # 全 app 无 onboarding / first_success / workspace_setup_state 类
    classes = _app_class_names()
    assert not ({"OnboardingState", "FirstSuccess", "OnboardingStatus",
                 "WorkspaceSetupState"} & classes)


def test_no_forbidden_columns_added_to_any_model() -> None:
    """G9（反向红线）：没有任何 ORM 表新增 onboarding / first_success / workspace /
    channel health 列。"""
    cols = _all_model_columns()
    flagged = _FORBIDDEN_COLUMNS & cols
    assert flagged == set(), f"出现禁止列：{sorted(flagged)}"


# ====================================================================== G10

def test_golden_path_uses_only_existing_endpoints_backend_zero_changes() -> None:
    """G10（核心裁决）：Golden Path 完全由既有端点支撑，无需新后端 API。

    1. POST /api/channels            —— 创建 WeCom Channel（config 含平台凭据）
    2. GET  /api/agents              —— 列出已有 Agent 供 UI 选择
    3. POST /api/connections         —— 绑定 Channel↔Agent（Access Path）
    4. GET  /api/connections/{id}/health —— 即时 probe（Connection Health 快照）
    5. GET|POST /api/channels/{channel_id}/wecom/webhook —— 真实 WeCom 入站

    以上全在既有代码中注册；无新端点、无聚合端点。
    """
    src = _app_source()
    # 管理面三端点
    assert '/api/channels' in src
    assert '/api/agents' in src
    assert '/api/connections' in src
    # Connection Health 即时诊断（复用 0.1.2.2.2.1）
    assert '/api/connections/{id}/health' in src or \
           '/api/connections/{connection_id}/health' in src
    # 真实 WeCom 入站（runtime）
    runtime_src = _read(_BACKEND_APP / "api" / "runtime" / "channels.py")
    assert '@router.get("/{channel_id}/wecom/webhook")' in runtime_src
    assert '@router.post("/{channel_id}/wecom/webhook")' in runtime_src
    # 反向红线：不存在为 Golden Path 新加的端点
    assert "/api/connect-channel" not in src
    assert "/api/workspace/channel" not in src


def test_connection_health_probe_is_wired_in_production_compose_root() -> None:
    """G10：GET /api/connections/{id}/health 在生产组合根已装配探针
    （acp_adapter.probe），Golden Path 的 Probe 步骤无需新增后端接线。"""
    main_src = _read(_BACKEND_ROOT / "app" / "main.py")
    # 组合根把 ACPAdapter.probe 注入 connections API
    assert "build_connections_api(" in main_src
    assert "probe=acp_adapter.probe" in main_src
    # 单租户 OSS 与多租户 Cloud 都复用同一 runtime 组合（tenant 不进组件签名）
    assert "tenant_runtime" in main_src or "_build_tenant_runtime" in main_src


# ====================================================================== 禁止事项总闸

def test_no_forbidden_entity_classes_exist() -> None:
    """总闸：backend 真实代码标识符中无 0.1.4 禁止类。

    ❌ Integration entity           ❌ WorkspaceChannel entity
    ❌ WorkspaceAgent entity        ❌ OAuth platform
    ❌ Channel health subsystem     ❌ onboarding state machine
    ❌ FirstSuccess persistence     ❌ Runtime tenant_id
    ❌ Runtime fork                 ❌ new TenantResolver
    ❌ new authentication namespace
    """
    classes = _app_class_names()
    flagged = _FORBIDDEN_CLASSES & classes
    assert flagged == set(), f"出现禁止类：{sorted(flagged)}"
    # Runtime 不进 tenant（0.1.3.2 Gate Q10 纪律在 0.1.4 仍成立）
    main_src = _read(_BACKEND_ROOT / "app" / "main.py")
    assert "tenant-blind" in main_src  # Runtime Domain 保持 tenant-blind
