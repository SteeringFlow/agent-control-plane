"""Agent Runtime SDK —— Agent Protocols（Step 15.5.5.1）。

两个最小 Protocol（只描述形状，无任何实现 / Hosting / Binding）：

- `Agent.handle_turn(request) -> AgentResponse`：同步单轮原语
  （minimal binding 的目标形态，15.5.5.2）；
- `StreamingAgent.stream_turn(request) -> AsyncIterator[Chunk]`：流式单轮
  原语（tencent-acp-sse binding 的目标形态，15.5.5.3）。`stream_turn` 是
  richer 原语，可退化表达 `handle_turn`（单 `ContentChunk` + `TurnEnd`）。

`Chunk` 只允许三种：`ReasoningChunk` / `ContentChunk` / `TurnEnd`。
本模块不实现 HTTP server / SSE parser / JSON-RPC —— 那些属于
15.5.5.2～15.5.5.4。
"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .models import AgentRequest, AgentResponse, ContentChunk, ReasoningChunk, TurnEnd

#: 流式单轮的唯一 Chunk 集（15.5.5.1 冻结：仅三种；TurnEnd 无业务载荷）。
Chunk = ReasoningChunk | ContentChunk | TurnEnd


@runtime_checkable
class Agent(Protocol):
    """同步单轮 Agent 协议（Agent 作者实现 `handle_turn`）。"""

    def handle_turn(self, request: AgentRequest) -> AgentResponse:
        """一轮请求 → 一轮响应（session state 由 Agent 作者自管）。"""
        ...


@runtime_checkable
class StreamingAgent(Protocol):
    """流式单轮 Agent 协议（Agent 作者实现 `stream_turn`）。"""

    def stream_turn(self, request: AgentRequest) -> AsyncIterator[Chunk]:
        """一轮请求 → Chunk 流（reasoning / content 显式分块 + TurnEnd 收尾）。"""
        ...
