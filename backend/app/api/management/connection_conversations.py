"""Grant Management HTTP API（Implementation Package 2 / Step 3，按 B4.3/B4.4 落地）。

端点面（B4.3 冻结形态；**独立资源**，不嵌在 Connection 下——ADR-2 红线：
Connection 域不得长成授权对象）：

    POST   /api/connection-conversations                       201 / 404 / 409 / 422
    GET    /api/connection-conversations?connection_id={id}    200
    DELETE /api/connection-conversations/{id}                  204 / 404
    ──
    GET    /api/channels/{channel_id}/known-conversations      200 / 404（B4.5 投影）

**没有 Update**：行不可变（B4.2——改授权 = 删 + 建）。

职责边界（与 api/management/identities.py 同一纪律）：

    FastAPI（本模块：HTTP 事实提取 + 应答序列化 + Application 错误→状态码映射）
        ↓
    ConnectionConversationApplicationService（业务规则唯一裁决点）
        ↓
    UoW Port → Persistence Adapter → DB

错误映射（B4.4：沿用既有先例，不发明）：

    DuplicateConnectionConversationError                   → 409 Conflict
    ConnectionConversationNotFoundError /
    ChannelNotFoundError / ConnectionNotFoundError         → 404 Not Found
    InvalidConnectionConversationInputError                → 422 Unprocessable Entity

**known-conversations 是 projection only**（本 Slice 红线）：它只回答「这个
Channel 过去出现过哪些会话键」；绝不创建 grant，更不把「出现在 Discovery 中」
当作「已授权」（known conversation ≠ authorized conversation）。

本模块不 import SQLAlchemy / app.persistence（经构造注入装配好的 Service，
由 main.py 组合根绑定 tenant-scoped UoW；测试注入内存库实例）。
"""

from fastapi import APIRouter, Depends, HTTPException

from app.application.connection_conversations import (
    ConnectionConversationApplicationService,
    CreateConnectionConversation,
)
from app.application.errors import (
    ApplicationError,
    ChannelNotFoundError,
    ConnectionConversationNotFoundError,
    ConnectionNotFoundError,
    DuplicateConnectionConversationError,
    InvalidConnectionConversationInputError,
)
from app.schemas.connection_conversations import (
    ConnectionConversationCreate,
    ConnectionConversationListResponse,
    ConnectionConversationResponse,
    KnownConversationsResponse,
)


def build_connection_conversations_api(
    service: ConnectionConversationApplicationService | None = None,
    *,
    service_provider=None,
) -> APIRouter:
    """构造 Grant 管理 router（依赖注入：传入装配好的 Service 或 tenant-scoped provider）。"""
    router = APIRouter(tags=["management-authorization-grants"])

    if service_provider is None:
        def _provider() -> ConnectionConversationApplicationService:
            assert service is not None, \
                "build_connection_conversations_api 需要 service 或 service_provider"
            return service
    else:
        _provider = service_provider
    service_dep = Depends(_provider)

    def _map_application_error(exc: ApplicationError) -> HTTPException:
        """Application 错误 → HTTP 状态码（唯一映射点，B4.4 裁决表）。"""
        if isinstance(exc, DuplicateConnectionConversationError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, (ConnectionConversationNotFoundError,
                            ChannelNotFoundError, ConnectionNotFoundError)):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, InvalidConnectionConversationInputError):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=500, detail="internal management error")

    # ------------------------------------------------ POST /api/connection-conversations

    @router.post("/api/connection-conversations", status_code=201,
                 response_model=ConnectionConversationResponse)
    def create_connection_conversation(
        payload: ConnectionConversationCreate,
        service: ConnectionConversationApplicationService = service_dep,
    ) -> ConnectionConversationResponse:
        """创建授权（Conversation → Connection 的 ALLOW 行）。

        channel / connection 不存在 → 404；重复授权 → 409；非法输入 → 422。
        绝不顺带创建 Channel / Connection / 会话。
        """
        try:
            grant = service.create_connection_conversation(
                CreateConnectionConversation(**payload.model_dump()))
        except ApplicationError as exc:
            raise _map_application_error(exc) from exc
        return ConnectionConversationResponse.model_validate(grant)

    # ------------------------------------------------ GET /api/connection-conversations

    @router.get("/api/connection-conversations",
                response_model=ConnectionConversationListResponse)
    def list_connection_conversations(
        connection_id: int, service: ConnectionConversationApplicationService = service_dep,
    ) -> ConnectionConversationListResponse:
        """列出一条 Connection 的全部授权（回显「它在哪工作」；MVP 无分页）。

        只返回**显式授权行**——绝不混入 known-conversations 投影。
        """
        try:
            grants = service.list_connection_conversations(connection_id)
        except ApplicationError as exc:
            raise _map_application_error(exc) from exc
        items = [ConnectionConversationResponse.model_validate(g) for g in grants]
        return ConnectionConversationListResponse(items=items, total=len(items))

    # ------------------------------------------ DELETE /api/connection-conversations/{id}

    @router.delete("/api/connection-conversations/{connection_conversation_id}",
                   status_code=204)
    def delete_connection_conversation(
        connection_conversation_id: int,
        service: ConnectionConversationApplicationService = service_dep,
    ) -> None:
        """撤销一条授权（行不可变：改授权 = 删 + 建）。不存在 → 404。"""
        try:
            service.delete_connection_conversation(connection_conversation_id)
        except ApplicationError as exc:
            raise _map_application_error(exc) from exc

    # --------------------------------- GET /api/channels/{id}/known-conversations（B4.5）

    @router.get("/api/channels/{channel_id}/known-conversations",
                response_model=KnownConversationsResponse)
    def list_known_conversations(
        channel_id: int, service: ConnectionConversationApplicationService = service_dep,
    ) -> KnownConversationsResponse:
        """只读投影：该 Channel 过去出现过哪些会话键（来自 sessions，distinct）。

        **projection only**：绝不创建 grant；出现在 Discovery 中**不代表**
        已授权——授权只能经 POST 显式成立（known ≠ authorized）。
        """
        try:
            conversations = service.list_known_conversations(channel_id)
        except ApplicationError as exc:
            raise _map_application_error(exc) from exc
        return KnownConversationsResponse(
            channel_id=channel_id, conversations=conversations)

    return router
