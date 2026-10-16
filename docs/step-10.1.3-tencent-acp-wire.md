# Step 10.1.3 — Tencent ACP Runtime Compatibility Validation（wire calibration）

> Step 10.1 的第三刀：把 10.1.2 的「最小 ACP HTTP binding 占位」**按腾讯 CodeBuddy
> Enterprise ACP Runtime 真实协议校准 wire**。只做 `AgentRequest → ACPAdapter →
> Tencent ACP Runtime → AgentResponse`，**不接 RuntimeCoordinator**（第二阶段再做完整
> Runtime E2E）。

## 为什么必须有这一步

10.1.2 在仓库无 ACP wire spec 的前提下用「最小 ACP HTTP binding」做架构反证，文档明确
预告：真实外部 ACP-compatible Agent 接入时只需在 `acp.py` 内部校准 wire。本步以腾讯
Enterprise Runtime **实际要求**为准（Runtime 创建即自带 session，**不擅自加 initialize**），
用真实 acpLink 校准。

## 实验变量（严格控制）

| 层 | 本步取值 | 说明 |
| --- | --- | --- |
| Channel | 不参与 | 只验证 Agent Adapter |
| Identity / Routing / Session / Dispatch / Delivery | 不参与 | Runtime Kernel 零改动 |
| Adapter | **真实 ACPAdapter**（`protocol="tencent-acp-sse"` binding） | `acp.py` 内新增 binding，minimal 保持默认 |
| Agent | **本地 SSE 模拟 Tencent ACP Runtime**（真实 TCP/HTTP + 持久 SSE + jsonrpc） 或 env 提供 acpLink 时打真实 Runtime | 变量隔离：协议语义在本地可重复验证 |

## Tencent ACP Runtime 事实（来自腾讯管理后台 API 集成说明）

1. Runtime 创建（管理面，方式 A，本步不自动执行）：
   `POST https://www.codebuddy.cn/v2/agentos/runtimes`
   - headers：`Authorization: Bearer <AgentOS token>` + `X-Source-App`
   - body：`runtimeName` + `agentManifest{id,name,manifestVersion,secrets}`
   - 响应：`data.id`（runtime.id）、`data.links.acpLink.url`、`data.links.acpLink.token`
2. **Session**：Runtime 默认 Session ID 与 Runtime ID 相同，可直接用 `{runtime.id}`。
3. ACP 通信（acpLink 提供 url/token）：
   - GET `{url}`：`Authorization: Bearer <acpLink.token>` + `Acp-Connection-Id` →
     建立**持久 SSE**；
   - POST `{url}`：同头 + `Content-Type: application/json` +
     `Accept: application/json, text/event-stream`，body 为 jsonrpc 2.0
     `session/prompt`（`sessionId` + `prompt:[{type:text,text}]`）；
   - 最终文本经 GET 的 SSE `session/update` → `agent_message_chunk` →
     `content.text` 逐块到达，以 `codebuddy.ai.isFinalStop`（或实际终态事件）收尾。

本步用**方式 B**：预创建 Runtime 后，把 `acpLink.url / acpLink.token / runtime.id`
三值放入 env（`TENCENT_ACP_URL / TENCENT_ACP_TOKEN / TENCENT_RUNTIME_ID`）驱动真实调用。

## 实现：`acp.py` 两种 binding（Port 不变）

`Connection.config["protocol"]` 选择 binding；默认 minimal（10.1.2 语义，回归全绿）。

| protocol | binding | 说明 |
| --- | --- | --- |
| 缺省 / `"minimal"` | 10.1.2 最小 ACP HTTP binding | 本地真实 HTTP 反证用，不面向生产 |
| `"tencent-acp-sse"` | **Tencent ACP SSE**（本步新增） | GET 持久 SSE + POST jsonrpc session/prompt + SSE 事件收尾 |

Tencent binding 细节：

1. 每次 `send` 生成独立 `Acp-Connection-Id`（uuid4 hex），**不缓存连接**；
2. 后台线程 GET `{url}`（Bearer + Acp-Connection-Id + `Accept: text/event-stream`），
   200 后进入逐行读 SSE；
3. 主流程 POST jsonrpc `session/prompt`（`sessionId` = `external_session_id` 或
   `runtime_id`；`prompt[0].text` = `AgentRequest.content`）；
4. 从 SSE 队列读事件：递归提取所有 `type=="text"` content 块、识别
   `codebuddy.ai.isFinalStop` / `isFinalStop` / `turn_finished`；
5. 收尾语义：isFinalStop → 成功返回；SSE EOF（自然收尾）→ 成功返回已收文本；
   超时未收尾 / reader transport 错误 → `AgentDispatchFailedError`（Fail Closed，
   绝不返回半成品）。

凭据/配置只来自 `Connection.config`：`endpoint`（acpLink.url）/ `bearer_token`
（acpLink.token）/ `runtime_id`。**不做**：initialize / session 缓存 / Token 刷新 /
重试 / RuntimeManager / 连接池（10.1.3 范围外，第二阶段也不膨胀）。

## 验收 T1–T8（本地 SSE 模拟 + env 门控真实）

| # | 验收 | 本地模拟断言 |
| --- | --- | --- |
| T1 | ACP Authentication — acpLink.token → `Authorization: Bearer` | 正确 token 成功；错误 token 401 → `AgentDispatchFailedError` |
| T2 | ACP Connection — 生成 `Acp-Connection-Id`，GET SSE 建立 | server 收到非空 conn_id，GET+POST 同一 conn_id |
| T3 | Prompt — content → jsonrpc `session/prompt` | server 记录 method/sessionId/text 正确 |
| T4 | Streaming — SSE chunk 正确拼接 | 分片推 `agent_message_chunk`，content 拼接完整 |
| T5 | Finalization — isFinalStop 收尾 / 超时 Fail Closed | isFinalStop 前内容收齐；no-stream 超时抛错 |
| T6 | AgentResponse — content/external_session_id/metadata | content 正确、external_session_id=runtime.id、metadata 含 protocol |
| T7 | Session — None→runtime.id→复用 | 两轮同 runtime.id 会话 |
| T8 | Frozen Port — `AgentAdapter.send(request, connection) -> AgentResponse` 不变 | isinstance + 签名 + acp.py 无新异常类 |

测试文件：`backend/tests/test_step10_1_3_tencent_acp_wire.py`（本地 SSE 模拟 13 项 +
`test_live_tencent_acp_wire_smoke` 真实 smoke，缺 env 自动 skip）。

## 真实调用（env 门控）

在 `backend/.env` 放入（方式 B 三值）：

```
TENCENT_ACP_URL=https://<runtime>.sandbox.codebuddy.cn/acp
TENCENT_ACP_TOKEN=<links.acpLink.token>
TENCENT_RUNTIME_ID=<runtime.id>
TENCENT_ACP_TIMEOUT_S=60        # 可选
```

然后：

```
cd backend && python -m pytest tests/test_step10_1_3_tencent_acp_wire.py -q -k live
```

真实运行时若 SSE 事件形状与本地模拟有差异（`codebuddy.ai.isFinalStop` 位置、content
块字段），只需在 `acp.py` 的 `_sse_event_texts_and_final` / `_json_collect_text` 内
校准（宽容递归已尽量兼容），**Port / Domain / Dispatch / Coordinator 一律不动**。

## Open items（本步边界外，明确不做）

1. 不接 RuntimeCoordinator 全链（T1–T8 全绿后才做第二阶段 Runtime E2E）；
2. 不做 initialize（腾讯 Runtime 创建即自带 session，以实际要求为准）；
3. 不做 TokenRefreshManager / ACPConnectionManager / RetryManager / RuntimeManager
   （wecom-aibot-bridge 的生产工程行为，不是协议兼容验证）；
4. 不做 Runtime 创建 API（方式 A 管理面，需要 AgentOS token + X-Source-App；
   本步用方式 B 预创建 Runtime 三值）。
