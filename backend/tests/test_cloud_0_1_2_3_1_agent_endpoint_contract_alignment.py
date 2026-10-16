"""Cloud-0.1.2.3.1 —— Agent Endpoint Contract Alignment（机器化护栏）.

设计 + 影响审计，不实现。静态 + 轻行为护栏，当前即可绿灯，锁死裁决：

    Agent.endpoint              = Agent Identity 的「可选描述性元数据」
    Connection.config["endpoint"] = 唯一权威的访问 endpoint（无双重事实）

即：
- Agent.endpoint 可选、可见、但不被 runtime 消费；
- 连通性只有 Connection.config["endpoint"] 一个事实来源；
- 既有 OSS 合同（11.2/11.3 + type="local"）本就允许 endpoint 可选。

与 Cloud-0.1.2.2 系列护栏同一纪律：只读审计 + 机器锁定；不启动 DB、不做实现。
"""

from __future__ import annotations

from pathlib import Path

from app.application.agents import AgentApplicationService, CreateAgent, UpdateAgent
from app.application.connections import ConnectionApplicationService
from app.domain.agent import Agent
from app.domain.connection import Connection
from app.persistence.models.agent import AgentModel
from app.persistence.models.connection import ConnectionModel
from app.schemas.agents import AgentCreate, AgentResponse, AgentUpdate
from app.schemas.connections import ConnectionCreate, ConnectionResponse

_BACKEND = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND.parent
_ADAPTERS = _BACKEND / "app" / "adapters" / "agents"
_AGENTS_APP = _BACKEND / "app" / "application" / "agents.py"
_WORKBUDDY_README = _REPO_ROOT / "examples" / "wecom-workbuddy" / "README.md"


def test_agent_endpoint_is_optional_metadata() -> None:
    """§2：Agent.endpoint 是可选（非必填）、可见的描述性元数据。"""
    # API 契约：endpoint 非必填。
    assert AgentCreate.model_fields["endpoint"].is_required() is False
    assert AgentUpdate.model_fields["endpoint"].is_required() is False

    # 领域字段默认 None。
    assert Agent.__dataclass_fields__["endpoint"].default is None

    # 响应面可见（描述性元数据，非写-only）。
    assert "endpoint" in AgentResponse.model_fields

    # ORM 列可为空（纯元数据列）。
    assert AgentModel.__table__.columns["endpoint"].nullable is True

    # 轻行为：不带 endpoint 即可构造 Create 输入与 API 模型（无校验错误）。
    assert CreateAgent(name="workbuddy", type="acp").endpoint is None
    assert CreateAgent(name="workbuddy", type="acp", endpoint=None).endpoint is None
    assert UpdateAgent(name="x").endpoint is None
    assert AgentCreate(name="workbuddy", type="acp").endpoint is None


def test_create_normalizes_blank_endpoint_but_patch_does_not() -> None:
    """0.1.2.3.1.1：create 侧镜像 credential_ref 归一化（空白 → None）；
    PATCH（update_agent）合同不变（仍以 _require_non_blank 校验 endpoint）。"""
    src = _AGENTS_APP.read_text(encoding="utf-8")
    assert "_normalize_endpoint" in src
    assert "endpoint = _normalize_endpoint(input.endpoint)" in src  # 只处理 create
    assert "_require_non_blank(change.endpoint" in src              # PATCH 未被改动


def test_runtime_never_reads_agent_endpoint() -> None:
    """§2/§3：runtime / adapter 从不读 agent.endpoint（无双重事实来源）。"""
    for path in (
        _ADAPTERS / "acp.py",
        _ADAPTERS / "local.py",
        _ADAPTERS / "registry.py",
        _AGENTS_APP,
    ):
        src = path.read_text(encoding="utf-8")
        assert "agent.endpoint" not in src, f"{path.name} 读取了 agent.endpoint"
        assert "request.agent.endpoint" not in src, f"{path.name} 读取了 request.agent.endpoint"


def test_actual_access_endpoint_is_connection_config() -> None:
    """§3：实际访问 endpoint 只来自 Connection.config；Connection 无顶层 endpoint 字段。"""
    acp_src = (_ADAPTERS / "acp.py").read_text(encoding="utf-8")
    assert "connection.config" in acp_src
    assert 'config.get("endpoint")' in acp_src
    # ACPAdapter 不读 Agent 上的任何连接要素。
    assert "agent.endpoint" not in acp_src
    assert "agent.config" not in acp_src

    # Connection 域 / schema 无顶层 endpoint（endpoint 只在 config 内）。
    assert "endpoint" not in Connection.__dataclass_fields__
    assert "endpoint" not in ConnectionCreate.model_fields
    assert "endpoint" not in ConnectionResponse.model_fields


def test_no_double_endpoint_fact() -> None:
    """§3：Agent 有 endpoint 元数据列，Connection 无 endpoint 列——不构成两个持久事实。"""
    agent_cols = {c.name for c in AgentModel.__table__.columns}
    conn_cols = {c.name for c in ConnectionModel.__table__.columns}
    assert "endpoint" in agent_cols          # Agent 侧：描述性元数据列
    assert "endpoint" not in conn_cols       # Connection 侧：无顶层 endpoint（只在 config JSON）


def test_workbuddy_recipe_omits_agent_endpoint() -> None:
    """§4：WorkBuddy 配方 Agent 不传 endpoint；endpoint 落在 Connection.config（证据锁定）。"""
    assert _WORKBUDDY_README.is_file()
    text = _WORKBUDDY_README.read_text(encoding="utf-8")
    # Agent 创建不带 endpoint。
    assert '"name": "workbuddy", "type": "acp"' in text
    # 实际访问 endpoint 在 Connection.config 中。
    assert '"endpoint": "<acpLink.url>"' in text
