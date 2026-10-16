"""Agent Runtime SDK（Step 15.5.5）—— Agent-side Runtime SDK Core。

独立 namespace（与 `backend/app` 平级的顶层包），给 Agent 作者实现
「被 Control Plane 的 AgentAdapter 调用的 Agent 服务」用的最小 wire 契约
与 Protocol 形状。

Hard Rules（15.5.3 冻结，机器化锁定）：
- 绝不 import `backend/app`（`app.*`）——依赖闭包只允许标准库 + 本包相对导入；
- 不含 ACP Domain / SQLAlchemy / persistence / SessionStore / Redis /
  FastAPI / serve() / HTTP hosting / Management Client / CLI；
- `control_plane_session_id` 与 `external_session_id` 是两个不同概念；
- `external_session_id` 只做 input/output pass-through，SDK 不保存 Session；
- `Chunk` 只允许 ReasoningChunk / ContentChunk / TurnEnd。

Public surface：
    15.5.5.1（Core，冻结）：
        AgentRequest, AgentResponse, ReasoningChunk, ContentChunk, TurnEnd,
        Chunk, Agent, StreamingAgent
    15.5.5.2（Minimal Binding，冻结）：
        MinimalBindingError, decode_request, encode_response, handle
    15.5.5.3（Tencent ACP SSE Binding，冻结）：
        TencentBindingError, TencentRpcError, decode_prompt,
        build_session_prompt, parse_sse_frame, encode_chunk,
        encode_sse_frame, handle_stream, new_connection_id,
        build_request_headers
"""

from .minimal import (
    MinimalBindingError,
    decode_request,
    encode_response,
    handle,
)
from .models import (
    AgentRequest,
    AgentResponse,
    Artifact,
    ContentChunk,
    ReasoningChunk,
    TurnEnd,
)
from .protocols import Agent, Chunk, StreamingAgent
from .tencent_acp_sse import (
    TencentBindingError,
    TencentRpcError,
    build_request_headers,
    build_session_prompt,
    decode_prompt,
    encode_chunk,
    encode_sse_frame,
    handle_stream,
    new_connection_id,
    parse_sse_frame,
    parse_sse_stream,
)

__all__ = [
    "Agent",
    "AgentRequest",
    "AgentResponse",
    "Artifact",
    "Chunk",
    "ContentChunk",
    "MinimalBindingError",
    "ReasoningChunk",
    "StreamingAgent",
    "TencentBindingError",
    "TencentRpcError",
    "TurnEnd",
    "build_request_headers",
    "build_session_prompt",
    "decode_prompt",
    "decode_request",
    "encode_chunk",
    "encode_response",
    "encode_sse_frame",
    "handle",
    "handle_stream",
    "new_connection_id",
    "parse_sse_frame",
    "parse_sse_stream",
]
