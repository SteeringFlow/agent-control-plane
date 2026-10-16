# Step 8.4 —— Routing Contract（不实现 Router，不设计 Routing Rules）

一句话目标：冻结「给定已完成 Identity / Session Resolution 的 inbound
interaction，如何得到一条**唯一的、确定的** Connection（Agent Access Path）」，
并兑现 8.3 的裁决——**Connection 在 Routing 中被选择**。

    InboundInteraction
        ↓ Identity Resolution（8.2）
        ↓ Session Resolution（8.3）
        ↓
    Routing                    ← 本步骤只冻结输出契约
        ↓
    Connection（唯一、确定；Agent Access Path）
        ↓
    Agent

## 1. 契约（冻结在 `app/domain/routing_result.py`）

纯 domain 值对象（只 import dataclasses）：

| 契约件 | 内容 |
| --- | --- |
| 输出 `RoutingResult` | frozen dataclass：`connection_id: int` + `agent_id: int`（均必填） |
| 失败码 | `ROUTING_FAILED = "ROUTING_FAILED"` |

`agent_id` **派生**自被选中的 Connection（`Connection.agent_id`），不是独立
选择——不允许出现「选了 C 却派给别的 Agent」。

## 2. 职责边界（不得职责倒流）

```
Routing
  ├── Resolve Identity  ❌   （8.2 已完成；只能消费上下文）
  ├── Resolve Session   ❌   （8.3 已完成；不得创建/复用 Session）
  ├── Select Connection ✅   （唯一职责）
  └── Dispatch Agent    ❌   （下一步）
```

机器化锁定：`RoutingResult` **只表达 Access Path**——无 identity / session /
conversation / content 字段，不含任何 `find_`/`resolve_` 意图。

## 3. MVP 选择依据（最小机制，不是规则引擎）

```
Channel → 唯一 Connection → Agent
```

- 选择键只有 `channel_id`；resolved Identity / conversation 只是上下文，**不是
  选择键**；`content` 不参与选择；
- 结果必须唯一且确定：Channel 有且仅有一条 active Connection → 选它；0 条或
  >1 条 → 无法唯一确定 → `ROUTING_FAILED`（Fail Closed）；
- **不做** RoutingRule / RoutingPolicy / RoutingResolver / RoutingTable /
  priority / weight / fallback / load_balance / round_robin——等真实用例出现
  「一个 Channel 多条 Connection 需要按用户/会话/内容选择」时才引入。

## 4. 反向审计精确扩边

6.7 的 Runtime Boundary 审计（test_k）原本禁止一切 routing 组件；8.4 把
`app/domain/routing_result.py::RoutingResult` 加入精确白名单（文件 + 类名 +
模块 import 三者都放行）。除此之外的 `Router` / RoutingRule / Dispatcher /
Resolver 仍是偷渡——mutation 注入 `router.py::class Router` 必须报红。

## 5. 仍然不做

❌ Router ❌ RoutingRule / Policy / Resolver / Table ❌ priority/weight/fallback/
load_balance/round_robin ❌ Agent Dispatch ❌ Channel Adapter ❌ API
❌ Session 实体 / SessionRepository（等 Session Vertical Slice）

## 6. 验收

`tests/test_step8_4_routing_contract.py`：12 项全绿（纯 domain / 字段面 /
失败码 / 运行时行为 / 输出只表达 Access Path / agent_id 派生自 Connection /
无规则引擎类 / MVP 依据冻结 / Fail Closed / mutation 探测）。

`python -m pytest -q` → **342 passed**（330 回归 + 8.4 新增 12）。

后续：Routing 的 Resolver 实现（thin 确定性选择）→ Session Vertical Slice →
Management API → WeCom Vertical Slice。
