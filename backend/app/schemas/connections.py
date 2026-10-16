"""Connection Management API 的 HTTP 契约（Step 11.11，按 11.10 审计裁决落地）。

五个模型（11.11 冻结合同 + Cloud-0.1.2.2.2.1 健康快照）：

    ConnectionCreate          POST   /api/connections       请求体
    ConnectionUpdate          PATCH  /api/connections/{id}  请求体（全字段可选，缺省=不改）
    ConnectionResponse        GET/PATCH/POST 的响应体
    ConnectionListResponse    GET    /api/connections       响应体
    ConnectionHealthResponse  GET    /api/connections/{id}/health  即时诊断快照

Cloud-0.1.2.2.2.1：`ConnectionHealthResponse` 是**一次即时诊断**的快照
（`reachable` / `checked_at` / `detail` / `probe`），**不含任何持久字段**——
Connection 上没有 health 字段、没有 health 表；`checked_at` 由请求时刻生成，
绝不落库（Gate 0.1.2.2.2 §2/§7）。

边界（11.10 五焦点裁决 + 用户 11.11 指令；刻意不复制 Channel 的合同）：

- **credential_ref 是写-only 字段**：只出现在请求模型（Create/Update），
  **绝不出现**在响应模型——「引用入、引用不出」（复制 Agent 的字段面护栏，
  11.10 焦点2）；
- **Response 字段面 == 6 个可见字段**（id / name / channel_id / agent_id /
  status / config）。刻意**不携带** channel_name / agent_name——列表只返回
  绑定 ID，名称由 UI 经 /api/channels、/api/agents 解耦解析（用户 11.11 指令）；
- **config = Access Path 参数 + 响应面敏感键脱敏（16.1.2，取代 11.11 原
  「不脱敏」决定）**：P1 真实接入证实 Connection.config 携带 runtime 凭据
  （如 tencent-acp-sse 的 bearer_token），原样回显即明文泄漏。响应一律经
  sanitize_connection_config() 脱敏（敏感键 → ********，键名 strip+lower
  匹配，宁多勿漏，镜像 Channel 11.8 先例）；非敏感键（endpoint /
  runtime_id / timeout_s / ...）原样返回，管理可见。写路径（Create/Update）
  仍接受完整 config，但**拒绝哨兵值 "********"**（422）——防止脱敏回显被
  「整段替换」语义原样回写，把真实凭据覆写为占位符、打断 runtime dispatch；
- **status 白名单 active/inactive（schema 级，ConnectionUpdate）**：status 是
  纯管理态（11.10 焦点3），「error」在 HTTP 合同面就无法进入——如同
  ChannelResponse 的读时脱敏，属于合同保证而非业务规则，故在模型层强制；
- **绑定不可变（11.10 焦点4）**：channel_id / agent_id 不在 Update 模型中，
  且 Update/Create 均 extra="forbid"——「改绑定」在合同面直接 422，绝不被
  静默忽略（换绑 = 旧 Connection 置 inactive + 新建，不是 PATCH）；
- id 只出现在响应（只读）。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

# 11.10 焦点3：Connection status 是纯管理态（无 runtime error 双轴）。
# 白名单在 schema 层强制：任何非 active/inactive 的值（含 "error"）→ 422。
CONNECTION_MANAGEMENT_STATUSES = frozenset({"active", "inactive"})

# 16.1.2：Connection.config 敏感键集合（响应脱敏的唯一事实来源）。
# 匹配时 strip + lower，宁可多脱敏不可漏（镜像 Channel 11.8 的键名纪律）。
SENSITIVE_CONNECTION_CONFIG_KEYS = frozenset({
    "bearer_token", "access_token", "api_key", "client_secret",
    "token", "secret", "password", "authorization",
})

_REDACTED = "********"


def sanitize_connection_config(config: dict | None) -> dict | None:
    """对 config 的敏感键做读时脱敏；其余键原样返回（16.1.2 合同）。

    真实凭据只存 DB，经 runtime 读路径（ACPAdapter 直读 Connection.config）
    使用，永不进入任何管理响应（GET list / GET detail / POST / PATCH）。
    """
    if config is None:
        return None
    return {
        key: (
            _REDACTED
            if str(key).strip().lower() in SENSITIVE_CONNECTION_CONFIG_KEYS
            else value
        )
        for key, value in config.items()
    }


def _reject_redacted_sentinel(config: dict | None) -> dict | None:
    """写路径哨兵护栏：config 的值不得等于脱敏占位符（16.1.2）。

    POST/PATCH 的 config 是整段替换——若客户端把响应中的 "********" 原样
    回传，会把真实凭据覆写为占位符并打断 runtime dispatch。在合同面直接
    ValueError（→ 422），绝不静默容忍。按值匹配、与键名无关：任何真实
    凭据都不会恰好等于 8 个星号。
    """
    if config is None:
        return None
    for key, value in config.items():
        if isinstance(value, str) and value == _REDACTED:
            raise ValueError(
                f"config[{key!r}] 是脱敏占位符，不能作为真实值写入——"
                "请提供真实凭据，或省略 config 以保留现状")
    return config


class ConnectionCreate(BaseModel):
    """POST /api/connections 请求体。首次配置允许提交完整 config 与 credential_ref。"""

    model_config = ConfigDict(extra="forbid")

    channel_id: int
    agent_id: int
    name: str
    config: dict | None = None
    credential_ref: str | None = None  # 写-only：引用入

    @field_validator("config")
    @classmethod
    def _no_redacted_sentinel(cls, value: dict | None) -> dict | None:
        return _reject_redacted_sentinel(value)


class ConnectionUpdate(BaseModel):
    """PATCH /api/connections/{id} 请求体（局部更新：缺省/null = 不修改）。

    无 channel_id / agent_id —— immutable binding（改绑定 = 旧 inactive + 新建）。
    config：提供 = 整段替换（11.11 冻结；刻意不同于 Channel 的 merge——
    Connection 响应不脱敏、所见即所存，不存在「脱敏回显清空密键」问题）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    status: str | None = None
    config: dict | None = None
    credential_ref: str | None = None  # 写-only：缺省/null=不改；非空串=覆写

    @field_validator("status")
    @classmethod
    def _status_is_management_state(cls, value: str | None) -> str | None:
        if value is not None and value not in CONNECTION_MANAGEMENT_STATUSES:
            raise ValueError("status 必须是 active 或 inactive（纯管理态，无 error）")
        return value

    @field_validator("config")
    @classmethod
    def _no_redacted_sentinel(cls, value: dict | None) -> dict | None:
        return _reject_redacted_sentinel(value)


class ConnectionResponse(BaseModel):
    """Connection 管理面的统一响应体（单个 / 列表元素共用）。

    字段面 == 6 个可见字段；credential_ref 被「不在场」排除（引用不出）；
    不携带 channel_name / agent_name（绑定 ID only，UI 解耦解析）。
    config 敏感键读时脱敏（16.1.2）：validator 在模型层强制，无法绕过。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    channel_id: int
    agent_id: int
    status: str
    config: dict | None = None

    @field_validator("config", mode="after")
    @classmethod
    def _sanitize_config(cls, value: dict | None) -> dict | None:
        return sanitize_connection_config(value)


class ConnectionListResponse(BaseModel):
    """GET /api/connections 响应体（MVP 无分页/过滤）。"""

    items: list[ConnectionResponse]
    total: int


class ConnectionHealthResponse(BaseModel):
    """GET /api/connections/{id}/health 响应体 —— 即时诊断快照（Cloud-0.1.2.2.2.1）。

    冻结字段面（Gate 0.1.2.2.2 §7）：
    - `connection_id`：被诊断的 Access Path；
    - `reachable`：此刻探针是否连上 Agent runtime（Fail-Closed，恒有值）；
    - `checked_at`：**请求时刻**生成的时间戳（绝不落库——无 health 字段/表）；
    - `detail`：不可达时的简短原因（可达时 None）；
    - `probe`：探针所走 binding 标签（"tencent-acp-sse" / "minimal"）。

    这是「配置存在 ≠ 当前能连」中**后者**的一次性结论，不是持久 Health 状态系统：
    不写库、无状态机、无缓存（Gate 0.1.2.2.2 §2）。
    """

    connection_id: int
    reachable: bool
    checked_at: datetime
    detail: str | None = None
    probe: str | None = None
