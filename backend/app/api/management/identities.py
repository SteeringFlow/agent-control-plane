"""Identity Management HTTP API（Step 12.4，按 11.3/11.8/11.11 同一注入纪律落地）。

端点面（12.4 冻结；无 DELETE——管理态变更 = PATCH status）：

    POST   /api/identities         201 / 404 / 409 / 422
    GET    /api/identities         200
    GET    /api/identities/{id}    200 / 404
    PATCH  /api/identities/{id}    200 / 404 / 422

职责边界（与 api/management/agents.py、channels.py、connections.py 同一纪律）：

    FastAPI（本模块：HTTP 事实提取 + 应答序列化 + Application 错误→状态码映射）
        ↓
    IdentityApplicationService（业务规则唯一裁决点：User/Channel 存在性、
    (channel_id, external_id) 唯一性、非空校验、metadata dict 校验）
        ↓
    UoW Port → Persistence Adapter → DB

本模块不做业务判断——错误映射是唯一的 API 层职责：

    DuplicateIdentityError                              → 409 Conflict
    IdentityNotFoundError / UserNotFoundError /
    ChannelNotFoundError                                → 404 Not Found
    InvalidIdentityInputError                           → 422 Unprocessable Entity

创建依赖（12.4 目标）：User API / Channel API 先行——create_identity 依赖
user_id 与 channel_id 已存在（→ 404）。List 是绑定 UI 的陈列面（跨渠道同
external_id 如实陈列、不合并）。

**POST 的语义 = Create managed binding（Gate 3 §D 冻结）**：A（创建 External
Identity）与 B（绑定到 ACP User）原子合成一个用例，`user_id` 保持必填。
V0 **不暴露**独立 API：❌ 创建未绑定 Identity  ❌ Bind  ❌ Unbind
（Gate 2.6 D7 的语义拆分 ≠ API 数量必须增加）。
**响应体 `user_id` 可为 null**（未绑定；Gate 2.6 D1/D2）——纯放宽，
不是匿名 / 访客 / 系统用户。

仍然（12.4 冻结）：❌ DELETE  ❌ Identity Linking（绑定骨架 extra=forbid，
改绑定在合同面 422）❌ 自动创建 Identity  ❌ Runtime 调 Management API。

本模块不 import SQLAlchemy / app.persistence（经构造注入装配好的 Service，
由 main.py 组合根绑定 SqlAlchemyUnitOfWork；测试注入内存库实例）。
"""

from fastapi import APIRouter, Depends, HTTPException

from app.application.errors import (
    ApplicationError,
    ChannelNotFoundError,
    DuplicateIdentityError,
    IdentityNotFoundError,
    InvalidIdentityInputError,
    UserNotFoundError,
)
from app.application.identities import (
    CreateIdentity,
    IdentityApplicationService,
    UpdateIdentity,
)
from app.schemas.identities import (
    IdentityCreate,
    IdentityListResponse,
    IdentityResponse,
    IdentityUpdate,
)


def build_identities_api(
    service: IdentityApplicationService | None = None, *, service_provider=None
) -> APIRouter:
    """构造 Identity 管理 router（依赖注入：传入装配好的 IdentityApplicationService）。

    Cloud-0.1.1-C：可选 `service_provider`（FastAPI 依赖）——组合根用它把
    「请求解析出的 TenantScope → tenant-scoped Service」接进来；不传则退回
    注入的 `service`（测试 / OSS 单租户路径，行为不变）。
    """
    router = APIRouter(prefix="/api/identities", tags=["management-identities"])

    if service_provider is None:
        def _provider() -> IdentityApplicationService:
            assert service is not None, "build_identities_api 需要 service 或 service_provider"
            return service
    else:
        _provider = service_provider
    service_dep = Depends(_provider)

    def _map_application_error(exc: ApplicationError) -> HTTPException:
        """Application 错误 → HTTP 状态码（唯一映射点，12.4 裁决表）。"""
        if isinstance(exc, DuplicateIdentityError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, (IdentityNotFoundError, UserNotFoundError,
                            ChannelNotFoundError)):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, InvalidIdentityInputError):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=500, detail="internal management error")

    # ------------------------------------------------------------ POST /api/identities

    @router.post("", status_code=201, response_model=IdentityResponse)
    def create_identity(
        payload: IdentityCreate, service: IdentityApplicationService = service_dep
    ) -> IdentityResponse:
        """创建 Identity（外部身份 → User 的绑定）。

        User / Channel 不存在 → 404；(channel_id, external_id) 已占用 → 409；
        status 由领域默认 active（变更走 PATCH）。
        """
        try:
            identity = service.create_identity(
                CreateIdentity(**payload.model_dump()))
        except ApplicationError as exc:
            raise _map_application_error(exc) from exc
        return IdentityResponse.model_validate(identity)

    # ------------------------------------------------------------ GET /api/identities

    @router.get("", response_model=IdentityListResponse)
    def list_identities(
        service: IdentityApplicationService = service_dep,
    ) -> IdentityListResponse:
        """列出全部 Identity（MVP 无分页/过滤；跨渠道同 external_id 不合并）。"""
        identities = service.list_identities()
        items = [IdentityResponse.model_validate(i) for i in identities]
        return IdentityListResponse(items=items, total=len(items))

    # ------------------------------------------------------------ GET /api/identities/{id}

    @router.get("/{identity_id}", response_model=IdentityResponse)
    def get_identity(
        identity_id: int, service: IdentityApplicationService = service_dep
    ) -> IdentityResponse:
        """读取单个 Identity；不存在 → 404。"""
        identity = service.get_identity(identity_id)
        if identity is None:
            raise HTTPException(
                status_code=404, detail=f"Identity {identity_id} 不存在")
        return IdentityResponse.model_validate(identity)

    # ------------------------------------------------------------ PATCH /api/identities/{id}

    @router.patch("/{identity_id}", response_model=IdentityResponse)
    def patch_identity(
        identity_id: int, payload: IdentityUpdate,
        service: IdentityApplicationService = service_dep,
    ) -> IdentityResponse:
        """局部更新 Identity 档案字段（12.3 冻结：缺省/null = 不修改；PATCH {} → noop）。

        可变字段仅 display_name / status / metadata（提供 = 整段替换）；
        user_id / channel_id / external_id 不可变（schema extra=forbid →
        改绑定在合同面直接 422；改绑定 = Identity Linking，绝不引入）。
        """
        try:
            identity = service.update_identity(
                identity_id, UpdateIdentity(**payload.model_dump()))
        except ApplicationError as exc:
            raise _map_application_error(exc) from exc
        return IdentityResponse.model_validate(identity)

    return router
