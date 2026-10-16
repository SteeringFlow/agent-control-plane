"""Channel Management HTTP API（Step 11.8，按 11.7 审计裁决落地）。

端点面（11.8 冻结；无 DELETE——软下线 = PATCH status；不做 webhook / health /
routing / connection / channel registry / credential vault / channel secret 表）：

    POST   /api/channels         201 / 409 / 422
    GET    /api/channels         200
    GET    /api/channels/{id}    200 / 404
    PATCH  /api/channels/{id}    200 / 404 / 409 / 422

职责边界（与 api/management/agents.py 同一注入纪律）：

    FastAPI（本模块：HTTP 事实提取 + 应答序列化 + Application 错误→状态码映射）
        ↓
    ChannelApplicationService（业务规则唯一裁决点：非空校验、(type,name)
    去重守卫、config merge 语义）
        ↓
    UoW Port → Persistence Adapter → DB

本模块不做业务判断——错误映射是唯一的 API 层职责：

    DuplicateChannelError     → 409 Conflict
    ChannelNotFoundError      → 404 Not Found
    InvalidChannelInputError  → 422 Unprocessable Entity

Secret 契约（11.8 合同 #4）：Create / Patch 请求可携带敏感 config 键（首次配置
需要录入 secret），一切响应经 ChannelResponse 的 sanitize_channel_config()
脱敏——token / encoding_aes_key / corpsecret 永不出现在任何响应中。

与 api/runtime/channels.py（入站 webhook 边界）互不混合：管理面归本模块，
运行时入站协议归 Runtime——两边界保持隔离（用户 11.8 指令）。

本模块不 import SQLAlchemy / app.persistence（经构造注入装配好的 Service，
由 main.py 组合根绑定 SqlAlchemyUnitOfWork；测试注入内存库实例）。
"""

from fastapi import APIRouter, Depends, HTTPException

from app.application.channels import (
    ChannelApplicationService,
    CreateChannel,
    UpdateChannel,
)
from app.application.errors import (
    ApplicationError,
    ChannelNotFoundError,
    DuplicateBotChannelError,
    DuplicateChannelError,
    InvalidChannelInputError,
)
from app.schemas.channels import (
    ChannelCreate,
    ChannelListResponse,
    ChannelResponse,
    ChannelUpdate,
)


def build_channels_api(
    service: ChannelApplicationService | None = None, *, service_provider=None
) -> APIRouter:
    """构造 Channel 管理 router（依赖注入：传入装配好的 ChannelApplicationService）。

    Cloud-0.1.1-C：可选 `service_provider`（FastAPI 依赖）——组合根用它把
    「请求解析出的 TenantScope → tenant-scoped Service」接进来；不传则退回
    注入的 `service`（测试 / OSS 单租户路径，行为不变）。
    """
    router = APIRouter(prefix="/api/channels", tags=["management-channels"])

    if service_provider is None:
        def _provider() -> ChannelApplicationService:
            assert service is not None, "build_channels_api 需要 service 或 service_provider"
            return service
    else:
        _provider = service_provider
    service_dep = Depends(_provider)

    def _map_application_error(exc: ApplicationError) -> HTTPException:
        """Application 错误 → HTTP 状态码（唯一映射点，11.8 裁决表）。

        M7-AUI-10.6.7：Bot 身份冲突用**结构化 detail**（{code, message}）——
        前端需可靠区分「Bot 身份冲突」与「同名冲突」并以 code 分支提示
        「复用已有渠道」，不能靠模糊文案匹配。既有 DuplicateChannelError 保持
        纯字符串 detail（向后兼容）。message 只含冲突渠道 name/id，不含
        bot_id / bot_secret / config。
        """
        if isinstance(exc, DuplicateBotChannelError):
            return HTTPException(status_code=409, detail={
                "code": "duplicate_bot_channel",
                "message": str(exc),
            })
        if isinstance(exc, DuplicateChannelError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, ChannelNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, InvalidChannelInputError):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=500, detail="internal management error")

    # ------------------------------------------------------------ POST /api/channels

    @router.post("", status_code=201, response_model=ChannelResponse)
    def create_channel(
        payload: ChannelCreate, service: ChannelApplicationService = service_dep
    ) -> ChannelResponse:
        """创建 Channel。完整 config 可入（首次配置需要 secret）；响应一律脱敏。"""
        try:
            channel = service.create_channel(CreateChannel(**payload.model_dump()))
        except ApplicationError as exc:
            raise _map_application_error(exc) from exc
        return ChannelResponse.model_validate(channel)

    # ------------------------------------------------------------ GET /api/channels

    @router.get("", response_model=ChannelListResponse)
    def list_channels(
        service: ChannelApplicationService = service_dep,
    ) -> ChannelListResponse:
        """列出全部 Channel（MVP 无分页/过滤）。响应一律脱敏。"""
        channels = service.list_channels()
        items = [ChannelResponse.model_validate(c) for c in channels]
        return ChannelListResponse(items=items, total=len(items))

    # ------------------------------------------------------------ GET /api/channels/{id}

    @router.get("/{channel_id}", response_model=ChannelResponse)
    def get_channel(
        channel_id: int, service: ChannelApplicationService = service_dep
    ) -> ChannelResponse:
        """读取单个 Channel；不存在 → 404。响应一律脱敏。"""
        channel = service.get_channel(channel_id)
        if channel is None:
            raise HTTPException(status_code=404, detail=f"Channel {channel_id} 不存在")
        return ChannelResponse.model_validate(channel)

    # ------------------------------------------------------------ PATCH /api/channels/{id}

    @router.patch("/{channel_id}", response_model=ChannelResponse)
    def patch_channel(
        channel_id: int, payload: ChannelUpdate,
        service: ChannelApplicationService = service_dep,
    ) -> ChannelResponse:
        """局部更新 Channel（缺省/null = 不修改）。

        config：merge 语义（合同 #3）——提供的键覆写、未提供的键（尤其密钥）保留；
        改 name / type 撞已有 (type, name) → 409。
        """
        try:
            channel = service.update_channel(
                channel_id, UpdateChannel(**payload.model_dump()))
        except ApplicationError as exc:
            raise _map_application_error(exc) from exc
        return ChannelResponse.model_validate(channel)

    return router
