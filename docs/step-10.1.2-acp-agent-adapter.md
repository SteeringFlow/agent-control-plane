# Step 10.1.2 — Real ACP Agent Adapter Implementation

> Step 10.1 的第二刀：从 discovery（10.1.1）到**真实拿一个 ACP Agent 接进来**。

## 实验变量（严格控制）

| 层 | 本步取值 | 说明 |
| --- | --- | --- |
| Channel | Test Channel | 变量不放大：只验证 Agent Adapter |
| Identity / Routing / Session | 既有 Runtime | **零改动** |
| Adapter | **真实 ACPAdapter** | `app/adapters/agents/acp.py`（第一个真实 Adapter） |
| Agent | **真实 HTTP ACP Agent** | 本地 ThreadingHTTPServer：真实 TCP/HTTP、会话存储、凭据校验 |

失败时能精确定位在 Adapter / Agent 层，而非同时引入 Channel/Webhook/鉴权等十几个变量。

## 反证实验前提（10.1.1 延续）

仓库无 ACP wire spec → 10.1.2 用「最小 ACP HTTP binding」做**架构反证**：

    POST {Connection.config.endpoint}
      body    {"message": content, "session_id": external_session_id}
      headers Content-Type/Accept: application/json
              X-Acp-Credential-Ref: <Connection.credential_ref>（有才带）
      200     {"content": str, "session_id": str|None}
      其余    Fail Closed

真实外部 ACP-compatible Agent 接入时只需在 `acp.py` 内部校准 wire 细节；**Frozen
Port / Runtime Kernel 一律不动**。

## 实现边界（Protocol Translation，不是 Mini Control Plane）

ACPAdapter 可以：读 `Connection.endpoint/config/credential_ref`；做 ACP auth /
HTTP transport / ACP serialization；管理 ACP protocol session（external_session_id
透传）；ACP response → AgentResponse。

ACPAdapter 不可以：自选 Agent/Connection；查 Repository；建/改 CP Session；重新
Identity/Session/Routing；直接 Delivery；import ORM/SQLAlchemy；把 ACP 专有概念塞进
AgentRequest/AgentResponse。

## 失败映射（只复用既有失败码，不泄漏新错误码）

| 情形 | 落点 | 错误 |
| --- | --- | --- |
| transport / HTTP / protocol（不可达、非 200、不可解、schema 不符） | Adapter raise | `AgentDispatchFailedError` → AGENT_DISPATCH_FAILED |
| ACP 200 语义可用但 Agent 无可用内容（content=""） | Adapter 归一化 `AgentResponse(content="")` → Dispatch 契约校验拒绝 | `AgentResponseInvalidError` → AGENT_RESPONSE_INVALID |
| 401/500/unknown-session 等具体 ACP 错误 | 留在 Adapter 内部 | 不外泄为新 Control Plane 错误码 |

要点：AGENT_RESPONSE_INVALID 是 CP 层契约校验（dispatch 后置校验）抛出的——Adapter
不猜 content，只把空内容如实归一化，由既有 `_valid_response` 判无效；这正是 9.5
冻结语义，Runtime Kernel 因此无需改动。

## 验收结果（A–G）

- A 最小真实调用：`AgentRequest → 真实 HTTP POST → 真实 Agent → AgentResponse`（content
  `ack:hello:turn1`、`external_session_id=agent-session-1`）✅
- A/F Runtime Kernel 全链：Test Channel → RuntimeCoordinator（既有组件）→ 真实
  ACPAdapter → 真实 HTTP Agent → 回复经 Delivery 回到 Test Channel；CREATE_NEW 真实
  建 CP Session ✅（`coordinator.py / agent_dispatch.py / runtime.py / domain/*` 零改动）
- B Response normalization：ACP 原始响应稳定归一化为 AgentResponse 三字段 ✅
- C Session 回流：None → `agent-session-1` → 后续调用带回并复用（Adapter 级 +
  Dispatch 级各一条；**未建** AgentSession/SessionMapping 表，只观察真实行为）；
  未知 session → 409 → AGENT_DISPATCH_FAILED ✅
- D Connection ownership：凭据/endpoint 全部来自传入 Connection（错凭据 401、缺
  endpoint Fail Closed；无全局配置）✅
- E Port 不变：`isinstance(adapter, AgentAdapter)`（runtime_checkable）+ send 签名 ✅
- G 失败映射：transport（连接被拒）→ AGENT_DISPATCH_FAILED；invalid ACP response
  （空内容）→ AGENT_RESPONSE_INVALID；协议异常（非 JSON/非 object/缺 content/坏
  session_id/401）一律 AGENT_DISPATCH_FAILED；acp.py 无任何自定义异常类型 ✅

## 空壳/组件守卫精确扩边（本步落地触发）

`adapters/agents` 从空壳变为「唯一真实实现 acp.py」；`adapters/channels`、
`api/runtime` 仍空壳。同步精确扩边（均为"允许 agents 含 acp.py"，Channel 守卫语义
不变）：

- 8.6 `DELIVERY_SCOPE_ALLOWED` += `("app/adapters/agents/acp.py", "ACPAdapter")`
  （Agent 侧出站实现，非 Delivery/Channel 组件）；
- 8.1 / 8.5 / 8.6 / 6.5 / 7.1 / 9.6 / 9.A / 5.6(persistence_smoke) 空壳断言；
- 10.1.1 的 F 锁定演进为「ACPAdapter 只存在于 app/adapters/agents/acp.py」。

## Open items（10.2 前后连真实 ACP-compatible Agent 再校准）

1. wire 细节（endpoint 路径/消息信封/流式/事件）——真实 Agent 实测；
2. credential_ref 解析（Credential Management，未来）；
3. Agent external session 与 CP Session 的生命周期关联（当前只透传，不做映射表）；
4. timeout/retry、AGENT_TIMEOUT 独立语义。

## 验收数字

`backend/tests/test_step10_1_2_acp_agent_adapter.py` 15 项通过；全仓 `pytest -q` =
**629 passed**，lint 0。
