# Step 10.1.4 — Runtime Vertical Slice Closure & Baseline Freeze

> 不是加功能，是把 10.1.3 → 10.1.4.3 已经验证过的 Runtime Vertical Slice
> **正式冻结**，并建立后续开发的回归基线。
>
> 本 Step：production code change = **0**。

## 1. 冻结的 Runtime 链

```
Channel
  ↓
ChannelAdapter
  ↓
InboundInteraction
  ↓
Identity Resolution          (IdentityResolutionExecutor)
  ↓
Routing                      (RoutingExecutor → RoutingResult)
  ↓
Session Resolution           (SessionResolutionExecutor)
  ├── CREATE_NEW ──→ Session Creation ──┐
  └── REUSE_ACTIVE ─────────────────────┤
         ↓                              ↓
      Agent Dispatch         (AgentDispatchExecutor)
         ↓
      AgentAdapter           (AgentAdapter Port / ACPAdapter)
         ↓
      Agent
         ↓
      AgentResponse
         ↓
      Delivery               (DeliveryExecutor → OutboundResponse)
         ↓
      ChannelAdapter
```

## 2. 六个 Runtime Boundary

| # | Boundary | 冻结内容 | 归属 |
| --- | --- | --- | --- |
| 1 | Identity | `(channel_id, external_identity_id)` → Identity → User；actor 是 `Identity.id` | `identity_resolution.py` |
| 2 | Routing | `channel_id` → 唯一 active Connection → 派生 `agent_id`；**不选 Agent、不与 Agent 通信** | `routing.py` |
| 3 | Session | 隔离键 = `Identity × Connection × external_conversation_id` | `session_resolution.py` / `session_creation.py` |
| 4 | Dispatch | `RoutingResult → Connection → AgentAdapter.send(request, connection)`；**不实现协议** | `agent_dispatch.py` |
| 5 | Agent Adapter | protocol auth / serialization / transport / response parsing | `adapters/agents/acp.py` |
| 6 | Delivery | `AgentResponse → OutboundResponse → ChannelAdapter.deliver`；**不读库、不重选路** | `delivery.py` |

## 3. 执行顺序（冻结）

唯一来源：`app/application/runtime.py :: RUNTIME_EXECUTION_ORDER`

```
("identity_resolution", "routing", "session_resolution",
 "session_creation",        # conditional：仅 CREATE_NEW 分支执行
 "agent_dispatch", "delivery")
```

`session_creation` 是**条件阶段**，因此有两条真实执行路径：

```
CREATE_NEW:
  Identity → Routing → Session Resolution → Session Creation → Dispatch → Delivery

REUSE_ACTIVE:
  Identity → Routing → Session Resolution ─────────────────→ Dispatch → Delivery
                                     （Session Creation 被跳过）
```

两条路径均由 Closure Audit 的 spy 跟踪**真实执行**（不是只读常量）验证：
`test_create_new_path_executes_all_six_stages_in_order` /
`test_reuse_active_path_skips_session_creation`。

## 4. Session Boundary（冻结）

隔离键三元组：

```
Identity.id  +  Connection.id  +  external_conversation_id
```

| 输入 | 决策 |
| --- | --- |
| 同 Identity + 同 Connection + 同 conversation | `REUSE_ACTIVE` |
| 同 Identity + 同 Connection + **不同** conversation | `CREATE_NEW` |

- actor 是 `Identity.id`（Channel 边界），**不是 `User.id`**——Coordinator 全文件无 `user_id`；
- 行为证据：`tests/test_step10_1_4_3_reuse_active_session.py`
  （决策序列 `[CREATE_NEW, REUSE_ACTIVE]` / `[CREATE_NEW, REUSE_ACTIVE, CREATE_NEW]`，
  同一 CP Session 被复用、无重复 Session）。

## 5. Dispatch Boundary（冻结）

```
RoutingResult ──→ AgentDispatchExecutor ──→ Connection ──→ AgentAdapter
```

- **Router 不负责 Agent 通信**：`routing.py` 不 import `AgentAdapter`、不调用 `send`；
- **AgentDispatchExecutor 不实现 ACP / HTTP**：全仓传输实现（http / urllib / socket …）
  唯一归属 `app/adapters/agents/acp.py`；Runtime Core（application + domain）
  **零传输依赖**，Dispatch 只调注入的 `self._agent_adapter.send`；
- 协议仍然属于 `AgentAdapter`。

## 6. Delivery Boundary（冻结）

```
AgentResponse ──→ OutboundResponse ──→ DeliveryExecutor ──→ ChannelAdapter
```

- `OutboundResponse` 构造面 = `channel_id / external_conversation_id / content /
  metadata`（4 个字段，不带任何内部 id）；
- `DeliveryExecutor` 只依赖 `ChannelAdapter` Port——**不依赖 UoW / Repository**（不读库）；
- **processing success ≠ delivery success**：
  `AGENT_DISPATCH_FAILED` 与 `CHANNEL_DELIVERY_FAILED` 是两个独立 typed error。
  Delivery 失败时 processing 仍 SUCCESS、delivery = FAILED（4.2/5.3 冻结语义，
  9.8.3 已行为验证）——**后续功能开发不得把两者合并**。

## 7. Failure Boundary（一并封板）

| Failure Stage | typed Error | 停止语义 |
| --- | --- | --- |
| `IDENTITY` | `IdentityResolutionFailedError` | STOP（不进入 Routing） |
| `ROUTING` | `RoutingFailedError` | STOP（不创建 Session） |
| `SESSION` | `SessionResolutionFailedError` | STOP（不进入 Dispatch） |
| `AGENT_DISPATCH` | `AgentDispatchFailedError` | STOP（**No Delivery**） |
| `AGENT_EXECUTION` | `AgentResponseInvalidError`（= `AGENT_RESPONSE_INVALID`） | STOP（No Delivery） |
| `CHANNEL_DELIVERY` | `ChannelDeliveryFailedError` | **processing 仍 SUCCESS**，仅 delivery = FAILED |

约束：

- Coordinator **无 try/except**：错误按 stage 语义原样传播，不吞包、不归一成 `RuntimeFailed`；
- 每个 stage 只 raise **自己的** typed error（Audit 逐模块校验 import 面，失败语义不跨 stage 借用）；
- 本阶段**不引入** retry framework / circuit breaker / failover / queue / Saga / Outbox /
  distributed lock。

## 8. External Agent 状态（重要：避免误读）

| 项目 | 结果 |
| --- | --- |
| Tencent ACP Wire protocol | PASS |
| Authentication（acpLink Bearer） | PASS |
| SSE transport | PASS |
| Prompt | PASS |
| Final-stop detection | PASS |
| CP Dispatch | PASS |
| CP Delivery | PASS |
| **Agent business task** | **NOT VERIFIED** |

`10.1.4.2` 的真实调用中，Runtime 返回的 `delivery.content` 为
`Authentication required. Please use /login command to sign in to your account`——
链路（Bearer 鉴权 → SSE → prompt → agent_message_chunk → 收尾 → 回流 session →
CP Session 创建 → Delivery）**全部技术打通**，但该 Runtime 背后的 Agent 自身处于
需登录态 / 缺少有效 `CODEBUDDY_API_KEY`，因此返回的是登录提示而非任务回答。

Baseline 表述（防止误判）：

> External Agent transport/integration is verified; Agent-internal business
> execution remains environment-dependent and is intentionally outside this closure.

**这不是 Control Plane failure**，也不属于本 Closure 的验证范围；待 Agent 侧凭据就绪后
重跑 `test_step10_1_4_real_agent_runtime.py` 即可验证业务回答（无需改测试或生产代码）。

## 9. Baseline

```
Runtime MVP Baseline
────────────────────────────────
Identity Resolution       FROZEN
Routing                   FROZEN
Session Resolution        FROZEN
Session Creation          FROZEN
REUSE_ACTIVE              FROZEN
Agent Dispatch            FROZEN
ACP Adapter               FROZEN
Delivery                  FROZEN
Failure Boundaries        FROZEN
Execution Ordering        FROZEN
```

测试基线（Closure Audit 后实测总数）：

```
pytest -q          662 passed          （含 2 项 env 门控 live test）
lint               0
production code change   0
```

无凭证环境：2 项 env 门控 live test（`10.1.3` wire smoke、`10.1.4.2` E2E）自动
skip → `660 passed, 2 skipped`，其余全部通过。

## 10. Freeze Rule（从本 Step 起生效）

后续功能若认为 Runtime Core 需要修改，**不能直接修改**，必须先回答：

1. 现有 Runtime contract 为什么无法满足？
2. 是真实测试暴露的问题，还是新功能需求？
3. 是否可以在 Application / Adapter 层解决？
4. 是否破坏已有 Failure Boundary？
5. 是否破坏 Session Boundary？
6. 是否需要重新打开 Runtime Baseline？

```
                 Runtime MVP
                     │
              ┌──────┴──────┐
              │   FROZEN    │
              └──────┬──────┘
                     │
        ┌────────────┼────────────┐
        ↓            ↓            ↓
     Channel       Agent       Management
      Layer         Layer          UI
        │            │             │
        └────────────┼─────────────┘
                     ↓
             Runtime Contract
              不随意变化
```

后续开发进入产品层（Channel / Agent / Management UI），不再在 Runtime 基础设施里打转。

## 11. 交付物与验收

交付物：

- `docs/step-10.1.4-runtime-vertical-slice-closure.md`（本文档）；
- `backend/tests/test_step10_1_4_runtime_vertical_slice_closure.py`（16 项 Closure Audit）；
- **无生产代码改动**。

验收：

| 项 | 结果 |
| --- | --- |
| Closure Audit targeted | 16 passed |
| Full regression | 662 passed |
| lint | 0 |
| production code change | 0 |

过程记录（透明）：Audit 编写期间有一条断言最初失败——
`test_coordinator_session_branch_has_no_hidden_branch` 误把 Python 的 `elif`
（AST 中表示为嵌套 `If`）当成隐藏分支。经核对，生产代码 `coordinator.py` 的
`if / elif / else` 恰恰是正确的两分支 + Fail Closed，**问题在测试侧**，已修正测试
断言，生产代码未作任何改动。除此之外，Audit 未发现现有实现与已冻结 contract 不一致。

至此 10.1.3 → 10.1.4.3 的 Runtime Vertical Slice 正式闭环并冻结为
**Runtime MVP Baseline**。
