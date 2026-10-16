# Step 8.5 —— Agent Dispatch Contract & Boundary（不实现 Dispatcher / Adapter）

一句话目标：8.5 不再做任何“决策”（8.1~8.4 已完成），只冻结「如何把**已确定**
的 Runtime 请求，交给**已确定**的 Agent」的边界。

    RoutingResult(connection_id, agent_id)
              ↓
    AgentRequest ──→ AgentAdapter（未来）──→ Agent ──→ AgentResponse

核心原则：**Dispatch is execution, not selection.**

## 1. 契约（冻结在 `app/domain/agent_request.py` / `agent_response.py`）

| 契约件 | 内容 |
| --- | --- |
| `AgentRequest` | frozen dataclass：`agent_id` / `control_plane_session_id` / `content`（必填）+ `external_session_id` / `metadata`（可选） |
| `AgentResponse` | frozen dataclass：`content`（必填）+ `external_session_id` / `metadata`（可选） |
| 失败码 | `AGENT_DISPATCH_FAILED` / `AGENT_TIMEOUT` / `AGENT_RESPONSE_INVALID`（`dispatch_failure.py`） |

定位：
- `AgentRequest` = Control Plane → Agent Adapter 的统一交付模型；不是 ACP/A2A/HTTP/
  Agent 内部 Message（协议差异归 AgentAdapter）；
- `AgentResponse` = AgentAdapter 归一化后的统一响应；协议差异归 AgentAdapter。

## 2. 职责边界（Dispatch = execution，不是 selection）

```
Routing    = Where should this interaction go?
Dispatch   = Send this interaction there.

Identity Resolution ❌  Session Resolution ❌  Connection Selection ❌
Agent Selection      ❌  Protocol Translation ❌（归 Adapter）
Agent Dispatch       ✅  Channel Delivery     ❌
```

机器化锁定：不新增 AgentDispatcher / AgentDispatchRequest/Result/Context/Policy
等任何 DTO；全仓无 Dispatcher / Router / RoutingRule / Resolver / Policy 类。

## 3. Session 边界（8.5 的关键克制点）

- `control_plane_session_id`：这次 Agent 调用属于哪个 **Control Plane Session**；
  它**不等于** Agent internal session；
- `external_session_id`：Agent 侧**已存在**的、由 Agent / Agent Protocol 管理的
  session 标识；Control Plane 可以继续传给 Adapter，但**不创建/管理/解释**其生命周期；
- 所有权：Control Plane owns → CP Session；Agent owns → Agent Conversation /
  Context / Memory / Business State；
- Agent 首次响应返回 `external_session_id` 的保存/关联属于后续 Session 生命周期
  处理——8.5 不提前引入 Session 更新机制。

## 4. Connection 边界

- Dispatch 消费的是 **RoutingResult（connection_id, agent_id）**——不重新 Routing，
  不面对「一堆 Connections 自己选」；
- `AgentRequest` 只带 `agent_id`（已确定），**不携带** `connection_id` / `channel_id`；
- `agent_id` 来源 = 被选中 Connection 的 `agent_id`（与 8.4 一致）。

## 5. AgentAdapter 所有权边界（8.5 只冻结归属，不实现）

Adapter 可以做：protocol serialization / authentication / connection-level
transport / protocol response parsing / timeout handling / protocol-specific
error translation。
Adapter **不可以**做：Identity/Session Resolution、Routing、Connection Selection、
RBAC、Capability Authorization、Channel Delivery、Agent business logic、
Agent internal memory management。

## 6. Fail Closed / Dispatch Failure Contract

- 没有确定 Connection → **不 Dispatch**；Agent 不可确定 → **不 Dispatch**；
  AgentResponse 无法验证 → **不继续 Runtime 成功链**；
- 失败码互相独立：`AGENT_DISPATCH_FAILED`（transport/protocol 失败）、
  `AGENT_TIMEOUT`（只是慢，单列）、`AGENT_RESPONSE_INVALID`（Adapter 没产出统一响应）；
- 绝不「随便挑一个 Agent」或「猜一个 content」。

## 7. AgentRequest 不携带什么（结构化锁死）

`user_id` / `identity_id` / `channel_id` / `connection_id` / `routing_rule` /
`capability` / `permission` / `tool` / `memory` / `conversation_history` /
`session_state`——不能塞进 metadata 当“万能逃生舱”；metadata 只是普通
`dict | None`（结构化限制，不是穷举 JSON key）。

## 8. 仍然不做

❌ AgentDispatcher ❌ ACP/A2A/HTTP/Custom Adapter ❌ 真实 Agent ❌ 网络请求
❌ Retry / Circuit breaker / Agent failover ❌ Capability / Permission
❌ Agent Runtime ❌ API

## 9. 验收

`tests/test_step8_5_agent_dispatch_contract.py`：17 项全绿（AgentRequest/
AgentResponse 字段面 / failure codes / 无 Dispatch 组件 / Adapter 空壳 /
Connection 边界 / Agent ownership / metadata 结构化 / Fail Closed / mutation）。
6.7 反向审计放行 `dispatch_failure.py`（失败码文件，不含 Dispatcher 类）。

`python -m pytest -q` → **359 passed**（342 回归 + 8.5 新增 17）。

后续：Step 8.6 Delivery → AgentDispatcher / Adapter 实现 → WeCom Vertical Slice。
