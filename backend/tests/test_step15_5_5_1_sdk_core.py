"""Step 15.5.5.1 —— Agent Runtime SDK Core（independent models + protocols）契约测试。

验证面（15.5.5.1 范围，不涉及 binding / hosting）：
1. 字段面：AgentRequest / AgentResponse / ReasoningChunk / ContentChunk /
   TurnEnd 与冻结字段一致，且全部 frozen；
2. Chunk 集合：`Chunk` 恰为三种 Chunk 的 union，TurnEnd 无业务载荷；
3. Protocol 一致性：`Agent.handle_turn` / `StreamingAgent.stream_turn`
   结构化可检验、可调用；
4. pass-through 语义：`external_session_id` 只透传，SDK 不保存 Session 状态；
5. import dependency closure：SDK 只允许标准库 + 本包相对导入，
   绝不 import `app.*` / sqlalchemy / fastapi / redis / httpx / anyio；
6. 无 ACP backend 也可成功 import（子进程内屏蔽 `app.*` 后导入）。
"""

import ast
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import get_args

import pytest

import agent_runtime_sdk
from agent_runtime_sdk import (
    Agent,
    AgentRequest,
    AgentResponse,
    Chunk,
    ContentChunk,
    ReasoningChunk,
    StreamingAgent,
    TurnEnd,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# ================================================================== 1. 字段面


def test_agent_request_field_face():
    """AgentRequest：content 必填，其余四字段冻结为 None 默认。"""
    req = AgentRequest(content="hello")
    assert req.content == "hello"
    assert req.agent_id is None
    assert req.control_plane_session_id is None
    assert req.external_session_id is None
    assert req.metadata is None

    full = AgentRequest(
        content="m",
        agent_id=7,
        control_plane_session_id=42,
        external_session_id="agent-session-abc",
        metadata={"protocol": "minimal"},
    )
    assert full.agent_id == 7
    assert full.control_plane_session_id == 42
    assert full.external_session_id == "agent-session-abc"
    assert full.metadata == {"protocol": "minimal"}

    with pytest.raises(FrozenInstanceError):
        req.content = "mutated"


def test_agent_request_content_is_required():
    with pytest.raises(TypeError):
        AgentRequest()  # type: ignore[call-arg]


def test_agent_response_field_face():
    """AgentResponse：content 必填，external_session_id / metadata 可选。"""
    resp = AgentResponse(content="ok")
    assert resp.content == "ok"
    assert resp.external_session_id is None
    assert resp.metadata is None

    full = AgentResponse(
        content="ok",
        external_session_id="agent-session-abc",
        metadata={"protocol": "tencent-acp-sse"},
    )
    assert full.external_session_id == "agent-session-abc"

    with pytest.raises(FrozenInstanceError):
        resp.external_session_id = "mutated"


def test_chunk_models_field_face():
    """ReasoningChunk / ContentChunk 只有 content；TurnEnd 无业务载荷且 frozen。"""
    r = ReasoningChunk(content="thinking...")
    c = ContentChunk(content="answer")
    assert r.content == "thinking..."
    assert c.content == "answer"

    end = TurnEnd()
    assert end == TurnEnd()  # 无载荷 → 全部相等
    with pytest.raises(FrozenInstanceError):
        end.content = "no-payload"  # type: ignore[attr-defined]
    with pytest.raises(FrozenInstanceError):
        r.content = "mutated"


# ================================================================== 2. Chunk 集合


def test_chunk_union_is_exactly_three_types():
    """Chunk 恰为 ReasoningChunk | ContentChunk | TurnEnd（冻结，不可扩展）。"""
    assert get_args(Chunk) == (ReasoningChunk, ContentChunk, TurnEnd)


# ================================================================== 3. Protocol 一致性


class _EchoAgent:
    """最小同步 Agent（仅测试用一致性样本，不是 Stateful example）。"""

    def handle_turn(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            content=f"echo:{request.content}",
            external_session_id=request.external_session_id,
        )


class _StreamingEchoAgent:
    """最小流式 Agent（reasoning / content 显式分块 + TurnEnd 收尾）。"""

    async def stream_turn(self, request: AgentRequest):
        yield ReasoningChunk(content="thinking")
        yield ContentChunk(content=request.content)
        yield TurnEnd()


def test_agent_protocol_conformance_and_call():
    agent = _EchoAgent()
    assert isinstance(agent, Agent)  # runtime_checkable 结构化检验
    resp = agent.handle_turn(
        AgentRequest(content="hi", external_session_id="s-1")
    )
    assert isinstance(resp, AgentResponse)
    assert resp.content == "echo:hi"
    assert resp.external_session_id == "s-1"


def test_streaming_agent_protocol_conformance_and_stream():
    agent = _StreamingEchoAgent()
    assert isinstance(agent, StreamingAgent)

    async def _collect() -> list:
        req = AgentRequest(content="hi", external_session_id="s-1")
        return [chunk async for chunk in agent.stream_turn(req)]

    import asyncio

    chunks = asyncio.run(_collect())
    assert chunks == [
        ReasoningChunk(content="thinking"),
        ContentChunk(content="hi"),
        TurnEnd(),
    ]


# ================================================================== 4. pass-through 语义


def test_external_session_id_is_passthrough_only():
    """SDK 不保存 Session：同一 agent 对不同 external_session_id 的输入互不影响。"""
    agent = _EchoAgent()
    r1 = agent.handle_turn(AgentRequest(content="a", external_session_id="s-1"))
    r2 = agent.handle_turn(AgentRequest(content="b"))  # 首轮：external 为 None
    r3 = agent.handle_turn(AgentRequest(content="c", external_session_id="s-1"))

    assert r1.external_session_id == "s-1"
    assert r2.external_session_id is None
    assert r3.external_session_id == "s-1"
    # frozen 模型即不可变状态：SDK 侧不存在可被 mutation 的 session 容器
    with pytest.raises(FrozenInstanceError):
        r1.content = "mutated"


# ================================================================== 5. import dependency closure

#: SDK 依赖闭包中禁止出现的顶层模块（15.5.3 Hard Rules 机器化锁定）。
_FORBIDDEN_ROOTS = {
    "app",
    "sqlalchemy",
    "fastapi",
    "redis",
    "httpx",
    "anyio",
    "uvicorn",
    "pytest",
}


def _iter_top_level_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue  # 包内相对导入，允许
            roots.append((node.module or "").split(".")[0])
    return roots


def test_sdk_dependency_closure_is_stdlib_only():
    """SDK 全部模块只允许标准库 + 本包相对导入；禁止 app.* 与任何第三方。"""
    sdk_dir = Path(agent_runtime_sdk.__file__).resolve().parent
    py_files = sorted(sdk_dir.rglob("*.py"))
    assert py_files, "SDK package should contain modules"

    for py in py_files:
        for root in _iter_top_level_imports(py):
            assert root not in _FORBIDDEN_ROOTS, f"{py.name}: forbidden import '{root}'"
            assert root in sys.stdlib_module_names or root == "agent_runtime_sdk", (
                f"{py.name}: non-stdlib import '{root}'"
            )


# ================================================================== 6. 无 ACP backend 可导入

#: 在屏蔽 `app.*` 的子进程中 import SDK —— 证明依赖闭包不含 ACP backend runtime。
_BLOCK_APP_AND_IMPORT = (
    "import importlib.abc\n"
    "import sys\n"
    "class _BlockApp(importlib.abc.MetaPathFinder):\n"
    "    def find_spec(self, fullname, path=None, target=None):\n"
    "        if fullname == 'app' or fullname.startswith('app.'):\n"
    "            raise ImportError('ACP backend (app.*) import is blocked')\n"
    "        return None\n"
    "sys.meta_path.insert(0, _BlockApp())\n"
    "import agent_runtime_sdk\n"
    "print('SDK_IMPORT_OK')\n"
)


def test_sdk_imports_without_acp_backend():
    result = subprocess.run(
        [sys.executable, "-c", _BLOCK_APP_AND_IMPORT],
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "SDK_IMPORT_OK" in result.stdout
