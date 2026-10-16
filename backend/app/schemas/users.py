"""User Management API 的 HTTP 契约（Step 12.4，与 11.3/11.8/11.11 同一模式）。

四个模型：

    UserCreate        POST   /api/users      请求体
    UserUpdate        PATCH  /api/users/{id} 请求体（全字段可选，缺省=不改）
    UserResponse      GET/PATCH/POST 的响应体
    UserListResponse  GET    /api/users      响应体

边界：

- **Schema 只做形状，不做业务判断**：name 非空校验由 Application Service
  单一裁决（InvalidUserInputError → 422），API 层不重复实现业务规则；
- **UserCreate 只有 name**：status 由领域默认 active（与 Channel Create 同一
  先例）；name 不是唯一键（唯一性归 Identity，6.4 冻结），无去重端点语义；
- **UserUpdate 的 None 语义与 UpdateUser 一致（12.3 冻结）**：缺省 / 显式
  null = 不修改；PATCH {} → noop；非 None 空串 → 422（服务层）；
- **id 不可改**：不在 Create/Update 模型中，且 extra="forbid"——试图在
  请求体携带 id 在合同面直接 422，绝不被静默忽略；
- **无敏感字段**：User 只有 name/status，响应即领域字段面（无 credential_ref
  类写-only 字段，无 Channel 式脱敏需求）；
- **无 created_at/updated_at 回显**：Connection 11.11 同一先例——响应字段面
  == 领域可见字段（User 领域无时间戳，domain 纯 dataclass 边界不引入 datetime）。
"""

from pydantic import BaseModel, ConfigDict


class UserCreate(BaseModel):
    """POST /api/users 请求体。status 由领域默认 active（变更走 PATCH）。"""

    model_config = ConfigDict(extra="forbid")

    name: str


class UserUpdate(BaseModel):
    """PATCH /api/users/{id} 请求体（局部更新：缺省/null = 不修改，12.3 冻结）。

    无 id 字段——immutable（extra="forbid"：携带 id 在合同面直接 422）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    status: str | None = None


class UserResponse(BaseModel):
    """User 管理面的统一响应体（单个 / 列表元素共用）。

    字段面 == 领域可见字段（id / name / status）；无敏感字段、无脱敏需求。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: str


class UserListResponse(BaseModel):
    """GET /api/users 响应体（MVP 无分页/过滤；Identity 绑定 UI 的 User 下拉数据源）。"""

    items: list[UserResponse]
    total: int
