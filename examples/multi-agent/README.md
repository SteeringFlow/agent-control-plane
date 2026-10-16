# Example: 多 Agent 隔离（multi-agent）

两条 Local Channel 各接一个**独立的 standalone Agent**，验证 ACP 的核心隔离语义：

```
Local Channel A ── Connection(path-a) ──→ Agent A (port 9101) ──→ Response A
Local Channel B ── Connection(path-b) ──→ Agent B (port 9102) ──→ Response B

A ≠ B；Agent A 永远收不到 B 的消息，反之亦然（Session 隔离键
(identity_id, connection_id, external_conversation_id) 保证会话也互不串）。
```

前置：按仓库根 README 启动 ACP（`cd backend && uvicorn app.main:app`）。

## 快速运行（推荐）

```bash
cd examples/multi-agent
./run.sh                 # Linux / macOS
# 或
powershell -File run.ps1 # Windows
```

runner 会自动：启动两个 Agent → 建 User/Identity/Channel×2/Agent×2/Connection×2
→ 向 A 发 `msg-from-A`、向 B 发 `msg-from-B` → 轮询 Observation → 断言：

- Channel A 的响应来自 AgentA 且内容含 `msg-from-A`；
- Channel B 的响应来自 AgentB 且内容含 `msg-from-B`；
- `agent-a.log` **不含** `msg-from-B`，`agent-b.log` **不含** `msg-from-A`
  （零串路的直接证据）。

输出 `MULTI-AGENT ISOLATION: PASS` 即验证完成。脚本可重复运行（资源名带时间后缀）。

## 手动运行

Agent 服务（一个脚本，两个实例）：

```bash
cd examples/multi-agent
python agent_server.py --name AgentA --port 9101 --log agent-a.log &
python agent_server.py --name AgentB --port 9102 --log agent-b.log &
```

访问路径（`BASE=http://127.0.0.1:8000`）：

```bash
curl -s -X POST $BASE/api/users -H 'Content-Type: application/json' -d '{"name":"multi-agent"}'
curl -s -X POST $BASE/api/agents -H 'Content-Type: application/json' -d '{"name":"custom-agent-a","type":"acp"}'
curl -s -X POST $BASE/api/agents -H 'Content-Type: application/json' -d '{"name":"custom-agent-b","type":"acp"}'
curl -s -X POST $BASE/api/channels -H 'Content-Type: application/json' -d '{"name":"local-a","type":"local"}'
curl -s -X POST $BASE/api/channels -H 'Content-Type: application/json' -d '{"name":"local-b","type":"local"}'
curl -s -X POST $BASE/api/identities -H 'Content-Type: application/json' -d '{"user_id":1,"channel_id":1,"external_id":"local-user-1"}'
curl -s -X POST $BASE/api/identities -H 'Content-Type: application/json' -d '{"user_id":1,"channel_id":2,"external_id":"local-user-1"}'
curl -s -X POST $BASE/api/connections -H 'Content-Type: application/json' \
  -d '{"channel_id":1,"agent_id":1,"name":"path-a","config":{"protocol":"minimal","endpoint":"http://127.0.0.1:9101"}}'
curl -s -X POST $BASE/api/connections -H 'Content-Type: application/json' \
  -d '{"channel_id":2,"agent_id":2,"name":"path-b","config":{"protocol":"minimal","endpoint":"http://127.0.0.1:9102"}}'

curl -s -X POST $BASE/api/channels/1/local/message -H 'Content-Type: application/json' \
  -d '{"external_id":"local-user-1","text":"msg-from-A"}'
curl -s -X POST $BASE/api/channels/2/local/message -H 'Content-Type: application/json' \
  -d '{"external_id":"local-user-1","text":"msg-from-B"}'

curl -s $BASE/api/interactions/1   # response_payload.content == "AgentA echo: msg-from-A (turn 1)"
curl -s $BASE/api/interactions/2   # response_payload.content == "AgentB echo: msg-from-B (turn 1)"
```

## 模型要点（为什么这样建）

- **多 Agent = 多 Channel + 多 Connection**：MVP Routing 语义是「一个 Channel
  对应唯一 active Connection，Agent 由 Connection 派生」。同一个 Channel 挂两个
  active Connection 会被 Fail Closed（`ROUTING_FAILED`），这是冻结的
  Capability Gap，不是 bug。
- 换 Agent = 旧 Connection 置 inactive + 新建（绑定不可变，无 PATCH 改绑）。
- 同一 `external_id` 可在不同 Channel 各建一条 Identity（身份是 channel-scoped）。
