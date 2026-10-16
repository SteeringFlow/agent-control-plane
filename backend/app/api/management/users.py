"""User Management HTTP API（Step 12.4，按 11.3/11.8/11.11 同一注入纪律落地）。

端点面（12.4 冻结；无 DELETE——管理态变更 = PATCH status）：

    POST   /api/users         201 / 422
    GET    /api/users         200
    GET    /api/users/{id}    200 / 404
    PATCH  /api/users/{id}    200 / 404 / 422

职责边界（与 api/management/agents.py、channels.py、connections.py 同一纪律）：

    FastAPI（本模块：HTTP 事实提取 + 应答序列化 + Application 错误→状态码映射）
        ↓
    UserApplicationService（业务规则唯一裁决点：name 非空等输入校验）
        ↓
    UoW Port → Persistence Adapter → DB

本模块不做业务判断——错误映射是唯一的 API 层职责：

    UserNotFoundError      → 404 Not Found
    InvalidUserInputError  → 422 Unprocessable Entity

List 是 Identity 绑定 UI 的数据源（User dropdown，12.4 目标）；Channel 下拉
由既有 GET /api/channels 提供，本模块不消费、不 join。

与 api/runtime 互不混合：管理面归本模块，运行时协议归 Runtime
（Runtime 消费 DB 事实，绝不调用 Management API——12.4 冻结边界）。

本模块不 import SQLAlchemy / app.persistence（经构造注入装配好的 Service，
由 main.py 组合根绑定 SqlAlchemyUnitOfWork；测试注入内存库实例）。
"""

from fastapi import APIRouter, Depends, HTTPException

from app.application.errors import (
    ApplicationError,
    InvalidUserInputError,
    UserNotFoundError,
)
from app.application.users import CreateUser, UpdateUser, UserApplicationService
from app.schemas.users import (
    UserCreate,
    UserListResponse,
    UserResponse,
    UserUpdate,
)


def build_users_api(
    service: UserApplicationService | None = None, *, service_provider=None
) -> APIRouter:
    """构造 User 管理 router（依赖注入：传入装配好的 UserApplicationService）。

    Cloud-0.1.1-C：可选 `service_provider`（FastAPI 依赖）——组合根用它把
    「请求解析出的 TenantScope → tenant-scoped Service」接进来；不传则退回
    注入的 `service`（测试 / OSS 单租户路径，行为不变）。
    """
    router = APIRouter(prefix="/api/users", tags=["management-users"])

    if service_provider is None:
        def _provider() -> UserApplicationService:
            assert service is not None, "build_users_api 需要 service 或 service_provider"
            return service
    else:
        _provider = service_provider
    service_dep = Depends(_provider)

    def _map_application_error(exc: ApplicationError) -> HTTPException:
        """Application 错误 → HTTP 状态码（唯一映射点，12.4 裁决表）。"""
        if isinstance(exc, UserNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, InvalidUserInputError):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=500, detail="internal management error")

    # ------------------------------------------------------------ POST /api/users

    @router.post("", status_code=201, response_model=UserResponse)
    def create_user(
        payload: UserCreate, service: UserApplicationService = service_dep
    ) -> UserResponse:
        """创建 User。name 是显示名不是唯一键（同名合法，6.4 冻结）；status 默认 active。"""
        try:
            user = service.create_user(CreateUser(**payload.model_dump()))
        except ApplicationError as exc:
            raise _map_application_error(exc) from exc
        return UserResponse.model_validate(user)

    # ------------------------------------------------------------ GET /api/users

    @router.get("", response_model=UserListResponse)
    def list_users(
        service: UserApplicationService = service_dep,
    ) -> UserListResponse:
        """列出全部 User（MVP 无分页/过滤；Identity 绑定 UI 的 User 下拉数据源）。"""
        users = service.list_users()
        items = [UserResponse.model_validate(u) for u in users]
        return UserListResponse(items=items, total=len(items))

    # ------------------------------------------------------------ GET /api/users/{id}

    @router.get("/{user_id}", response_model=UserResponse)
    def get_user(
        user_id: int, service: UserApplicationService = service_dep
    ) -> UserResponse:
        """读取单个 User；不存在 → 404。"""
        user = service.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail=f"User {user_id} 不存在")
        return UserResponse.model_validate(user)

    # ------------------------------------------------------------ PATCH /api/users/{id}

    @router.patch("/{user_id}", response_model=UserResponse)
    def patch_user(
        user_id: int, payload: UserUpdate,
        service: UserApplicationService = service_dep,
    ) -> UserResponse:
        """局部更新 User（12.3 冻结：缺省/null = 不修改；PATCH {} → noop）。

        name / status 可变；id 不可变（schema extra=forbid → 携带 id 直接 422）。
        """
        try:
            user = service.update_user(user_id, UpdateUser(**payload.model_dump()))
        except ApplicationError as exc:
            raise _map_application_error(exc) from exc
        return UserResponse.model_validate(user)

    return router
