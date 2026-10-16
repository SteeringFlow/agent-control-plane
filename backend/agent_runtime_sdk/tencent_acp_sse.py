"""Agent Runtime SDK —— Tencent ACP SSE Binding（Step 15.5.5.3）。

把 CP 侧 ACPAdapter（`app/adapters/agents/acp.py`）已验证的 Tencent ACP
SSE / JSON-RPC wire semantics 封装为**独立** SDK binding —— 契约对偶、实现
独立、绝不 import `app.*`。Binding 理解 Tencent wire；SDK Core 不理解。

Wire semantics（12.8.6 真实 wire 校准结论，结构化信号，无自然语言启发式）：
- Inbound:  JSON-RPC `session/prompt`：`params.sessionId` + 单 text 块 prompt；
- Outbound: `sessionUpdate == agent_message_chunk / agent_message_delta` → 正文；
            `sessionUpdate == agent_thought_chunk / agent_thought / thought_chunk`
            → 推理；块级结构信号（`type ∈ reasoning*` / `role ∈ reasoning*` /
            `reasoning: true`）→ 推理；
- Final:    `codebuddy.ai.isFinalStop` / `isFinalStop` = true、
            `type ∈ {turn_finished, session/endTurn, endTurn}`、
            `stopReason == "end_turn"` —— 全部归一化为**唯一** `TurnEnd`。

Mapping（15.5.5.3 冻结）：
    Tencent thought event → ReasoningChunk
    Tencent message event → ContentChunk
    Tencent final event   → TurnEnd
    wire sessionId        ↔ AgentRequest.external_session_id（pass-through，
                            SDK 不缓存 session、不提供 SessionStore）

Fail-closed / 显式处理（D11/D12）：
- malformed SSE frame（非 JSON / JSON 非对象 / 空帧）→ `TencentBindingError`；
- 无法识别的 `sessionUpdate` kind（含裸 text 块、未知 kind 携带 text）→
  **显式 raise**，绝不凭猜测归为 ContentChunk（宁可失败，不静默删内容）；
- JSON-RPC error 帧 → `TencentRpcError`（结构化 code/message/data；
  `str()` 不包含 `data`，不携带任何凭据）。

Canonical emission（encode 方向）：SDK 发出的 canonical 事件形态
（CP 侧解析为递归 walk，可容忍任意嵌套）：
    ContentChunk   → {"sessionUpdate": "agent_message_chunk",
                      "content": {"type": "text", "text": ...}}
    ReasoningChunk → {"sessionUpdate": "agent_thought_chunk",
                      "content": {"type": "text", "text": ...}}
    TurnEnd        → {"type": "session/endTurn", "stopReason": "end_turn"}

Transport 表达（不绑定任何 HTTP client）：`new_connection_id()` 生成
`uuid4().hex` 形态的 `Acp-Connection-Id`（不持久化）；`build_request_headers()`
构造 Bearer 头 —— token 只作瞬时参数进入 header 值，绝不写入任何模型、
错误信息或持久化结构。
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from .models import AgentRequest, ContentChunk, ReasoningChunk, TurnEnd
from .protocols import Chunk

__all__ = [
    "TencentBindingError",
    "TencentRpcError",
    "build_request_headers",
    "build_session_prompt",
    "decode_prompt",
    "encode_chunk",
    "encode_sse_frame",
    "handle_stream",
    "new_connection_id",
    "parse_sse_frame",
    "parse_sse_stream",
]

PROTOCOL = "tencent-acp-sse"

#: message/content 的结构化信号（12.8.6：真实 wire 两种正文形态）。
_MESSAGE_UPDATES = frozenset({"agent_message_chunk", "agent_message_delta"})
#: thought/reasoning 的结构化信号（12.8.6：真实 wire 三种 thought kind）。
_THOUGHT_UPDATES = frozenset({
    "agent_thought_chunk",
    "agent_thought",
    "thought_chunk",
})
#: 块级 reasoning type 候选（Case A 结构信号；兼容 OpenAI-ish 命名）。
_REASONING_TYPES = frozenset({
    "reasoning",
    "reasoning_content",
    "thinking",
    "thought",
    "thinking_content",
})
#: 块级 reasoning role 候选（Case B 结构信号）。
_REASONING_ROLES = frozenset({"reasoning", "thinking", "thought"})
#: final 标记的 type 候选（`codebuddy.ai.isFinalStop`/`isFinalStop`/`stopReason`
#: 由 `_contains_final` 单独识别）。
_FINAL_TYPES = frozenset({"turn_finished", "session/endTurn", "endTurn"})


class TencentBindingError(ValueError):
    """Tencent ACP wire 违反冻结语义（Fail Closed；消息为静态文案，无凭据）。"""


class TencentRpcError(TencentBindingError):
    """结构化 JSON-RPC error 归一化（D13）。

    `code` / `message` / `data` 来自 wire error 对象；`str()` 只含 code 与
    message，**不含 data**，绝不携带凭据。
    """

    def __init__(self, code: Any, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"jsonrpc error {code!r}: {message}")


# ================================================================== inbound：session/prompt → AgentRequest


def decode_prompt(payload: Any) -> AgentRequest:
    """JSON-RPC `session/prompt` → AgentRequest（Fail Closed）。

    `params.sessionId` → `external_session_id`；prompt 中 text 块的 text →
    `content`（冻结契约：单 text 块；多块按顺序拼接属宽容解码）。
    `agent_id` / `control_plane_session_id` / `metadata` 恒为 None。
    """
    if not isinstance(payload, dict):
        raise TencentBindingError("session/prompt payload must be a JSON object")
    if payload.get("method") != "session/prompt":
        raise TencentBindingError(
            f"unsupported jsonrpc method: {payload.get('method')!r}")
    params = payload.get("params")
    if not isinstance(params, dict):
        raise TencentBindingError("session/prompt params must be a JSON object")
    session_id = params.get("sessionId")
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id
    ):
        raise TencentBindingError(
            "'sessionId' must be a non-empty string or null")
    prompt = params.get("prompt")
    if not isinstance(prompt, list):
        raise TencentBindingError("session/prompt 'prompt' must be a list")
    texts = [
        block["text"]
        for block in prompt
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    if not texts:
        raise TencentBindingError(
            "session/prompt carries no text block")
    return AgentRequest(content="".join(texts), external_session_id=session_id)


def build_session_prompt(
    content: str, session_id: str | None, request_id: int = 1,
) -> dict[str, Any]:
    """构造 JSON-RPC `session/prompt` 请求（与 CP ACPAdapter 发送形态对偶）。"""
    if not isinstance(content, str):
        raise TencentBindingError("'content' must be a string")
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id
    ):
        raise TencentBindingError("'session_id' must be a non-empty string or null")
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/prompt",
        "params": {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": content}],
        },
    }


# ================================================================== outbound：Chunk → wire


def encode_chunk(chunk: Chunk) -> dict[str, Any]:
    """Chunk → canonical Tencent wire event dict（只接受三种冻结 Chunk）。"""
    if isinstance(chunk, ContentChunk):
        if not isinstance(chunk.content, str):
            raise TencentBindingError("ContentChunk.content must be a string")
        return {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": chunk.content},
        }
    if isinstance(chunk, ReasoningChunk):
        if not isinstance(chunk.content, str):
            raise TencentBindingError("ReasoningChunk.content must be a string")
        return {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": chunk.content},
        }
    if isinstance(chunk, TurnEnd):
        return {"type": "session/endTurn", "stopReason": "end_turn"}
    raise TencentBindingError("tencent binding can only encode frozen Chunks")


def encode_sse_frame(chunk: Chunk) -> str:
    """Chunk → canonical SSE frame（`data: {...}` + 空行收尾）。"""
    return f"data: {json.dumps(encode_chunk(chunk), ensure_ascii=False)}\n\n"


async def handle_stream(payload: Any, agent: Any) -> AsyncIterator[str]:
    """一次 Tencent wire 调用：decode_prompt → agent.stream_turn → SSE 帧流。"""
    request = decode_prompt(payload)
    async for chunk in agent.stream_turn(request):
        yield encode_sse_frame(chunk)


# ================================================================== inbound：SSE frame → Chunk


def parse_sse_frame(raw: str) -> list[Chunk]:
    """**单条** SSE `data:` 帧 → Chunk 列表（顺序保持；final 归一化为唯一 TurnEnd）。

    Fail Closed：
    - 非 str / 空帧 / 非法 JSON / JSON 非 object → `TencentBindingError`；
    - JSON-RPC error 帧 → `TencentRpcError`（结构化归一化）；
    - 无法识别的事件（未知 `sessionUpdate` kind、无信封的裸 text 块）→
      显式 raise，绝不猜测为 content（D12）。
    多帧流请用 `parse_sse_stream`。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise TencentBindingError("SSE frame must be a non-empty string")
    data = raw.strip()
    if data.startswith("data:"):
        data = data[5:].strip()
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as exc:
        raise TencentBindingError("malformed SSE frame: invalid JSON") from exc
    if not isinstance(obj, dict):
        raise TencentBindingError("SSE frame JSON must be an object")
    return _parse_event_object(obj)


def parse_sse_stream(raw: str) -> list[Chunk]:
    """SSE **多帧流**（逐 `data:` 行）→ Chunk 列表（顺序保持）。

    空行与 `:` 注释行跳过；出现非 `data:` 的非空行 → fail closed；
    流中没有任何 data 帧 → fail closed。其余语义与 `parse_sse_frame` 一致。
    """
    if not isinstance(raw, str):
        raise TencentBindingError("SSE stream must be a string")
    chunks: list[Chunk] = []
    seen = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            continue
        if not stripped.startswith("data:"):
            raise TencentBindingError(
                "SSE stream contains a non-'data:' line")
        payload = stripped[5:].strip()
        seen = True
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise TencentBindingError(
                "malformed SSE frame: invalid JSON") from exc
        if not isinstance(obj, dict):
            raise TencentBindingError("SSE frame JSON must be an object")
        chunks.extend(_parse_event_object(obj))
    if not seen:
        raise TencentBindingError("SSE stream carries no data frames")
    return chunks


def _parse_event_object(obj: dict) -> list[Chunk]:
    """已解析的事件 JSON object → Chunk 列表（frame/stream 共用）。"""
    error = _rpc_error_of(obj)
    if error is not None:
        code = error.get("code")
        message = error.get("message")
        if not isinstance(message, str):
            message = json.dumps(message, ensure_ascii=False)
        raise TencentRpcError(code, message, error.get("data"))

    chunks: list[Chunk] = []
    unknown_kinds: list[str] = []
    _walk_event(obj, chunks, None, unknown_kinds)
    if _contains_final(obj):
        chunks.append(TurnEnd())
    if not chunks:
        detail = f" (sessionUpdate={unknown_kinds!r})" if unknown_kinds else ""
        raise TencentBindingError(f"unrecognized Tencent ACP event{detail}")
    return chunks


def _rpc_error_of(obj: dict) -> dict | None:
    """递归寻找 JSON-RPC error 对象（与 CP 侧 `_json_rpc_error_text` 对偶）。"""
    if isinstance(obj, dict):
        if isinstance(obj.get("error"), dict):
            return obj["error"]
        for value in obj.values():
            found = _rpc_error_of(value) if isinstance(value, (dict, list)) else None
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _rpc_error_of(item) if isinstance(item, (dict, list)) else None
            if found is not None:
                return found
    return None


def _walk_event(
    node: Any,
    out: list[Chunk],
    kind_ctx: str | None,
    unknown_kinds: list[str],
) -> None:
    """递归遍历事件 JSON；只按结构化信号分类（无自然语言启发式）。

    `kind_ctx`：最近父级 `sessionUpdate` 信封的显式分类（message/thought）；
    None 表示无信封 —— 此时裸 text 块 fail closed（D12）。
    """
    if isinstance(node, dict):
        kind = node.get("sessionUpdate")
        if isinstance(kind, str):
            if kind in _MESSAGE_UPDATES:
                kind_ctx = "message"
            elif kind in _THOUGHT_UPDATES:
                kind_ctx = "thought"
            else:
                kind_ctx = None
                if kind not in unknown_kinds:
                    unknown_kinds.append(kind)
        btype = node.get("type")
        text = node.get("text")
        # Case A：块级独立 reasoning type（显式结构信号，优先于信封）。
        if isinstance(btype, str) and btype in _REASONING_TYPES:
            if isinstance(text, str) and text:
                out.append(ReasoningChunk(text))
            return
        if btype == "text" and isinstance(text, str) and text:
            # Case B：块级 role / reasoning 布尔信号（显式，优先于信封）。
            role = node.get("role")
            if (isinstance(role, str) and role.lower() in _REASONING_ROLES) \
                    or node.get("reasoning") is True:
                out.append(ReasoningChunk(text))
                return
            meta = node.get("metadata")
            if isinstance(meta, dict) and meta.get("reasoning") is True:
                out.append(ReasoningChunk(text))
                return
            # Case C：无块级信号时，只信任显式 message/thought 信封。
            if kind_ctx == "message":
                out.append(ContentChunk(text))
            elif kind_ctx == "thought":
                out.append(ReasoningChunk(text))
            else:
                raise TencentBindingError(
                    "text block without explicit message/thought envelope "
                    "(fail closed, not guessed as content)")
            return
        for value in node.values():
            _walk_event(value, out, kind_ctx, unknown_kinds)
    elif isinstance(node, list):
        for item in node:
            _walk_event(item, out, kind_ctx, unknown_kinds)


def _contains_final(node: Any) -> bool:
    """递归识别 final 标记（与 CP 侧 `_json_contains_final` 同一四种形态）。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("codebuddy.ai.isFinalStop", "isFinalStop") and value is True:
                return True
            if key == "type" and value in _FINAL_TYPES:
                return True
            if key == "stopReason" and value == "end_turn":
                return True
            if _contains_final(value):
                return True
    elif isinstance(node, list):
        for item in node:
            if _contains_final(item):
                return True
    return False


# ================================================================== transport 表达（不绑定 HTTP client）


def new_connection_id() -> str:
    """生成 `Acp-Connection-Id`（`uuid4().hex` 形态；不持久化、不复用）。"""
    return uuid.uuid4().hex


def build_request_headers(
    bearer_token: str, connection_id: str, *, accept: str = "text/event-stream",
) -> dict[str, str]:
    """构造 Tencent SSE GET 请求头。

    Bearer token 只作瞬时参数进入 header 值；任何失败路径的异常消息都是
    静态文案，绝不回显 token。
    """
    if not isinstance(bearer_token, str) or not bearer_token.strip():
        raise TencentBindingError("'bearer_token' must be a non-empty string")
    if not isinstance(connection_id, str) or not connection_id:
        raise TencentBindingError("'connection_id' must be a non-empty string")
    return {
        "Authorization": f"Bearer {bearer_token}",
        "Acp-Connection-Id": connection_id,
        "Accept": accept,
    }
