"""Browser Auth HTTP 契约（Cloud-0.1.1-C.1，最小闭环）。

形状：
    RegisterRequest   POST /api/auth/register 请求体
    LoginRequest      POST /api/auth/login    请求体
    AuthUserResponse  register / login 的响应体

边界：
- **只做形状，不做业务判断**：非空校验由 BrowserAuthService 单一裁决
  （InvalidBrowserAuthInputError → 422），API 层不重复实现；
- **响应绝不回显凭据**：只回 {user_id, tenant_id}，无 verifier / session_id
  （session_id 只经 HttpOnly Cookie 下发，不进入响应体）；
- `tenant_id` 仅 register 使用（本地/OSS 缺省 1）；login 不携带 tenant——
  它由凭据在服务端解析（bootstrap）。
"""

from pydantic import BaseModel, ConfigDict


class RegisterRequest(BaseModel):
    """POST /api/auth/register 请求体（最小 Browser User 注册）。"""

    model_config = ConfigDict(extra="forbid")

    login: str
    password: str
    tenant_id: int = 1


class SignUpRequest(BaseModel):
    """POST /api/auth/signup 请求体（Cloud-0.1.2.1）。

    刻意**只有** login / password：注册即获得一个 Tenant（用户 Workspace），
    用户不需要额外填写 Workspace Name / 走「创建 Workspace」步骤。
    """

    model_config = ConfigDict(extra="forbid")

    login: str
    password: str


class LoginRequest(BaseModel):
    """POST /api/auth/login 请求体。"""

    model_config = ConfigDict(extra="forbid")

    login: str
    password: str


class AuthUserResponse(BaseModel):
    """已认证用户的最小事实（无凭据、无会话令牌）。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: int
    tenant_id: int
