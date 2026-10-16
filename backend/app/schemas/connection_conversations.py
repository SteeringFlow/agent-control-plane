"""Grant Management API 的请求 / 响应契约（Implementation Package 2 / Step 3）。

与 identities/connections schema 同一纪律：

- 请求体 `extra="forbid"`（多字段 / 拼错字段在合同面直接 422）；
- 响应体以 ID 形式回显绑定骨架（不 join 名称，名称解析由前端经各自 API 完成）；
- **没有 Update schema**——行不可变（B4.2：改授权 = 删 + 建）。
"""

from pydantic import BaseModel, ConfigDict


class ConnectionConversationCreate(BaseModel):
    """POST /api/connection-conversations 请求体。

    语义（B4.2 Create）：为一条**已存在**的 Connection 指定「它在哪个会话上
    工作」——channel / connection 必须已存在（不存在 → 404，不顺带创建）。
    """

    model_config = ConfigDict(extra="forbid")

    channel_id: int
    external_conversation_id: str
    connection_id: int


class ConnectionConversationResponse(BaseModel):
    """一条授权的响应体（单个 / 列表元素共用）。

    字段面 == 领域可见字段；行不可变，因此没有 PATCH 请求体。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: int
    external_conversation_id: str
    connection_id: int


class ConnectionConversationListResponse(BaseModel):
    """GET /api/connection-conversations?connection_id=… 的响应体。"""

    items: list[ConnectionConversationResponse]
    total: int


class KnownConversationsResponse(BaseModel):
    """GET /api/channels/{id}/known-conversations 的响应体（B4.5 投影）。

    **只是观察**：这些会话键出现过，不代表它们被授权
    （known conversation ≠ authorized conversation）。
    """

    channel_id: int
    conversations: list[str]
