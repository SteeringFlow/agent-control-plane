"""Browser Authentication HTTP API（Cloud-0.1.1-C.1）。

端点面（Gate 裁决的最小闭环）：

    POST /api/auth/register   201 / 409 / 422   创建最小 Browser User + 凭据（显式 tenant）
                                                （条件挂载：expose_register=True 才暴露；
                                                  Cloud 组合根关闭——见 build_auth_api docstring）
    POST /api/auth/signup     201 / 409 / 422   注册即建租户 + 建会话（Cloud Sign Up，0.1.2.1）
    POST /api/auth/login      200 / 401 / 422   校验凭据 → 下发 opaque session Cookie
    GET  /api/auth/me         200 / 401         当前会话的已认证事实（user_id + tenant_id）
    POST /api/auth/logout     204               删除会话 + 清 Cookie

职责边界（与 api/management/* 同一注入纪律）：

    FastAPI（本模块：HTTP 事实提取 + 应答序列化 + Auth 错误→状态码映射 + Cookie）
        ↓
    BrowserAuthService（凭据校验 / 会话建立与解析的唯一裁决点）
        ↓
    UoW Port → Persistence Adapter → DB

**状态码纪律（Gate §2）**：Browser Auth 失败 → **401**；身份/输入不合法 → 422；
登录名冲突 → 409。**绝不产生 403** —— 403 保留给「已认证但无权限 / tenant 解析
失败」的语义（WeCom 入站验签用的就是 403），不混入授权语义。

本模块不 import SQLAlchemy / app.persistence（经构造注入装配好的 Service，
由 main.py 组合根绑定 UoW；测试注入内存库实例）。

`build_tenant_scope_provider` 产出 **tenant 解析依赖**：Cookie → AuthenticatedUser
→ `TenantScope`（L3，复用 app.core.tenant）。它是管理面 router 的注入点——管理面
服务由该依赖派生的 tenant-scoped UoW 构造（请求真正进入 B 已冻结的隔离）。
"""

from fastapi import APIRouter, HTTPException, Request, Response

from app.application.browser_auth import (
    BrowserAuthService,
    LoginBrowserUser,
    RegisterBrowserUser,
    SignUpBrowserUser,
)
from app.application.errors import (
    ApplicationError,
    DuplicateBrowserLoginError,
    InvalidBrowserAuthInputError,
)
from app.core.browser_auth import AuthenticatedUser, BrowserAuthError
from app.core.tenant import TenantScope, default_tenant_scope
from app.schemas.auth import (
    AuthUserResponse,
    LoginRequest,
    RegisterRequest,
    SignUpRequest,
)

# opaque 会话 Cookie 名。值是不透明随机令牌（非 JWT）。
SESSION_COOKIE_NAME = "acp_session"


def _map_auth_error(exc: Exception) -> HTTPException:
    """Browser Auth 错误 → HTTP 状态码（唯一映射点）。

    401 = 认证失败；**不产生 403**（授权语义不在此混入）。
    """
    if isinstance(exc, BrowserAuthError):
        return HTTPException(status_code=401, detail="authentication failed")
    if isinstance(exc, DuplicateBrowserLoginError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, InvalidBrowserAuthInputError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ApplicationError):
        return HTTPException(status_code=500, detail="internal auth error")
    raise exc  # 非预期异常冒泡为 500（不由本层吞）


def _set_session_cookie(response: Response, session_id: str, *, secure: bool) -> None:
    """写入会话 Cookie：HttpOnly + SameSite=Strict（+ 生产 Secure），Path=/。"""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )


def build_auth_api(
    service: BrowserAuthService,
    *,
    cookie_secure: bool = False,
    expose_register: bool = True,
) -> APIRouter:
    """构造 Browser Auth router（依赖注入：传入装配好的 BrowserAuthService）。

    `expose_register`（M7-AUI-10.8.23-C.5，ISS-07 最小修复）：是否挂载
    `POST /api/auth/register` 路由。缺省 True 保持 OSS / 既有测试行为不变；
    Cloud 组合根传 `expose_register=not settings.AUTH_REQUIRED`——Cloud 模式
    下该路由不注册（404），公网自助注册只保留 `/signup`（注册即建新租户，
    调用者无法选择租户）。服务层 `BrowserAuthService.register()` 保留不动
    （signup 内部复用它，tenant_id 恒来自服务端 provisioner）。
    """
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    # ------------------------------------------------------------ POST /api/auth/register

    if expose_register:

        @router.post("/register", status_code=201, response_model=AuthUserResponse)
        def register(payload: RegisterRequest) -> AuthUserResponse:
            """创建最小 Browser User（User + 凭据）；login 冲突 → 409。"""
            try:
                user = service.register(
                    RegisterBrowserUser(
                        login=payload.login,
                        password=payload.password,
                        tenant_id=payload.tenant_id,
                    )
                )
            except (BrowserAuthError, ApplicationError) as exc:
                raise _map_auth_error(exc) from exc
            return AuthUserResponse(user_id=user.user_id, tenant_id=user.tenant_id)

    # ------------------------------------------------------------ POST /api/auth/signup

    @router.post("/signup", status_code=201, response_model=AuthUserResponse)
    def signup(payload: SignUpRequest, response: Response) -> AuthUserResponse:
        """Cloud Sign Up：注册即建租户 + 建会话，直达 Workspace（0.1.2.1）。

        唯一产品动作：用户只提供 login / password。服务端 provision 一个 Tenant
        （用户 Workspace 的实体化）→ 建 User + 凭据 → 建 opaque 会话 → Set-Cookie。
        无需「创建 Workspace」步骤。
        """
        try:
            grant = service.signup(SignUpBrowserUser(
                login=payload.login, password=payload.password))
        except (BrowserAuthError, ApplicationError) as exc:
            raise _map_auth_error(exc) from exc
        _set_session_cookie(response, grant.session_id, secure=cookie_secure)
        return AuthUserResponse(
            user_id=grant.user.user_id, tenant_id=grant.user.tenant_id
        )

    # ------------------------------------------------------------ GET /api/auth/me

    @router.get("/me", response_model=AuthUserResponse)
    def current_user(request: Request) -> AuthUserResponse:
        """当前浏览器会话的已认证事实（user_id + tenant_id）。

        供 Workspace UI 判断「用户是否已进入自己的 Tenant Workspace」：
        - 有有效 Cookie → 200 {user_id, tenant_id}；
        - 无 / 无效 Cookie → 401（Cloud 语义；Workspace UI 据此显示 Sign Up）。
        仅暴露身份事实，绝不回显凭据 / 会话令牌，也无任何 Workspace 持久化实体。
        """
        proof = request.cookies.get(SESSION_COOKIE_NAME)
        if not proof:
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            user = service.verify(proof)
        except BrowserAuthError as exc:
            raise _map_auth_error(exc) from exc
        return AuthUserResponse(user_id=user.user_id, tenant_id=user.tenant_id)

    # ------------------------------------------------------------ POST /api/auth/login

    @router.post("/login", response_model=AuthUserResponse)
    def login(payload: LoginRequest, response: Response) -> AuthUserResponse:
        """校验凭据 → 建立 opaque 会话（HttpOnly Cookie）；失败 → 401。"""
        try:
            grant = service.login(
                LoginBrowserUser(login=payload.login, password=payload.password)
            )
        except (BrowserAuthError, ApplicationError) as exc:
            raise _map_auth_error(exc) from exc
        _set_session_cookie(response, grant.session_id, secure=cookie_secure)
        return AuthUserResponse(
            user_id=grant.user.user_id, tenant_id=grant.user.tenant_id
        )

    # ------------------------------------------------------------ POST /api/auth/logout

    @router.post("/logout", status_code=204)
    def logout(request: Request) -> Response:
        """删除会话并清 Cookie（无会话为 no-op）。"""
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        if session_id:
            service.logout(session_id)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return response

    return router


def build_tenant_scope_provider(
    service: BrowserAuthService, *, auth_required: bool
):
    """产出 FastAPI 依赖：Cookie → TenantScope（或 401 / OSS TENANT_LOCAL 回退）。

    - 有 Cookie：认证会话 → `TenantScope(tenant_id=user.tenant_id)`（L3）；
      无效 Cookie 一律 401（即使用户主动呈交了坏凭据）；
    - 无 Cookie 且 `auth_required=True`（Cloud 多租户）→ 401；
    - 无 Cookie 且 `auth_required=False`（OSS / 本地单租户）→ `default_tenant_scope()`
      （TenantScope(TENANT_LOCAL)），行为与隔离前逐字节一致。
    """

    def tenant_scope_from_request(request: Request) -> TenantScope:
        proof = request.cookies.get(SESSION_COOKIE_NAME)
        if proof:
            try:
                user = service.verify(proof)
            except BrowserAuthError as exc:
                raise _map_auth_error(exc) from exc
            return TenantScope(tenant_id=user.tenant_id)
        if auth_required:
            raise HTTPException(status_code=401, detail="authentication required")
        return default_tenant_scope()

    return tenant_scope_from_request


def build_authenticated_user_provider(service: BrowserAuthService):
    """产出 FastAPI 依赖：Cookie → `AuthenticatedUser`（**不回退**）。

    与 `build_tenant_scope_provider` 的**唯一且关键**差别：

        tenant_scope_provider      无 Cookie + AUTH_REQUIRED=False → TENANT_LOCAL 回退
        authenticated_user_provider 无 Cookie / 无效 Cookie → **401**

    M6.6-C3 / O-4（ADR-12 §15）：Bootstrap Arm 的 initiator 是**合法性的组成部分**。
    没有已认证主体 ⇒ **Arm 完全不可用**，绝不回退到缺省租户身份
    ——那等于用一个不可归属的主体充当授权来源（anonymous / guest / system /
    sentinel 一律禁止）。

    判定条件是「本次请求是否呈交了有效会话」，而**不是** `AUTH_REQUIRED` 是否为真：
    OSS 部署中已登录的操作者同样持有真实身份，因此可以 Arm。
    """

    def authenticated_user_from_request(request: Request) -> AuthenticatedUser:
        proof = request.cookies.get(SESSION_COOKIE_NAME)
        if not proof:
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            return service.verify(proof)
        except BrowserAuthError as exc:
            raise _map_auth_error(exc) from exc

    return authenticated_user_from_request
