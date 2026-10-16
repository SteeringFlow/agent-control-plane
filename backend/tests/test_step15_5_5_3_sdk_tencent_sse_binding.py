"""Step 15.5.5.3 —— Tencent ACP SSE Binding 契约测试（D1~D18）。

覆盖面（15.5.5.3 冻结）：
- D1  session/prompt → AgentRequest（content / external_session_id）
- D2  agent_message_chunk / delta → ContentChunk
- D3  agent_thought_chunk / agent_thought / thought_chunk → ReasoningChunk
- D4  final marker → TurnEnd
- D5  四种已知 final marker 全部归一化为唯一 TurnEnd
- D6  sessionId ↔ external_session_id round-trip（不缓存 session）
- D7  多个 content chunk 顺序保持
- D8  多个 reasoning chunk 顺序保持
- D9  reasoning/content 混合顺序保持
- D10 reasoning 不泄漏进 content
- D11 malformed SSE frame fail closed
- D12 unknown event 显式失败，绝不猜测为 content
- D13 JSON-RPC error 结构化归一化
- D14 Acp-Connection-Id 格式（uuid4().hex）
- D15 Bearer header 构造且 token 不泄漏
- D16 dependency closure stdlib-only
- D17 无 ACP backend import
- D18 无 SessionStore / session 持久化
"""

import ast
import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

import agent_runtime_sdk
from agent_runtime_sdk import (
    AgentRequest,
    ContentChunk,
    ReasoningChunk,
    TencentBindingError,
    TencentRpcError,
    TurnEnd,
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

# ================================================================== wire 帧工厂（测试内，模拟真实 Tencent 形态）


def _frame(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _message_frame(text: str) -> str:
    return _frame({
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": text},
    })


def _thought_frame(text: str) -> str:
    return _frame({
        "sessionUpdate": "agent_thought_chunk",
        "content": {"type": "text", "text": text},
    })


# ================================================================== D1 session/prompt mapping


def test_d1_decode_prompt_maps_session_prompt():
    payload = build_session_prompt("hello", "rt-123")
    req = decode_prompt(payload)
    assert isinstance(req, AgentRequest)
    assert req.content == "hello"
    assert req.external_session_id == "rt-123"
    assert req.agent_id is None
    assert req.control_plane_session_id is None
    assert req.metadata is None


def test_d1_decode_prompt_fails_closed_on_bad_shape():
    with pytest.raises(TencentBindingError):
        decode_prompt({"method": "other/method", "params": {}})
    with pytest.raises(TencentBindingError):
        decode_prompt({"method": "session/prompt", "params": {"prompt": []}})
    with pytest.raises(TencentBindingError):
        decode_prompt({"method": "session/prompt"})
    with pytest.raises(TencentBindingError):
        decode_prompt({"method": "session/prompt", "params": {"prompt": "x"}})


# ================================================================== D2/D3/D4 event → Chunk


def test_d2_message_event_maps_to_content_chunk():
    assert parse_sse_frame(_message_frame("hi")) == [ContentChunk("hi")]
    delta = _frame({
        "sessionUpdate": "agent_message_delta",
        "content": {"type": "text", "text": "hi"},
    })
    assert parse_sse_frame(delta) == [ContentChunk("hi")]


def test_d3_thought_events_map_to_reasoning_chunk():
    for kind in ("agent_thought_chunk", "agent_thought", "thought_chunk"):
        frame = _frame({
            "sessionUpdate": kind,
            "content": {"type": "text", "text": "thinking"},
        })
        assert parse_sse_frame(frame) == [ReasoningChunk("thinking")], kind


def test_d4_final_event_maps_to_turn_end():
    frame = _frame({"type": "session/endTurn", "stopReason": "end_turn"})
    assert parse_sse_frame(frame) == [TurnEnd()]


# ================================================================== D5 final 归一化


@pytest.mark.parametrize(
    "event",
    [
        {"codebuddy.ai.isFinalStop": True},
        {"isFinalStop": True},
        {"type": "turn_finished"},
        {"type": "session/endTurn"},
        {"type": "endTurn"},
        {"stopReason": "end_turn"},
        # 深层嵌套（CP 侧真实观察形态）
        {"update": {"meta": {"isFinalStop": True}}},
    ],
)
def test_d5_all_final_markers_normalize_to_single_turn_end(event):
    chunks = parse_sse_frame(_frame(event))
    assert chunks == [TurnEnd()]
    assert sum(isinstance(c, TurnEnd) for c in chunks) == 1


# ================================================================== D6 sessionId round-trip


def test_d6_session_id_round_trip_without_sdk_state():
    # wire sessionId → external_session_id →（Agent 透传）→ 构造回 wire
    req = decode_prompt(build_session_prompt("m", "rt-abc"))
    assert req.external_session_id == "rt-abc"
    rebuilt = build_session_prompt("m2", req.external_session_id)
    assert rebuilt["params"]["sessionId"] == "rt-abc"
    # None 态同样 round-trip
    req_null = decode_prompt(build_session_prompt("m", None))
    assert req_null.external_session_id is None
    # SDK 不缓存：同一 payload 两次 decode 产出独立实例
    payload = build_session_prompt("m", "rt-abc")
    assert decode_prompt(payload) is not decode_prompt(payload)


# ================================================================== D7/D8/D9 顺序保持


def test_d7_multiple_content_chunks_preserve_order():
    chunks = parse_sse_stream(
        _message_frame("a") + _message_frame("b") + _message_frame("c")
    )
    assert chunks == [ContentChunk("a"), ContentChunk("b"), ContentChunk("c")]


def test_d8_multiple_reasoning_chunks_preserve_order():
    chunks = parse_sse_stream(
        _thought_frame("t1") + _thought_frame("t2") + _thought_frame("t3")
    )
    assert chunks == [ReasoningChunk("t1"), ReasoningChunk("t2"), ReasoningChunk("t3")]


def test_d9_mixed_ordering_preserved():
    stream = (
        _thought_frame("think-1")
        + _message_frame("say-1")
        + _thought_frame("think-2")
        + _message_frame("say-2")
        + _frame({"type": "turn_finished"})
    )
    chunks = parse_sse_stream(stream)
    assert chunks == [
        ReasoningChunk("think-1"),
        ContentChunk("say-1"),
        ReasoningChunk("think-2"),
        ContentChunk("say-2"),
        TurnEnd(),
    ]


def test_d9_encode_round_trip_preserves_chunk_stream():
    """encode → parse 对偶 round-trip：canonical 形态可被自身 parser 消费。"""
    frames = (
        encode_sse_frame(ReasoningChunk("r"))
        + encode_sse_frame(ContentChunk("c"))
        + encode_sse_frame(TurnEnd())
    )
    assert parse_sse_stream(frames) == [ReasoningChunk("r"), ContentChunk("c"), TurnEnd()]
    assert encode_chunk(ContentChunk("c"))["sessionUpdate"] == "agent_message_chunk"
    assert encode_chunk(ReasoningChunk("r"))["sessionUpdate"] == "agent_thought_chunk"
    assert encode_chunk(TurnEnd()) == {"type": "session/endTurn", "stopReason": "end_turn"}


# ================================================================== D10 无 reasoning 泄漏


def test_d10_reasoning_never_leaks_into_content():
    stream = _thought_frame("secret thought") + _message_frame("public answer")
    chunks = parse_sse_stream(stream)
    contents = [c.content for c in chunks if isinstance(c, ContentChunk)]
    reasonings = [c.content for c in chunks if isinstance(c, ReasoningChunk)]
    assert contents == ["public answer"]
    assert reasonings == ["secret thought"]
    assert all("secret" not in c for c in contents)


# ================================================================== D11 malformed fail closed


@pytest.mark.parametrize(
    "bad_frame",
    [
        "",                       # 空帧
        "   ",                    # 纯空白
        "data: {bad json",        # 非法 JSON
        "data: [1,2,3]",          # JSON 非对象
        "data: \"just a string\"",
        42,                       # 非 str
        None,
    ],
)
def test_d11_malformed_frames_fail_closed(bad_frame):
    with pytest.raises(TencentBindingError) as exc_info:
        parse_sse_frame(bad_frame)
    assert not isinstance(exc_info.value, TencentRpcError)


# ================================================================== D12 unknown event 显式失败


@pytest.mark.parametrize(
    "unknown_frame",
    [
        # 未知 sessionUpdate kind 携带 text → 不得猜测为 content
        _frame({
            "sessionUpdate": "user_message_chunk",
            "content": {"type": "text", "text": "maybe content?"},
        }),
        # 无信封裸 text 块 → fail closed
        _frame({"type": "text", "text": "bare text"}),
        # 完全无法识别的事件
        _frame({"foo": "bar"}),
        _frame({"sessionUpdate": "something_new", "nested": {"x": 1}}),
    ],
)
def test_d12_unknown_events_fail_closed_not_guessed_as_content(unknown_frame):
    with pytest.raises(TencentBindingError) as exc_info:
        parse_sse_frame(unknown_frame)
    assert not isinstance(exc_info.value, TencentRpcError)
    assert "unrecognized" in str(exc_info.value) or "fail closed" in str(exc_info.value)


def test_d12_block_level_reasoning_signals_still_recognized():
    """显式块级结构信号（Case A/B）不受信封限制，仍归一化为 ReasoningChunk。"""
    assert parse_sse_frame(_frame({"type": "reasoning", "text": "r1"})) == [
        ReasoningChunk("r1")]
    assert parse_sse_frame(_frame({
        "type": "text", "role": "reasoning", "text": "r2"})) == [ReasoningChunk("r2")]
    assert parse_sse_frame(_frame({
        "type": "text", "text": "r3", "metadata": {"reasoning": True},
    })) == [ReasoningChunk("r3")]


# ================================================================== D13 JSON-RPC error 归一化


def test_d13_structured_rpc_error_normalization():
    frame = _frame({
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32000, "message": "session not found", "data": {"x": 1}},
    })
    with pytest.raises(TencentRpcError) as exc_info:
        parse_sse_frame(frame)
    err = exc_info.value
    assert err.code == -32000
    assert err.message == "session not found"
    assert err.data == {"x": 1}
    # str() 含 code/message 但不含 data
    assert "-32000" in str(err)
    assert "session not found" in str(err)
    assert "data" not in str(err)


def test_d13_nested_rpc_error_and_non_string_code():
    frame = _frame({"wrapper": {"error": {"code": "E-X", "message": "boom"}}})
    with pytest.raises(TencentRpcError) as exc_info:
        parse_sse_frame(frame)
    assert exc_info.value.code == "E-X"
    assert exc_info.value.message == "boom"


# ================================================================== D14 Acp-Connection-Id


def test_d14_connection_id_format_and_uniqueness():
    cid = new_connection_id()
    assert re.fullmatch(r"[0-9a-f]{32}", cid)          # uuid4().hex 形态
    assert cid != new_connection_id()                   # 不复用


# ================================================================== D15 Bearer header 且不泄漏


def test_d15_bearer_header_construction():
    cid = new_connection_id()
    token = "t0p-secret-token"
    headers = build_request_headers(token, cid)
    assert headers["Authorization"] == f"Bearer {token}"
    assert headers["Acp-Connection-Id"] == cid
    assert headers["Accept"] == "text/event-stream"


def test_d15_token_never_leaks_into_errors_or_state():
    token = "t0p-secret-token"
    # 空/非法 token → 静态错误文案，不回显 token
    for bad in ("", "   ", None, 42):
        with pytest.raises(TencentBindingError) as exc_info:
            build_request_headers(bad, "cid")  # type: ignore[arg-type]
        assert token not in str(exc_info.value)
    # 绑定层没有任何对象持有 token：全模块符号面无 token 存储
    import agent_runtime_sdk.tencent_acp_sse as tencent
    public = [n for n in dir(tencent) if not n.startswith("_")]
    assert "SessionStore" not in public
    assert all(not hasattr(getattr(tencent, n), "bearer_token")
               for n in public
               if callable(getattr(tencent, n)) and not n.startswith("Tencent"))


# ================================================================== D16/D17 dependency closure


_FORBIDDEN_ROOTS = {
    "app", "sqlalchemy", "fastapi", "flask", "redis",
    "httpx", "anyio", "uvicorn", "aiohttp", "requests", "pytest",
}


def _iter_top_level_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue
            roots.append((node.module or "").split(".")[0])
    return roots


def test_d16_sdk_dependency_closure_stays_stdlib_only():
    sdk_dir = Path(agent_runtime_sdk.__file__).resolve().parent
    for py in sorted(sdk_dir.rglob("*.py")):
        for root in _iter_top_level_imports(py):
            assert root not in _FORBIDDEN_ROOTS, f"{py.name}: forbidden '{root}'"
            assert root in sys.stdlib_module_names or root == "agent_runtime_sdk", (
                f"{py.name}: non-stdlib '{root}'")


def test_d17_no_acp_backend_import():
    for py in sorted(Path(agent_runtime_sdk.__file__).parent.rglob("*.py")):
        for root in _iter_top_level_imports(py):
            assert root != "app", f"{py.name}: imports ACP backend 'app'"


# ================================================================== D18 无 SessionStore / 无 session 持久化


def test_d18_no_sessionstore_no_session_persistence():
    import agent_runtime_sdk.tencent_acp_sse as tencent

    # 无 SessionStore 符号；binding 无任何可变 session 容器
    assert not hasattr(tencent, "SessionStore")
    for name in ("session_store", "_sessions", "SESSIONS", "_session_cache"):
        assert not hasattr(tencent, name)
    # decode/encode 全部纯函数：模型 frozen，无可变状态
    req = decode_prompt(build_session_prompt("m", "s1"))
    with pytest.raises(Exception):
        req.external_session_id = "mutated"  # type: ignore[misc]


# ================================================================== handle_stream（stream_turn 面向）


class _StreamingAgent:
    async def stream_turn(self, request: AgentRequest):
        yield ReasoningChunk("t")
        yield ContentChunk(request.content)
        yield TurnEnd()


def test_handle_stream_decodes_prompt_and_emits_sse_frames():
    payload = build_session_prompt("hi", "rt-1")
    frames = asyncio.run(_collect(payload))
    events = [json.loads(f.removeprefix("data: ").strip()) for f in frames]
    assert events[0]["sessionUpdate"] == "agent_thought_chunk"
    assert events[1]["sessionUpdate"] == "agent_message_chunk"
    assert events[1]["content"]["text"] == "hi"
    assert events[2] == {"type": "session/endTurn", "stopReason": "end_turn"}


async def _collect(payload) -> list[str]:
    return [frame async for frame in handle_stream(payload, _StreamingAgent())]
