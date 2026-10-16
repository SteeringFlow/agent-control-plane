"""Step 15.5.5.5 —— Agent Runtime SDK Contract Regression Fence。

本模块是 15.5.5.1~15.5.5.4 冻结契约的**最终机器化 fence**：不新增 SDK 功能、
不修改任何冻结 API，只为未来 SDK 修改建立不可无意破坏的契约线。

覆盖面：
一、Core Contract        —— 字段面/必填默认/frozen/Chunk union/Protocol 运行行为
二、Minimal Contract     —— mapping/round-trip/fail-closed/metadata 不泄漏/stateful
三、Tencent Contract     —— 全部冻结事件形态/顺序/reasoning-content 分离/final 归一化
四、Stateful + Streaming —— 三轮 lifecycle/每 turn 恰一 TurnEnd/跨 binding 共享 state
五、Architectural Guard  —— dependency closure / forbidden imports / forbidden symbols
六、Public Surface Guard —— __all__ 恰等于冻结面（新增即失败，防止无意扩面）
七、CP Isolation         —— 屏蔽 app.* 的子进程内跑通契约 smoke（不要求 backend 启动）

只测契约面（exact contract / guard / surface / integration fence），
不测内部实现细节（私有函数、私有结构、行数等）。
"""

import ast
import asyncio
import dataclasses
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import MISSING, fields
from pathlib import Path
from typing import get_args

import pytest

import agent_runtime_sdk
from agent_runtime_sdk import (
    Agent,
    AgentRequest,
    AgentResponse,
    Artifact,
    Chunk,
    ContentChunk,
    MinimalBindingError,
    ReasoningChunk,
    StreamingAgent,
    TencentBindingError,
    TencentRpcError,
    TurnEnd,
    build_session_prompt,
    decode_prompt,
    decode_request,
    encode_response,
    handle as minimal_handle,
    handle_stream,
    parse_sse_stream,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
SDK_DIR = Path(agent_runtime_sdk.__file__).resolve().parent
EXAMPLE_AGENT = (
    REPO_ROOT / "examples" / "agent-runtime-sdk" / "stateful-streaming" / "agent.py"
)

OPTIONAL = "MISSING_DEFAULT_GUARD"


# ================================================================== 一、Core Contract


def _field_contract(cls) -> None:
    """断言 frozen dataclass 的 exact 字段面：名字集 / 必填 / 默认值。"""
    fs = fields(cls)
    assert cls.__dataclass_params__.frozen is True
    required = [f.name for f in fs
                if f.default is MISSING and f.default_factory is MISSING]
    optional = {f.name: f.default for f in fs if f.default is not MISSING}
    return fs, required, optional


def test_core_agent_request_exact_contract():
    fs, required, optional = _field_contract(AgentRequest)
    assert {f.name for f in fs} == {
        "agent_id", "control_plane_session_id", "content",
        "external_session_id", "metadata",
    }
    assert required == ["content"]
    assert optional == {
        "agent_id": None, "control_plane_session_id": None,
        "external_session_id": None, "metadata": None,
    }
    types = {f.name: f.type for f in fs}
    assert types["content"] is str
    assert types["agent_id"] == (int | None)
    assert types["control_plane_session_id"] == (int | None)
    assert types["external_session_id"] == (str | None)
    assert types["metadata"] == (dict | None)


def test_core_agent_response_exact_contract():
    fs, required, optional = _field_contract(AgentResponse)
    # ACP V0.2：+artifacts 加性字段（默认 None ⇒ 旧用法不变）。
    assert {f.name for f in fs} == {
        "content", "external_session_id", "metadata", "artifacts"}
    assert required == ["content"]
    assert optional == {
        "external_session_id": None, "metadata": None, "artifacts": None}
    types = {f.name: f.type for f in fs}
    assert types["content"] is str
    assert types["external_session_id"] == (str | None)
    assert types["metadata"] == (dict | None)
    assert types["artifacts"] == (list[Artifact] | None)


def test_core_models_are_frozen():
    req = AgentRequest(content="m")
    resp = AgentResponse(content="c")
    for obj, attr in ((req, "content"), (req, "external_session_id"),
                      (resp, "content"), (resp, "external_session_id")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, attr, "mutated")


def test_core_chunk_union_exactness():
    assert get_args(Chunk) == (ReasoningChunk, ContentChunk, TurnEnd)
    assert [len(fields(c)) for c in (ReasoningChunk, ContentChunk)] == [1, 1]
    assert len(fields(TurnEnd)) == 0  # TurnEnd 无业务载荷
    assert TurnEnd() == TurnEnd()


def test_core_protocol_runtime_behavior():
    class _Ok:
        def handle_turn(self, request): return AgentResponse(content="ok")

        async def stream_turn(self, request):
            yield TurnEnd()

    class _NotAgent:
        pass

    assert isinstance(_Ok(), Agent)
    assert isinstance(_Ok(), StreamingAgent)
    assert not isinstance(_NotAgent(), Agent)
    assert not isinstance(_NotAgent(), StreamingAgent)


# ================================================================== 二、Minimal Binding Contract


def test_minimal_mapping_and_round_trip():
    req = decode_request({"message": "m", "session_id": "s1"})
    assert (req.content, req.external_session_id,
            req.agent_id, req.control_plane_session_id, req.metadata) == \
        ("m", "s1", None, None, None)
    out = encode_response(AgentResponse(content="c", external_session_id="s1"))
    assert out == {"content": "c", "session_id": "s1"}
    # None 态 round-trip
    assert encode_response(AgentResponse(content="c")) == \
        {"content": "c", "session_id": None}


@pytest.mark.parametrize("bad", [
    {}, {"session_id": "s"}, {"message": 1}, {"message": None},
    {"message": "m", "session_id": 5}, {"message": "m", "session_id": ""},
    [], "x", None,
])
def test_minimal_fail_closed(bad):
    with pytest.raises(MinimalBindingError):
        decode_request(bad)


def test_minimal_metadata_never_leaks_and_unknown_keys_do_not_pollute():
    out = encode_response(AgentResponse(content="c", metadata={"secret": True}))
    assert out == {"content": "c", "session_id": None}
    req = decode_request({"message": "m", "session_id": None, "future": "x"})
    assert req.metadata is None and req.agent_id is None


def test_minimal_stateful_external_session_id_round_trip():
    agent = _example_agent()
    out1 = minimal_handle({"message": "hi", "session_id": None}, agent)
    out2 = minimal_handle(
        {"message": "hi", "session_id": out1["session_id"]}, agent)
    assert out1["session_id"] == out2["session_id"] == "agent-session-1"
    assert (out1["content"], out2["content"]) == ("turn 1", "turn 2")


# ================================================================== 三、Tencent SSE Binding Contract


def _wire(kind: str, text: str) -> str:
    return "data: " + json.dumps({
        "sessionUpdate": kind,
        "content": {"type": "text", "text": text},
    }) + "\n\n"


@pytest.mark.parametrize("kind", ["agent_message_chunk", "agent_message_delta"])
def test_tencent_message_forms_become_content_chunk(kind):
    assert parse_sse_stream(_wire(kind, "hi")) == [ContentChunk("hi")]


@pytest.mark.parametrize("kind", ["agent_thought_chunk", "agent_thought", "thought_chunk"])
def test_tencent_thought_forms_become_reasoning_chunk(kind):
    assert parse_sse_stream(_wire(kind, "t")) == [ReasoningChunk("t")]


@pytest.mark.parametrize("event", [
    {"isFinalStop": True},
    {"codebuddy.ai.isFinalStop": True},
    {"type": "turn_finished"},
    {"type": "session/endTurn"},
    {"type": "endTurn"},
    {"stopReason": "end_turn"},
    {"nested": {"deep": {"isFinalStop": True}}},
])
def test_tencent_all_final_forms_normalize_to_exactly_one_turn_end(event):
    chunks = parse_sse_stream("data: " + json.dumps(event) + "\n\n")
    assert chunks == [TurnEnd()]
    assert sum(isinstance(c, TurnEnd) for c in chunks) == 1


def test_tencent_order_and_reasoning_content_separation():
    stream = (_wire("agent_thought_chunk", "t1") + _wire("agent_message_chunk", "c1")
              + _wire("agent_thought_chunk", "t2") + _wire("agent_message_chunk", "c2")
              + _wire("agent_message_delta", "c3")
              + 'data: {"type": "turn_finished"}\n\n')
    assert parse_sse_stream(stream) == [
        ReasoningChunk("t1"), ContentChunk("c1"),
        ReasoningChunk("t2"), ContentChunk("c2"), ContentChunk("c3"), TurnEnd(),
    ]
    # reasoning 绝不成为 content
    contents = [c.content for c in parse_sse_stream(stream)
                if isinstance(c, ContentChunk)]
    assert all("t1" != x and "t2" != x for x in contents)


@pytest.mark.parametrize("bad", [
    _wire("user_message_chunk", "x"),   # 未知 kind 携带 text
    'data: {"type": "text", "text": "bare"}',  # 无信封裸 text
    'data: {"foo": 1}',                 # 完全未知事件
    "data: {bad",                       # malformed
    "data: [1]",                        # JSON 非对象
    "",                                 # 空流
])
def test_tencent_fail_closed(bad):
    with pytest.raises(TencentBindingError):
        parse_sse_stream(bad)


def test_tencent_jsonrpc_error_normalization():
    frame = "data: " + json.dumps(
        {"jsonrpc": "2.0", "id": 1,
         "error": {"code": -32000, "message": "boom", "data": {"x": 1}}}) + "\n\n"
    with pytest.raises(TencentRpcError) as exc_info:
        parse_sse_stream(frame)
    assert (exc_info.value.code, exc_info.value.message) == (-32000, "boom")
    assert exc_info.value.data == {"x": 1}


def test_tencent_session_id_external_session_id_round_trip():
    req = decode_prompt(build_session_prompt("m", "rt-1"))
    assert req.external_session_id == "rt-1"
    assert decode_prompt(build_session_prompt("m", None)).external_session_id is None


# ================================================================== 四、Stateful + Streaming Contract


def _load_example_agent_class():
    spec = importlib.util.spec_from_file_location(
        "contract_fence_example_agent", EXAMPLE_AGENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.StatefulStreamingAgent


_example_agent = _load_example_agent_class()


async def _collect(aiter):
    return [c async for c in aiter]


def test_stateful_three_turn_lifecycle_and_cross_binding_fence():
    agent = _example_agent()
    # Turn 1：None → agent-session-1（minimal 面）
    r1 = agent.handle_turn(AgentRequest(content="hi"))
    assert (r1.content, r1.external_session_id) == ("turn 1", "agent-session-1")
    # Turn 2：同 id → 同一 Agent-owned state（tencent 流式面）
    frames = asyncio.run(_collect(handle_stream(
        build_session_prompt("hi", r1.external_session_id), agent)))
    chunks = parse_sse_stream("".join(frames))
    assert chunks == [ReasoningChunk("thinking"), ContentChunk("hello "),
                      ContentChunk("world"), TurnEnd()]
    assert sum(isinstance(c, TurnEnd) for c in chunks) == 1
    # Turn 3：同 id → turn 3（minimal 面）
    r3 = agent.handle_turn(
        AgentRequest(content="hi", external_session_id="agent-session-1"))
    assert (r3.content, r3.external_session_id) == ("turn 3", "agent-session-1")
    # Agent 拥有 state；SDK 拥有零 state（两实例互不可见）
    other = _example_agent()
    assert other.handle_turn(AgentRequest(content="hi")).content == "turn 1"


# ================================================================== 五、Architectural Guard

#: SDK 依赖闭包禁止的 import 根（15.5.5.5 冻结清单）。
SDK_FORBIDDEN_ROOTS = {
    "app", "sqlalchemy", "fastapi", "flask", "httpx", "anyio",
    "uvicorn", "redis", "requests", "aiohttp", "pytest",
}

#: SDK 包内禁止出现的符号（session 存储/缓存面；只针对 SDK，不针对 examples）。
SDK_FORBIDDEN_SYMBOLS = {
    "SessionStore", "session_store", "_sessions", "SESSIONS", "_session_cache",
}


def _top_level_import_roots(path: Path) -> list[str]:
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


def test_architecture_sdk_dependency_closure():
    for py in sorted(SDK_DIR.rglob("*.py")):
        for root in _top_level_import_roots(py):
            assert root not in SDK_FORBIDDEN_ROOTS, f"{py.name}: forbidden '{root}'"
            assert root in sys.stdlib_module_names or root == "agent_runtime_sdk", (
                f"{py.name}: non-stdlib '{root}'")


def test_architecture_sdk_contains_no_session_storage_symbols():
    import agent_runtime_sdk.minimal as minimal_mod
    import agent_runtime_sdk.models as models_mod
    import agent_runtime_sdk.protocols as protocols_mod
    import agent_runtime_sdk.tencent_acp_sse as tencent_mod
    for mod in (agent_runtime_sdk, minimal_mod, models_mod,
                protocols_mod, tencent_mod):
        leaked = SDK_FORBIDDEN_SYMBOLS & set(dir(mod))
        assert not leaked, f"{mod.__name__} leaks session-storage symbols: {leaked}"


# ================================================================== 六、Public Surface Guard

#: 15.5.5 冻结公共面（新增/删除任何名字都必须显式改本 fence）。
EXPECTED_PUBLIC_SURFACE = {
    # Core（15.5.5.1）
    # ACP V0.2：+Artifact（加性 mirror；surface 显式扩面）。
    "Agent", "AgentRequest", "AgentResponse", "Artifact", "Chunk",
    "ContentChunk", "ReasoningChunk", "StreamingAgent", "TurnEnd",
    # Minimal Binding（15.5.5.2）
    "MinimalBindingError", "decode_request", "encode_response", "handle",
    # Tencent SSE Binding（15.5.5.3）
    "TencentBindingError", "TencentRpcError", "decode_prompt",
    "build_session_prompt", "parse_sse_frame", "parse_sse_stream",
    "encode_chunk", "encode_sse_frame", "handle_stream",
    "new_connection_id", "build_request_headers",
}


def test_public_surface_is_exactly_frozen():
    assert set(agent_runtime_sdk.__all__) == EXPECTED_PUBLIC_SURFACE


# ================================================================== 七、CP Isolation

_BLOCK_APP_CONTRACT_SMOKE = (
    "import importlib.abc, asyncio, sys\n"
    "class _BlockApp(importlib.abc.MetaPathFinder):\n"
    "    def find_spec(self, fullname, path=None, target=None):\n"
    "        if fullname == 'app' or fullname.startswith('app.'):\n"
    "            raise ImportError('ACP backend (app.*) import is blocked')\n"
    "        return None\n"
    "sys.meta_path.insert(0, _BlockApp())\n"
    "import agent_runtime_sdk as sdk\n"
    "async def _collect(aiter):\n"
    "    return [c async for c in aiter]\n"
    "class _Streaming:\n"
    "    async def stream_turn(self, request):\n"
    "        yield sdk.ReasoningChunk('t')\n"
    "        yield sdk.ContentChunk(request.content)\n"
    "        yield sdk.TurnEnd()\n"
    "class _Sync:\n"
    "    def handle_turn(self, r):\n"
    "        return sdk.AgentResponse(content='c', external_session_id=r.external_session_id)\n"
    "req = sdk.decode_prompt(sdk.build_session_prompt('hi', 's1'))\n"
    "assert req.content == 'hi' and req.external_session_id == 's1'\n"
    "frames = ''.join(asyncio.run(_collect(sdk.handle_stream(\n"
    "    sdk.build_session_prompt('hi', 's1'), _Streaming()))))\n"
    "assert sdk.parse_sse_stream(frames) == [\n"
    "    sdk.ReasoningChunk('t'), sdk.ContentChunk('hi'), sdk.TurnEnd()]\n"
    "out = sdk.handle({'message': 'm', 'session_id': 's1'}, _Sync())\n"
    "assert out == {'content': 'c', 'session_id': 's1'}\n"
    "print('CONTRACT_FENCE_OK')\n"
)


def test_cp_isolation_contract_smoke_without_backend():
    env = {**os.environ, "PYTHONPATH": str(BACKEND_ROOT)}
    result = subprocess.run(
        [sys.executable, "-c", _BLOCK_APP_CONTRACT_SMOKE],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "CONTRACT_FENCE_OK" in result.stdout
