# Step 10.3.1 — Multi-Agent Routing Vertical Slice

> **目标只有一个**：证明同一个 Control Plane 可以**在不修改 Runtime Core 的
> 情况下**，把不同 Connection 正确路由到不同 Agent。这是 CP 从
> "Channel Bridge" 迈向 "Control Plane" 的关键验证。
>
> **本步生产代码改动：0 行。** 全部产出 = 测试 + 架构守卫 + 文档。

---

## 1. 实验模型（第一阶段：Routing Boundary，不是单 Channel 多 Agent）

既有 Routing 语义（8.4 冻结）：`find_active_by_channel(channel_id)` 必须得到
**恰好一个** ACTIVE Connection——即 **一个 Channel 对应一个 Active Agent**。
这不是 bug，是重要的架构事实。因此 10.3.1 第一阶段验证：

```
             ┌── Channel A ── Connection A ── Agent A（真实 HTTP ACP runtime）
Control Plane
             └── Channel B ── Connection B ── Agent B（真实 HTTP ACP runtime）
```

两个 Channel 使用**同一种** WeComAdapter / ACPAdapter（不新增任何 Port、
不引入第二种 Agent 协议），仅 channel_id / Connection / Agent endpoint 不同。

```
Inbound(channel_A) → Identity → Routing → Connection_A → Agent_A
Inbound(channel_B) → Identity → Routing → Connection_B → Agent_B
```

## 2. 刻意不做（范围控制）

```
❌ 第二种 Agent 协议（A2A / HTTP Agent / MCP）
❌ Agent Registry / Routing Rules 表
❌ Load Balancing / Failover / Priority / Weight / Policy Engine
```

否则「多 Agent 路由验证」会膨胀成新的平台基础设施。

## 3. Routing Capability Gap（诚实记录，不修）

**单 Channel 挂多个 ACTIVE Connection → `RoutingFailedError`**（8.4 数量判定
Fail Closed 的正验证，`test_a5`）。真实产品最终很可能需要：

```
WeCom ──┬── HR Agent
        ├── Sales Agent
        ├── IT Agent
        └── Engineering Agent
```

但「一个 Channel 是否/如何连接多个 Agent」是 CP 的核心产品问题，涉及
Inbound Interaction Identity + Idempotency + Selection Semantics——现在不改
Routing，先让现实暴露缺口，再单独开 Step。

## 4. 额外发现：external_session_id 不在 Runtime 主链路回流（记录）

`RuntimeCoordinator.execute()`（9.8.2 冻结）调用 dispatch 只传三参
`(routing_result, control_plane_session_id, content)`——`AgentResponse.
external_session_id`（Agent 侧会话引用）不回流到后续请求。10.1.2 已验证
dispatch 层的回流能力存在；主链路接通属于 Runtime/Interaction persistence
的未来演进，10.3.1 不改（同「先让现实暴露架构缺口」原则）。

---

## 5. 测试三层（20 项）

| Layer | 文件 | 数量 | 内容 |
| --- | --- | --- | --- |
| A Routing isolation | `test_step10_3_1_routing_isolation.py` | 6 | channel A→agent A / channel B→agent B；任意调用顺序不串；**agent_id 是派生不是选择**（result.agent_id 由 connection.agent_id 复核）；**Capability Gap 正验证**（双 ACTIVE → Fail Closed）；0 条/未知 channel 回归 |
| B+C Full Runtime E2E + Session isolation | `test_step10_3_1_multi_agent_e2e.py` | 6 | 两个独立本地 ACP runtime（真实 HTTP + 凭据校验）：请求 A 只到 Agent A（B 永远收不到）/ 反之；A,B,A,B 交错全程零串扰；Delivery A/B 不串（channel_id/conversation/content）；二次请求 REUSE_ACTIVE（CP 侧恰 1 条 session 且归属正确）；全局恰 2 条 session、归属互斥 |
| D Multi-Agent Isolation Guard | `test_step10_3_1_multi_agent_isolation_guard.py` | 7 | AST 守卫（见 §6） |

实验变量：Agent A/B = **真实 HTTP ACP runtime**（本地 ThreadingHTTPServer，
校验 `X-Acp-Credential-Ref`：cred-a/cred-b）；Adapter = **真实 ACPAdapter**；
Runtime = 既有六段零改动。

## 6. Multi-Agent Isolation Guard（AST 守卫，10.3.1 §八）

| 组件 | 断言 |
| --- | --- |
| `RoutingExecutor` | import 白名单（不 import AgentAdapter / adapters / Agent 域）；符号无 `AgentAdapter`/`send`/`AgentRequest`/`AgentResponse`；唯一仓库调用 = `find_active_by_channel` |
| `RuntimeCoordinator` | import 精确白名单（errors/runtime Port/domain）；**无 agent-specific branching**：所有 if/elif 条件不引用 agent_id/content/external_conversation_id/metadata/connection_id（分支只允许 Session mode 与 Fail Closed 判空） |
| `AgentDispatchExecutor` | 不 import Routing 实现；符号无 `find_active_by_channel`/`RoutingExecutor`（不自行选择 Agent）；唯一 send 调用点 = `self._agent_adapter.send`（绕过 RoutingResult 不可能） |
| `ACPAdapter` | 不 import Channel/Identity/Session/persistence/API 域；符号无 `ChannelAdapter`/`SessionRepository`/`IdentityRepository`/`InboundInteraction`/`OutboundResponse` |
| 全仓 | 无 celery/redis/rq/dramatiq/huey/kombu/kafka/pika/consul/etcd3（无新基础设施） |

## 7. 验收（Closure 条件全过）

| 条件 | 结果 |
| --- | --- |
| Channel A → Agent A 正确 / Channel B → Agent B 正确 | ✅（B1/B2/B3） |
| Session A/B 不串 | ✅（C1/C2/C3） |
| Delivery A/B 不串 | ✅（B1-B4） |
| AgentAdapter Port 不变 | ✅ `send(request, connection)` |
| Runtime Core 不变 | ✅ **git diff = 0（production）** |
| Routing 不知道 AgentAdapter / Dispatch 不自行选择 | ✅（D1-D5 AST） |
| 无新基础设施 | ✅（D7） |
| 全量回归 | ✅ **763 passed**（743 + 20 新增） |
| Worktree clean | ✅ |
