"""Stateful + Streaming Example Agent（Step 15.5.5.4）。

**Example / Integration Proof**：证明已冻结的 Agent Runtime SDK
（15.5.5.1~15.5.5.3 PASS / FROZEN）无需任何扩展即可支持：

- Agent-owned state / external_session_id（SDK 零 session storage）
- 同步 `handle_turn()`（Minimal Binding 面）
- 异步 `stream_turn()`（Tencent ACP SSE Binding 面）
- ReasoningChunk / ContentChunk / TurnEnd 显式分块

依赖闭包：stdlib + `agent_runtime_sdk`（无 backend.app / 无第三方依赖）。

State lifecycle（Agent 自己拥有 state，SDK 只做 pass-through）：

    Turn 1: request.external_session_id = None
            → Agent 创建 `agent-session-1`，response.external_session_id = agent-session-1
    Turn 2: request.external_session_id = agent-session-1
            → Agent 恢复自己的 state，turn = 2
    Turn 3: 同上，turn = 3

`external_session_id` 是 **Agent-owned reference**：引用未知 session 属于
Agent 业务错误（fail closed），与 SDK 无关。

每个 stream turn 严格产出：ReasoningChunk → ContentChunk → ContentChunk →
TurnEnd（不把 streaming 模拟进 AgentResponse）。
"""

from collections.abc import AsyncIterator

from agent_runtime_sdk import (
    AgentRequest,
    AgentResponse,
    Chunk,
    ContentChunk,
    ReasoningChunk,
    TurnEnd,
)

__all__ = ["StatefulStreamingAgent"]


class StatefulStreamingAgent:
    """最小 Stateful + Streaming Agent（session state 完全由本 Agent 拥有）。

    SDK 不保存任何状态：本类的 `self._sessions` 是 **Agent 自己的**业务
    state（示例用进程内 dict；真实实现可换任意存储）。同一个 Agent 抽象
    同时服务 Minimal Binding（`handle_turn`）与 Tencent SSE Binding
    （`stream_turn`），两个 Binding 共享同一份 session state。
    """

    def __init__(self) -> None:
        # Agent-owned state：agent-session-id → {"session_id", "turn"}。
        # 这是本 Agent 的私有业务 state，不是 SDK 的。
        self._sessions: dict[str, dict] = {}

    # ------------------------------------------------------------ 同步面（Minimal Binding）
    def handle_turn(self, request: AgentRequest) -> AgentResponse:
        """一轮同步调用：恢复/创建自己的 state → 递增 turn → 返回归一化响应。"""
        state = self._restore(request)
        state["turn"] += 1
        return AgentResponse(
            content=f"turn {state['turn']}",
            external_session_id=state["session_id"],
        )

    # ------------------------------------------------------------ 流式面（Tencent SSE Binding）
    async def stream_turn(self, request: AgentRequest) -> AsyncIterator[Chunk]:
        """一轮流式调用：同一份 state；严格 4 chunk —— 1 reasoning + 2 content + 1 收尾。"""
        state = self._restore(request)
        state["turn"] += 1
        yield ReasoningChunk("thinking")
        yield ContentChunk("hello ")
        yield ContentChunk("world")
        yield TurnEnd()

    # ------------------------------------------------------------ 只读观测口（测试/演示用）
    def turns_of(self, session_id: str) -> int:
        """返回某个 agent-owned session 的已执行 turn 数（只读，不动状态）。"""
        return self._sessions[session_id]["turn"]

    # ------------------------------------------------------------ 内部：Agent 自己的 state lifecycle
    def _restore(self, request: AgentRequest) -> dict:
        """external_session_id pass-through 语义下的 state 恢复/创建。"""
        session_id = request.external_session_id
        if session_id is None:
            # 首轮：Agent 自己创建 external_session_id 并登记自己的 state。
            session_id = f"agent-session-{len(self._sessions) + 1}"
            self._sessions[session_id] = {"session_id": session_id, "turn": 0}
        elif session_id not in self._sessions:
            # Agent-owned reference 指向未知 session：Agent 业务层 fail closed。
            raise ValueError(f"unknown agent-owned session: {session_id}")
        return self._sessions[session_id]
