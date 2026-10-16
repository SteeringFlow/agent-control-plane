"""Channel Management API 的 HTTP 契约（Step 11.8，按 11.7 审计裁决落地）。

四个模型（11.8 冻结合同）：

    ChannelCreate        POST   /api/channels       请求体
    ChannelUpdate        PATCH  /api/channels/{id}  请求体（全字段可选，缺省=不改）
    ChannelResponse      GET/PATCH/POST 的响应体
    ChannelListResponse  GET    /api/channels       响应体

边界（11.7 §2.2/§2.3 裁决 + 用户 11.8 五条冻结合同）：

- **Schema 只做形状**，不做业务判断——type 保持自由字符串（不建枚举，合同 #5）、
  status 不设白名单；业务判断单一裁决点在 Application Service；
- **Channel 没有 credential_ref 字段**——凭据材料（token / encoding_aes_key /
  corpsecret）直接住在 Channel.config 内。因此 Agent 的「credential_ref 写-only、
  引用入引用不出」在 Channel 上的对应物是「**敏感 config 键 → 读时脱敏**」
  （合同 #4）：
    * Create / Patch 请求模型接受完整 config（合同 #2：首次配置需要录入 secret）；
    * 响应模型一律经 `sanitize_channel_config()` 脱敏（合同 #1）——
      token / encoding_aes_key / corpsecret 永不出现在任何响应中；
    * 非敏感键（corp_id / agentid / api_base / timeout_s）原样返回，管理可见；
- config 更新为 **merge 语义**（合同 #3，由 Application Service 单一裁决）：
  PATCH 提供的键覆写、未提供的键保留——防止脱敏回显把密键整段清空；
- 敏感键集合的唯一事实来源 = 各 Channel Adapter 的真实读取键（11.7 §2.2）；
  出现第二个 Channel Adapter 时重新审计 —— 该 re-audit 已在 3B-2.2 执行：
  新增 `app/adapters/channels/wecom_bot.py`（读取 `bot_id` / `bot_secret` /
  `ws_url`）→ `bot_secret` 已并入下方集合。
- id 只出现在响应（只读）。
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: OpenAPI 可读说明（受控集合的唯一裁决点仍是 Application Service；此处只做文档化，
#: 不在 Schema 层重复实现校验逻辑）。
_TYPE_DESCRIPTION = "受控渠道类型：wecom | local | wecom_bot（服务层校验，非法值 422）"

# 11.7 §2.2 敏感度分类：Channel.config 中的 credential-like material。
# 匹配时大小写不敏感（strip + lower），宁可多脱敏不可漏。
SENSITIVE_CHANNEL_CONFIG_KEYS = frozenset(
    {"token", "encoding_aes_key", "corpsecret", "bot_secret"}
)

_REDACTED = "********"


def sanitize_channel_config(config: dict | None) -> dict | None:
    """对 config 的敏感键做读时脱敏；其余键原样返回（合同 #1）。

    非敏感键（corp_id / agentid / api_base / timeout_s / ...）保持管理可见；
    token / encoding_aes_key / corpsecret 替换为固定占位符。
    """
    if config is None:
        return None
    return {
        key: (
            _REDACTED
            if str(key).strip().lower() in SENSITIVE_CHANNEL_CONFIG_KEYS
            else value
        )
        for key, value in config.items()
    }


class ChannelCreate(BaseModel):
    """POST /api/channels 请求体。首次配置允许提交完整 config（含 secret）。"""

    name: str
    type: str = Field(description=_TYPE_DESCRIPTION)
    config: dict | None = None


class ChannelUpdate(BaseModel):
    """PATCH /api/channels/{id} 请求体（局部更新：缺省/null = 不修改）。

    config：提供（部分）dict = merge 进现有 config（提供的键覆写、未提供的保留）；
    本 MVP 无「清空 config / 删除单个键」语义。
    """

    name: str | None = None
    type: str | None = Field(default=None, description=_TYPE_DESCRIPTION)
    status: str | None = None
    config: dict | None = None


class ChannelResponse(BaseModel):
    """Channel 管理面的统一响应体（单个 / 列表元素共用）。

    字段面 == 5 个可见字段（合同 #1：id / name / type / status / config）；
    config 一律脱敏——token / encoding_aes_key / corpsecret 永不出现在
    任何 GET / List / PATCH / POST 响应中（validator 在模型层强制，无法绕过）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: str
    status: str
    config: dict | None = None

    @field_validator("config", mode="after")
    @classmethod
    def _sanitize_config(cls, value: dict | None) -> dict | None:
        return sanitize_channel_config(value)


class ChannelListResponse(BaseModel):
    """GET /api/channels 响应体（MVP 无分页/过滤）。"""

    items: list[ChannelResponse]
    total: int
