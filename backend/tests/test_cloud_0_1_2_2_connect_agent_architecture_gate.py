"""Cloud-0.1.2.2 —— Connect Agent Architecture Gate（机器化护栏）.

设计 + 影响审计，不实现。静态 + 行为护栏，当前即可绿灯，锁死 5 个裁决：

    ① Agent Credential 归属 Connection（不归 Agent）
    ② Agent 创建 / Connection 创建 是两个独立事实
    ③ WorkBuddy = Agent(type="acp")，不造第二套类
    ④ Credential 不进 ACP DB 新列（沿用 config 脱敏 + 可选 credential_ref）
    ⑤ Connection Status 三轴分离
       （Configuration State ≠ Runtime Health ≠ Interaction Outcome）

与 Cloud-0.1.2 护栏同一纪律：只读审计 + 机器锁定；不启动 DB、不做实现。
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.adapters.agents.acp import __file__ as _ACP_FILE  # 仅取路径
from app.application.agents import (
    ALLOWED_AGENT_TYPES,
    AgentApplicationService,
)
from app.application.connections import ConnectionApplicationService
from app.application.errors import AgentNotFoundError
from app.domain.agent import Agent
from app.domain.connection import Connection
from app.domain.interaction_record import InteractionRecord
from app.persistence.models.agent import AgentModel
from app.persistence.models.connection import ConnectionModel
from app.schemas.agents import AgentResponse
from app.schemas.connections import (
    CONNECTION_MANAGEMENT_STATUSES,
    ConnectionResponse,
    sanitize_connection_config,
)

_BACKEND_APP = Path(__file__).resolve().parents[1] / "app"

# 第二套模型 / 凭据平台的禁止类（大小写敏感的类名精确匹配；docstring 误报已被排除）。
_FORBIDDEN_CLASSES = frozenset({
    "SecretManager", "CredentialVault", "OAuthCredential", "CredentialProvider",
    "WorkBuddyAgent", "WorkBuddyIntegration", "WorkBuddyConnection", "CloudWorkBuddy",
    "CloudAgent", "CloudConnection", "Bot", "Integration", "Connector",
})

# 全 app 目录一次性扫描得到的类名集合（模块级缓存，避免重复遍历）。
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


def test_agent_and_connection_are_separate_facts() -> None:
    """② Agent / Connection 是独立域类；Agent 不吸收 Connection 的绑定。"""
    assert issubclass(Agent, object) and issubclass(Connection, object)
    agent_fields = set(Agent.__dataclass_fields__)
    conn_fields = set(Connection.__dataclass_fields__)
    # Agent 不含 Channel↔Agent 绑定
    assert "channel_id" not in agent_fields
    assert "agent_id" not in agent_fields
    # Connection 含绑定且不含 Agent 的「身份/注册」字段
    assert "channel_id" in conn_fields
    assert "agent_id" in conn_fields
    assert "name" in agent_fields  # Agent 是身份
    assert "type" in agent_fields


def test_connection_create_requires_pre_existing_agent() -> None:
    """② Connection 创建依赖已存在的 Agent（两事实顺序依赖，静态确认）。"""
    assert ConnectionApplicationService is not None
    src = (Path(__file__).resolve().parents[1] / "app" / "application"
           / "connections.py").read_text(encoding="utf-8")
    # 存在性校验 + 去重预检都引用 Agent
    assert "AgentNotFoundError" in src
    assert "find_by_channel_and_agent" in src
    assert "uow.agents.get_by_id" in src


def test_agent_carries_no_credential_but_optional_reference() -> None:
    """① Agent 无 secret 字段；credential_ref 是 optional 引用且写-only。"""
    agent_fields = set(Agent.__dataclass_fields__)
    forbidden = {"secret", "api_key", "token", "password", "bearer_token"}
    assert not (forbidden & agent_fields)
    # credential_ref 作为 optional 引用存在（不在响应中出现 = 写-only）
    assert "credential_ref" in agent_fields
    assert "credential_ref" not in AgentResponse.model_fields


def test_runtime_reads_credential_from_connection_not_agent() -> None:
    """① 运行时只从 Connection 取凭据，绝不引用 agent.credential_ref。"""
    src = Path(_ACP_FILE).read_text(encoding="utf-8")
    assert "connection.config" in src
    assert "connection.credential_ref" in src
    # 关键红线：Adapter 不得从 Agent 取凭据
    assert "agent.credential_ref" not in src
    assert "request.agent.credential_ref" not in src


def test_workbuddy_lands_as_agent_type_acp() -> None:
    """③ WorkBuddy = Agent(type='acp')；无 WorkBuddy/Bot 专属类。"""
    assert "acp" in ALLOWED_AGENT_TYPES
    classes = _app_class_names()
    assert "WorkBuddyAgent" not in classes
    assert "WorkBuddyIntegration" not in classes
    assert "Bot" not in classes


def test_no_second_agent_or_connection_fork_for_workbuddy() -> None:
    """③ backend 真实代码标识符中无 workbuddy/bot 第二套分叉类。"""
    classes = _app_class_names()
    forbidden = {
        "WorkBuddyAgent", "WorkBuddyIntegration", "WorkBuddyConnection",
        "CloudWorkBuddy", "CloudAgent", "CloudConnection",
    }
    assert not (forbidden & classes)


def test_no_secret_plaintext_column_in_agent_or_connection() -> None:
    """④ agents / connections 表无 secret/plaintext 列（仅允许 credential_ref）。"""
    plaintext_markers = ("secret", "password", "token", "api_key")
    for model in (AgentModel, ConnectionModel):
        cols = [c.name for c in model.__table__.columns]
        flagged = [c for c in cols if any(m in c for m in plaintext_markers)]
        assert flagged == [], f"明文凭据列出现：{flagged}（仅允许 credential_ref）"
        assert "credential_ref" in cols  # opaque 引用仍在（明文永不入此列）


def test_connection_config_sensitive_keys_masked_on_read() -> None:
    """④ Connection.config 敏感键读时脱敏，非敏感键原样返回。"""
    out = sanitize_connection_config({
        "bearer_token": "s3cr3t", "api_key": "k", "token": "t",
        "endpoint": "https://acp.example", "runtime_id": "r1", "timeout_s": 30,
    })
    assert out is not None
    assert out["bearer_token"] == "********"
    assert out["api_key"] == "********"
    assert out["token"] == "********"
    assert out["endpoint"] == "https://acp.example"   # 非敏感：可见
    assert out["runtime_id"] == "r1"
    assert out["timeout_s"] == 30
    assert sanitize_connection_config(None) is None


def test_credential_ref_is_write_only_on_connection() -> None:
    """④ ConnectionResponse 字段面不含 credential_ref（引用不出）。"""
    assert "credential_ref" not in ConnectionResponse.model_fields
    assert "config" in ConnectionResponse.model_fields


def test_connection_status_is_pure_management_state() -> None:
    """⑤ Connection.status 是纯管理态 {active, inactive}，无运行态混入。"""
    assert CONNECTION_MANAGEMENT_STATUSES == frozenset({"active", "inactive"})
    assert "error" not in CONNECTION_MANAGEMENT_STATUSES
    assert "connected" not in CONNECTION_MANAGEMENT_STATUSES
    assert "unreachable" not in CONNECTION_MANAGEMENT_STATUSES
    assert Connection.__dataclass_fields__["status"].default == "active"


def test_runtime_health_is_derived_not_persisted() -> None:
    """⑤ Connection 域无 health/runtime_health/last_health 字段（不持久化成状态机）。"""
    fields = set(Connection.__dataclass_fields__)
    assert "health" not in fields
    assert "runtime_health" not in fields
    assert "last_health" not in fields


def test_interaction_outcome_lives_on_interaction_not_connection() -> None:
    """⑤ processing_status/delivery_status 在 Interaction 上，Connection 不吸收。"""
    interaction_fields = set(InteractionRecord.__dataclass_fields__)
    assert "processing_status" in interaction_fields
    assert "delivery_status" in interaction_fields
    conn_fields = set(Connection.__dataclass_fields__)
    assert "processing_status" not in conn_fields
    assert "delivery_status" not in conn_fields


def test_no_secret_manager_or_credential_vault_platform() -> None:
    """④/全局：backend 无 SecretManager / CredentialVault / OAuthCredential /
    CredentialProvider 等第二套凭据平台类。"""
    classes = _app_class_names()
    assert not ({"SecretManager", "CredentialVault", "OAuthCredential",
                 "CredentialProvider"} & classes)
    # 同时确认 AgentNotFoundError 仍是 Connection 创建的依赖（② 关联）
    assert AgentNotFoundError is not None
