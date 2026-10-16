# Stateful + Streaming Example（Agent Runtime SDK）

Step 15.5.5.4 —— 用一个**独立 Example Agent** 证明已冻结的 Agent Runtime SDK
（15.5.5.1 Independent Models / 15.5.5.2 Minimal Binding /
15.5.5.3 Tencent SSE Binding，均 PASS / FROZEN）无需任何扩展即可支持：

1. Agent-owned state / `external_session_id`（SDK **零** session storage）
2. 同步 `handle_turn()` —— Minimal Binding 面
3. 异步 `stream_turn()` —— Tencent ACP SSE Binding 面
4. `ReasoningChunk` / `ContentChunk` / `TurnEnd` 显式分块

## 文件

- `agent.py`：`StatefulStreamingAgent` —— 依赖闭包 `stdlib + agent_runtime_sdk`。

## State lifecycle（Agent 自己拥有 state）

| Turn | request.external_session_id | Agent 行为 | response.external_session_id |
|---|---|---|---|
| 1 | `None` | 创建 `agent-session-1` | `agent-session-1` |
| 2 | `agent-session-1` | 恢复自己的 state，turn = 2 | `agent-session-1` |
| 3 | `agent-session-1` | 恢复自己的 state，turn = 3 | `agent-session-1` |

`external_session_id` 是 **Agent-owned reference**：SDK 只做 input/output
pass-through；引用未知 session 是 Agent 业务错误（fail closed）。

## 每轮 stream 输出（严格顺序）

```
ReasoningChunk("thinking")
ContentChunk("hello ")
ContentChunk("world")
TurnEnd()
```

## 如何独立运行（无需启动 ACP backend）

```bash
# 仓库根目录
PYTHONPATH=backend:examples/agent-runtime-sdk/stateful-streaming python - <<'PY'
import asyncio
from agent_runtime_sdk import (
    AgentRequest, build_session_prompt, parse_sse_stream,
    decode_prompt, handle_stream,
)
from agent import StatefulStreamingAgent

agent = StatefulStreamingAgent()

# Minimal Binding 面：三轮 stateful handle_turn
r1 = agent.handle_turn(AgentRequest(content="hi"))
r2 = agent.handle_turn(AgentRequest(content="hi", external_session_id=r1.external_session_id))
r3 = agent.handle_turn(AgentRequest(content="hi", external_session_id=r2.external_session_id))
print(r1.content, r2.content, r3.content)   # turn 1 / turn 2 / turn 3
print(r3.external_session_id)               # agent-session-1

# Tencent SSE Binding 面：同一 Agent 抽象，流式 + 显式分块
payload = build_session_prompt("hi", r3.external_session_id)

async def run():
    return [frame async for frame in handle_stream(payload, agent)]

chunks = parse_sse_stream("".join(asyncio.run(run())))
print([type(c).__name__ for c in chunks])   # ReasoningChunk / ContentChunk / ContentChunk / TurnEnd
PY
```

（Windows PowerShell 下把 `PYTHONPATH=...` 换成 `$env:PYTHONPATH="backend;examples/agent-runtime-sdk/stateful-streaming"`。）

## 机器证据

契约测试：`backend/tests/test_step15_5_5_4_stateful_streaming_example.py`
（D1~D15，含 dependency closure 审计与屏蔽 `app.*` 的子进程导入证明）。
