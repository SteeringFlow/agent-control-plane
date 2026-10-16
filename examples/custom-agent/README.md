# Example: 接入你自己的 Agent（custom-agent）

用 **Local Channel**（零外部依赖）把消息路由给一个 **standalone 的自定义 HTTP
Agent**，并在 Observation 里看到它的真实响应。

你将验证的完整链路：

```
POST /local/message
  → Local Channel 入站（PENDING → ACK）
  → Worker → Runtime（Identity → Routing → Session → Dispatch → Delivery）
  → ACPAdapter（minimal binding）→ 你的 agent.py（独立进程）
  → AgentResponse → Observation
```

前置：按仓库根 README 启动 ACP（`cd backend && uvicorn app.main:app`）。

## 1. 启动你的 Agent（独立进程，不依赖 ACP 代码）

```bash
cd examples/custom-agent
python agent.py --port 9101
# custom-agent listening on http://127.0.0.1:9101
```

`agent.py` 只用 Python 标准库，实现的是 `ACPAdapter` 的 **minimal binding**：

```
POST {endpoint}   body {"message": str, "session_id": str|null}
200               {"content": str(非空), "session_id": str|null}
```

任何语言/框架实现这个形状的 HTTP 服务，都能被 `type=acp` 的 Agent 调用。

## 2. 在 ACP 里建好访问路径

```bash
BASE=http://127.0.0.1:8000

curl -s -X POST $BASE/api/users -H 'Content-Type: application/json' \
  -d '{"name": "custom-agent-demo"}'

curl -s -X POST $BASE/api/channels -H 'Content-Type: application/json' \
  -d '{"name": "local-1", "type": "local"}'

curl -s -X POST $BASE/api/identities -H 'Content-Type: application/json' \
  -d '{"user_id": 1, "channel_id": 1, "external_id": "local-user-1"}'

# 关键一步：Agent 用 type=acp；endpoint 指向你的 agent.py（完整 URL）
curl -s -X POST $BASE/api/agents -H 'Content-Type: application/json' \
  -d '{"name": "my-agent", "type": "acp"}'

curl -s -X POST $BASE/api/connections -H 'Content-Type: application/json' \
  -d '{"channel_id": 1, "agent_id": 1, "name": "my-path",
       "config": {"protocol": "minimal", "endpoint": "http://127.0.0.1:9101"}}'
```

## 3. 发消息 → 看 Agent 收到 → 看响应回流

```bash
curl -s -X POST $BASE/api/channels/1/local/message \
  -H 'Content-Type: application/json' \
  -d '{"external_id": "local-user-1", "text": "hello"}'
# -> {"interaction_id": 1, "status": "accepted"}   （200 只是 ACK，执行在后台）

# agent.py 终端此时会打印：  <<< message='hello' session=... turn=1

curl -s $BASE/api/interactions/1
# -> processing_status=succeeded、delivery_status=delivered、
#    response_payload.content == "[custom-agent] turn 1, you said: hello"
```

再发一条（同一渠道 = 同一 CP Session，REUSE_ACTIVE 复用）：

```bash
curl -s -X POST $BASE/api/channels/1/local/message \
  -H 'Content-Type: application/json' \
  -d '{"external_id": "local-user-1", "text": "hello again"}'
# -> response_payload.content == "[custom-agent] turn 1, you said: hello again"
```

## 已知边界：Agent 侧会话引用当前不在 dispatch 回传（诚实记录）

上面第二条消息的 `turn` 仍是 1 —— 这是**当前架构的真实行为**，不是 bug：

- 你的 Agent 返回的 `session_id` **会被 Control Plane 记录**（持久化到
  `sessions.external_session_id`，10.8.4）；
- 但生产执行链当前**不把它传回**给下一轮 dispatch（`coordinator.py` 的
  dispatch 调用只传三参）——这是 Step 10.3.1 明确冻结记录的已知缺口
  「external_session_id 不在 Runtime 主链路回流」，留待后续 Runtime/持久化
  演进裁决；
- **不受影响的部分**：CP 侧会话粘性（同一渠道/身份/会话 → REUSE_ACTIVE 复用
  同一条 Session）、隔离语义、响应回流，全部正常。对无状态或自带会话管理的
  Agent（如示例中的 echo），这个缺口无实际影响；有状态 Agent 需要等待该缺口
  关闭（届时无需改你的 Agent，只改 Runtime 内部接线）。

## 替换成你的真实 Agent

把 `endpoint` 换成你的服务地址即可；`timeout_s` 可选（默认 30）。若你的
Agent 需要鉴权引用，在 Connection 上配 `credential_ref`（写-only，会以
`X-Acp-Credential-Ref` 头透传给你的服务）。

> 面向腾讯 CodeBuddy Enterprise ACP Runtime 的 SSE wire（`tencent-acp-sse`）
> 见 `backend/app/adapters/agents/acp.py` docstring §2；本示例使用默认的
> minimal binding，两者互不影响。
