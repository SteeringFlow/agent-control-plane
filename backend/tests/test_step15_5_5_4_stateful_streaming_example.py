"""Step 15.5.5.4 —— Stateful + Streaming Example（Integration Proof）契约测试。

Example 位置：`examples/agent-runtime-sdk/stateful-streaming/agent.py`。
本步是 Example / Integration Proof：**不修改 SDK Core**，只证明冻结的
SDK（15.5.5.1~15.5.5.3）无需扩展即可支撑 stateful + streaming Agent。

覆盖面（D1~D15）：
- D1  Turn 1：None → agent-session-1
- D2  Turn 2：同 id → turn 2
- D3  Turn 3：同 id → turn 3
- D4  Agent 自管 state；SDK 不保存状态（实例间隔离 + 模型 frozen）
- D5  handle_turn 返回正确 AgentResponse
- D6  stream_turn 严格产出 ReasoningChunk / ContentChunk / ContentChunk / TurnEnd
- D7  chunk 顺序保持
- D8  每 turn 恰一个 TurnEnd
- D9  Reasoning 与 Content 不混淆
- D10 external_session_id round-trip（双 binding）
- D11 同一 Agent 抽象接 Minimal Binding
- D12 同一 Agent 抽象接 Tencent Binding（跨 binding 共享同一份 state）
- D13 Example 不 import backend.app
- D14 SDK 零 SessionStore / session cache
- D15 无第三方依赖（stdlib + agent_runtime_sdk）
"""

import ast
import asyncio
import importlib.util
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import agent_runtime_sdk
from agent_runtime_sdk import (
    AgentRequest,
    AgentResponse,
    ContentChunk,
    ReasoningChunk,
    TurnEnd,
    build_session_prompt,
    decode_prompt,
    handle as minimal_handle,
    handle_stream,
    parse_sse_stream,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
EXAMPLE_AGENT = (
    REPO_ROOT / "examples" / "agent-runtime-sdk" / "stateful-streaming" / "agent.py"
)


def _load_example_agent_class():
    assert EXAMPLE_AGENT.exists(), f"missing example: {EXAMPLE_AGENT}"
    spec = importlib.util.spec_from_file_location(
        "stateful_streaming_example_agent", EXAMPLE_AGENT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.StatefulStreamingAgent


StatefulStreamingAgent = _load_example_agent_class()


async def _collect(aiter):
    return [chunk async for chunk in aiter]


# ================================================================== D1~D3 stateful lifecycle


def test_d1_d2_d3_stateful_lifecycle():
    agent = StatefulStreamingAgent()

    # Turn 1：external_session_id = None → Agent 创建 agent-session-1
    r1 = agent.handle_turn(AgentRequest(content="hi"))
    assert r1.external_session_id == "agent-session-1"
    assert r1.content == "turn 1"

    # Turn 2：同 id 回流 → Agent 恢复自己的 state，turn = 2
    r2 = agent.handle_turn(
        AgentRequest(content="hi", external_session_id=r1.external_session_id)
    )
    assert r2.external_session_id == "agent-session-1"
    assert r2.content == "turn 2"

    # Turn 3：流式轮共享同一份 state，turn = 3
    chunks3 = asyncio.run(_collect(
        agent.stream_turn(
            AgentRequest(content="hi", external_session_id="agent-session-1")
        )
    ))
    assert chunks3 == [ReasoningChunk("thinking"), ContentChunk("hello "),
                       ContentChunk("world"), TurnEnd()]
    assert agent.turns_of("agent-session-1") == 3


# ================================================================== D4 Agent 拥有 state，SDK 不保存


def test_d4_agent_owns_state_sdk_stores_nothing():
    # 两个 Agent 实例的 state 完全独立（state 属于 Agent，不在 SDK）
    a1 = StatefulStreamingAgent()
    a2 = StatefulStreamingAgent()
    r1 = a1.handle_turn(AgentRequest(content="hi"))
    r1_b = a2.handle_turn(AgentRequest(content="hi"))
    assert r1.external_session_id == r1_b.external_session_id == "agent-session-1"
    assert a1.turns_of("agent-session-1") == 1
    assert a2.turns_of("agent-session-1") == 1

    # SDK 模型 frozen：不存在可被外部 mutation 的 session 容器
    with pytest.raises(FrozenInstanceError):
        r1.external_session_id = "mutated"
    # SDK 纯函数：同一 wire payload 两次 decode 产出独立实例
    p = {"message": "hi", "session_id": "agent-session-1"}
    from agent_runtime_sdk import decode_request
    assert decode_request(p) is not decode_request(p)


# ================================================================== D5 handle_turn 响应面


def test_d5_handle_turn_returns_correct_agent_response():
    agent = StatefulStreamingAgent()
    resp = agent.handle_turn(AgentRequest(content="hi"))
    assert isinstance(resp, AgentResponse)
    assert resp.content == "turn 1"
    assert resp.external_session_id == "agent-session-1"
    assert resp.metadata is None


# ================================================================== D6~D9 streaming 面


def test_d6_d7_d8_d9_stream_shape_order_and_single_turn_end():
    agent = StatefulStreamingAgent()
    # turn 1（stream 轮，新 session）
    chunks1 = asyncio.run(_collect(
        agent.stream_turn(AgentRequest(content="hi"))))
    assert chunks1 == [ReasoningChunk("thinking"), ContentChunk("hello "),
                       ContentChunk("world"), TurnEnd()]
    assert sum(isinstance(c, TurnEnd) for c in chunks1) == 1

    # turn 2（同 session 再流一轮）：形状一致，且每 turn 恰一个 TurnEnd
    chunks2 = asyncio.run(_collect(agent.stream_turn(
        AgentRequest(content="hi", external_session_id="agent-session-1"))))
    assert chunks2 == [ReasoningChunk("thinking"), ContentChunk("hello "),
                       ContentChunk("world"), TurnEnd()]
    assert sum(isinstance(c, TurnEnd) for c in chunks2) == 1

    # D9：reasoning 与 content 不混淆
    contents = [c.content for c in chunks1 if isinstance(c, ContentChunk)]
    reasonings = [c.content for c in chunks1 if isinstance(c, ReasoningChunk)]
    assert contents == ["hello ", "world"]
    assert reasonings == ["thinking"]


# ================================================================== D10 external_session_id round-trip


def test_d10_external_session_id_round_trip_both_bindings():
    agent = StatefulStreamingAgent()
    # Minimal 面：Agent 创建的 id 经 response 回流，再经 request 进入下一轮
    r1 = agent.handle_turn(AgentRequest(content="hi"))
    r2 = agent.handle_turn(
        AgentRequest(content="hi", external_session_id=r1.external_session_id))
    assert r2.external_session_id == r1.external_session_id == "agent-session-1"
    # Tencent 面：wire sessionId ↔ external_session_id
    req = decode_prompt(build_session_prompt("hi", r2.external_session_id))
    assert req.external_session_id == "agent-session-1"


# ================================================================== D11 Minimal Binding 接入


def test_d11_same_agent_works_with_minimal_binding():
    agent = StatefulStreamingAgent()
    out1 = minimal_handle({"message": "hi", "session_id": None}, agent)
    assert out1 == {"content": "turn 1", "session_id": "agent-session-1"}
    out2 = minimal_handle({"message": "hi", "session_id": "agent-session-1"}, agent)
    assert out2 == {"content": "turn 2", "session_id": "agent-session-1"}


# ================================================================== D12 Tencent Binding 接入（跨 binding 共享 state）


def test_d12_same_agent_works_with_tencent_binding():
    agent = StatefulStreamingAgent()
    # minimal 轮创建 session（turn 1）
    r1 = agent.handle_turn(AgentRequest(content="hi"))
    # tencent 流式轮恢复同一份 state（turn 2）
    payload = build_session_prompt("hi", r1.external_session_id)
    frames = asyncio.run(_collect(handle_stream(payload, agent)))
    chunks = parse_sse_stream("".join(frames))
    assert chunks == [ReasoningChunk("thinking"), ContentChunk("hello "),
                      ContentChunk("world"), TurnEnd()]
    assert agent.turns_of("agent-session-1") == 2
    # Agent-owned reference 指向未知 session → Agent 业务层 fail closed
    with pytest.raises(ValueError):
        asyncio.run(_collect(
            handle_stream(build_session_prompt("hi", "unknown-session"), agent)))


# ================================================================== D13/D15 Example dependency closure

_FORBIDDEN_ROOTS = {
    "app", "sqlalchemy", "fastapi", "flask", "redis",
    "httpx", "anyio", "uvicorn", "aiohttp", "requests", "pytest",
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


def test_d15_example_dependency_closure_stdlib_plus_sdk_only():
    for root in _top_level_import_roots(EXAMPLE_AGENT):
        assert root in sys.stdlib_module_names or root == "agent_runtime_sdk", (
            f"example imports non-stdlib/non-SDK module: {root}")


def test_d13_example_does_not_import_backend_app():
    assert "app" not in _top_level_import_roots(EXAMPLE_AGENT)


# ================================================================== D14 SDK 零 session storage


def test_d14_sdk_contains_no_sessionstore_or_session_cache():
    assert not hasattr(agent_runtime_sdk, "SessionStore")
    import agent_runtime_sdk.minimal as minimal_mod
    import agent_runtime_sdk.tencent_acp_sse as tencent_mod
    import agent_runtime_sdk.models as models_mod
    import agent_runtime_sdk.protocols as protocols_mod
    for mod in (agent_runtime_sdk, minimal_mod, tencent_mod, models_mod, protocols_mod):
        for symbol in ("SessionStore", "session_store", "_sessions",
                       "SESSIONS", "_session_cache"):
            assert not hasattr(mod, symbol), f"{mod.__name__} has {symbol}"


# ================================================================== 无 ACP backend 也可运行


def test_example_imports_and_runs_without_acp_backend():
    """子进程内屏蔽 app.* 后导入并跑通 Example —— 不要求 ACP backend 启动。"""
    script = (
        "import importlib.abc, importlib.util, sys\n"
        "class _BlockApp(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'app' or fullname.startswith('app.'):\n"
        "            raise ImportError('ACP backend (app.*) import is blocked')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _BlockApp())\n"
        "spec = importlib.util.spec_from_file_location(\n"
        f"    'example_agent', r'{EXAMPLE_AGENT.as_posix()}')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "agent = mod.StatefulStreamingAgent()\n"
        "from agent_runtime_sdk import AgentRequest\n"
        "r1 = agent.handle_turn(AgentRequest(content='hi'))\n"
        "r2 = agent.handle_turn(AgentRequest(content='hi', external_session_id=r1.external_session_id))\n"
        "assert r1.content == 'turn 1' and r2.content == 'turn 2'\n"
        "print('EXAMPLE_RUN_OK')\n"
    )
    env = {**os.environ, "PYTHONPATH": str(BACKEND_ROOT)}
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "EXAMPLE_RUN_OK" in result.stdout
