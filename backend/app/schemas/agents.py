"""Agent Management API 的 HTTP 契约（Step 11.3，按 11.2 冻结契约落地）。

四个模型（11.2 §5）：

    AgentCreate        POST   /api/agents      请求体
    AgentUpdate        PATCH  /api/agents/{id} 请求体（全字段可选，缺省=不改）
    AgentResponse      GET/PATCH/POST 的响应体
    AgentListResponse  GET    /api/agents      响应体

边界（11.2 §7 + 用户 11.3 指令）：

- **Schema 只做形状**（字段类型 / 必填与可选 / 响应字段面），**不做业务判断**——
  type 受控集合、status 管理态白名单、endpoint URL 校验、credential_ref 非空
  全部由 Application Service 单一裁决（InvalidAgentInputError → 422），
  API 层不重复实现业务规则；
- **credential_ref 是写-only 字段**：只出现在请求模型（Create/Update），
  **绝不出现**在响应模型（AgentResponse/AgentListResponse）——「引用入、引用不出」；
- AgentUpdate 的 None 语义与 UpdateAgent 一致：缺省 / 显式 null = 不修改
  （本 MVP 无清空语义）；提供非空字符串 = 覆写；
- id / created_at / updated_at 只出现在响应（只读审计回显）。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: OpenAPI 可读说明（受控集合的唯一裁决点仍是 Application Service）。
_AGENT_TYPE_DESCRIPTION = "受控 Agent 类型：acp | local（服务层校验，非法值 422）"

# M7-AUI-10.2：Agent.config 敏感键集合（响应脱敏的唯一事实来源）。
# 匹配时 strip + lower，宁可多脱敏不可漏（镜像 Channel 11.8 / Connection 16.1.2
# 的键名纪律）。provider / workbuddy_agent_id 是非敏感管理键，原样返回。
SENSITIVE_AGENT_CONFIG_KEYS = frozenset({"api_key"})

_REDACTED = "********"


def sanitize_agent_config(config: dict | None) -> dict | None:
    """对 config 的敏感键做读时脱敏；其余键原样返回（镜像 Channel 11.8 合同 #1）。

    非敏感键（provider / workbuddy_agent_id / ...）保持管理可见；
    api_key 替换为固定占位符——真实 API Key 绝不出现在任何响应中。
    """
    if config is None:
        return None
    return {
        key: (
            _REDACTED
            if str(key).strip().lower() in SENSITIVE_AGENT_CONFIG_KEYS
            else value
        )
        for key, value in config.items()
    }


def _reject_redacted_sentinel(config: dict | None) -> dict | None:
    """写路径哨兵护栏（M7-AUI-10.2，镜像 Connection 16.1.2）。

    POST/PATCH 的 config 中任何值等于脱敏占位符（"********"，**按值匹配，
    与键名无关**）→ 422——绝不能把「已配置」的占位标记当真实值写进数据库
    覆盖真实 secret。真实轮换 = 提交新值；保留现状 = 省略该键（前端哨兵
    省略 + 服务层 merge 共同保证）。
    """
    if config is None:
        return None
    for key, value in config.items():
        if isinstance(value, str) and value == _REDACTED:
            raise ValueError(
                f"config[{key!r}] 是脱敏占位符，不能作为真实值写入——"
                "请提供真实凭据，或省略该键以保留现状")
    return config


class AgentCreate(BaseModel):
    """POST /api/agents 请求体。首次配置允许提交完整 config（含 secret）。"""

    name: str
    type: str = Field(description=_AGENT_TYPE_DESCRIPTION)
    endpoint: str | None = None
    status: str | None = None          # 管理态 ∈ {active, inactive}；服务层校验
    config: dict | None = None
    credential_ref: str | None = None  # 写-only：引用入

    @field_validator("config")
    @classmethod
    def _no_redacted_sentinel(cls, value: dict | None) -> dict | None:
        return _reject_redacted_sentinel(value)


class AgentUpdate(BaseModel):
    """PATCH /api/agents/{id} 请求体（局部更新：缺省/null = 不修改）。

    config：提供（部分）dict = merge 进现有 config（提供的键覆写、未提供的保留，
    M7-AUI-10.2）；config 的任何值不得是脱敏占位符（哨兵 422）。
    """

    name: str | None = None
    type: str | None = Field(default=None, description=_AGENT_TYPE_DESCRIPTION)
    status: str | None = None
    endpoint: str | None = None
    config: dict | None = None
    credential_ref: str | None = None  # 写-only：缺省/null=不改；非空串=覆写

    @field_validator("config")
    @classmethod
    def _no_redacted_sentinel(cls, value: dict | None) -> dict | None:
        return _reject_redacted_sentinel(value)


class AgentResponse(BaseModel):
    """Agent 管理面的统一响应体（单个 / 列表元素共用）。

    from_attributes：直接从领域 Agent（frozen dataclass）装配。
    字段面 == 8 个可见字段；credential_ref 被「不在场」排除（引用不出）。
    config 一律经 sanitize_agent_config 脱敏（M7-AUI-10.2）——api_key 永不
    出现在任何 GET / List / POST / PATCH 响应中（validator 在模型层强制，
    无法绕过，镜像 Channel 11.8 合同 #1）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: str
    status: str
    endpoint: str | None = None
    config: dict | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("config", mode="after")
    @classmethod
    def _sanitize_config(cls, value: dict | None) -> dict | None:
        return sanitize_agent_config(value)


class AgentListResponse(BaseModel):
    """GET /api/agents 响应体（MVP 无分页/过滤，11.2 §5.4）。"""

    items: list[AgentResponse]
    total: int
