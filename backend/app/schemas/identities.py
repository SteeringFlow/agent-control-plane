"""Identity Management API 的 HTTP 契约（Step 12.4，与 11.3/11.8/11.11 同一模式）。

四个模型：

    IdentityCreate        POST   /api/identities      请求体
    IdentityUpdate        PATCH  /api/identities/{id} 请求体（全字段可选，缺省=不改）
    IdentityResponse      GET/PATCH/POST 的响应体
    IdentityListResponse  GET    /api/identities      响应体

边界：

- **Schema 只做形状，不做业务判断**：User/Channel 存在性（→ 404）、
  (channel_id, external_id) 唯一性（→ 409）、非空校验（→ 422）全部由
  Application Service 单一裁决；
- **Create 无 status**：status 由领域默认 active（与 Channel Create 同一先例）；
  管理态变更走 PATCH；
- **绑定骨架不可变（6.6/12.3 冻结，绝不引入 Identity Linking）**：
  user_id / channel_id / external_id 不在 Update 模型中，且 Create/Update 均
  extra="forbid"——「改绑定」在合同面直接 422，绝不被静默忽略；
- **IdentityUpdate 的 None 语义与 UpdateIdentity 一致（12.3 冻结）**：
  缺省 / null = 不修改；PATCH {} → noop；metadata 提供 = 整段替换；
  metadata 非 dict 在模型层（dict 类型）与服务层双重拒绝 → 422；
- **metadata 不脱敏**：Identity.metadata 不是凭据容器（与 Connection config
  同一先例：所见即所存）；若未来出现敏感键再引入 sanitize，不在本步预设；
- 无 created_at/updated_at 回显（Connection 11.11 同一先例）。
"""

from pydantic import BaseModel, ConfigDict


class IdentityCreate(BaseModel):
    """POST /api/identities 请求体。user_id / channel_id 必须已存在（服务层校验）。

    **语义（Gate 3 §D 冻结）**：本动作实际是 **Create managed binding** ——
    「创建 External Identity（A）」与「绑定到 ACP User（B）」被原子地合成一个用例。
    `user_id` 因此保持**必填**：V0 **不暴露**「创建未绑定 Identity」与「Bind / Unbind」
    的独立 API（Gate 2.6 D7 的语义拆分已落在模型 / 运行时 / 文档层，
    **语义拆分 ≠ API 数量必须增加**）。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int
    channel_id: int
    external_id: str
    display_name: str | None = None
    metadata: dict | None = None


class IdentityUpdate(BaseModel):
    """PATCH /api/identities/{id} 请求体（局部更新：缺省/null = 不修改，12.3 冻结）。

    无 user_id / channel_id / external_id —— immutable binding
    （extra="forbid"：改绑定在合同面直接 422；改绑定 = Identity Linking，
    属于未来 Slice，本步绝不引入）。
    metadata：提供 = 整段替换（非 Channel 11.8 的逐键合并——Identity 响应
    不脱敏，无「脱敏回显清空密键」问题）。
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    status: str | None = None
    metadata: dict | None = None


class IdentityResponse(BaseModel):
    """Identity 管理面的统一响应体（单个 / 列表元素共用）。

    字段面 == 领域可见字段（7 个）；绑定骨架以 ID 形式回显——channel 名称
    解析由 UI 经 /api/channels 解耦完成（与 Connection 只回绑定 ID 同一裁决）。

    **`user_id` 可空（Implementation Package 1 / Gate 2.6 D1/D2）**：
    None = 该外部身份未与 ACP Account 建立管理绑定（不是匿名 / 访客 / 系统用户）。
    这是纯放宽——JSON 里 key 始终存在，既有消费方按非空读取的行为不变。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None
    channel_id: int
    external_id: str
    display_name: str | None = None
    status: str
    metadata: dict | None = None


class IdentityListResponse(BaseModel):
    """GET /api/identities 响应体（MVP 无分页/过滤；跨渠道同 external_id 不合并）。"""

    items: list[IdentityResponse]
    total: int
