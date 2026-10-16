# Step 9.8.1 — Runtime Execution Ordering Resolution

> 第一次发现真实的「契约语义顺序 ≠ 实际执行顺序」问题并正式修正。用**真实输入依赖**
> 裁决执行顺序，不凭架构对称性。

## 一、矛盾精确化

两个已冻结事实：
- 8.3 Session Resolution：消费 `identity_id + connection_id + external_conversation_id`
  ——消费一个**已经确定的 Connection**；
- 8.4 Routing：`channel_id → active Connection → RoutingResult`——负责**确定
  Connection**。

所以真实依赖是 `Routing → connection_id → Session Resolution`，而不是
`Session Resolution → Routing`。旧的 `Identity → Session → Routing → Dispatch`
是 Runtime 的**语义组成关系**，不是最终的 imperative execution order。

## 二、正式冻结的真实执行顺序

```
RUNTIME_EXECUTION_ORDER = (
    "identity_resolution",
    "routing",
    "session_resolution",
    "session_creation",    # conditional：仅 CREATE_NEW 分支
    "agent_dispatch",
    "delivery",
)
```

1. Inbound → 2. Identity → 3. Routing → 4. Session → 5. Session Creation [仅
CREATE_NEW] → 6. Dispatch → 7. Delivery。

### 为什么 Routing 提前

Routing 输入极小（只要 `channel_id`，Inbound 已提供）；不需要 User / Identity /
Session / Content / Conversation。Identity 与 Routing 语义上是两个独立决策（Who /
Where），先固定为串行 Identity → Routing（不引入并行）。

### Session 此后获得完整输入

```
IdentityResolution.identity.id ─┐
                                ├─→ SessionResolution(identity_id, connection_id,
RoutingResult.connection_id ────┘    external_conversation_id)
                                       ├─ REUSE_ACTIVE（不创建）
                                       └─ CREATE_NEW → Session Creation（9.7）
                                                  → control_plane_session_id → Dispatch
```

## 三、不破坏 8.3 / 8.4

- 8.3 契约不变（identity_id / connection_id / external_conversation_id / mode /
  existing_session_id；不选择 Connection）——只是明确了 Runtime 在调它之前必须先
  Routing；
- 8.4 不变（RoutingExecutor.execute(channel_id)；0/1/>1 → fail/success/fail；
  Routing 不知道 Session / Identity / Conversation / Content）。

## 四、Contract Graph ≠ Execution Order（架构表述修正）

- **Runtime Contract Graph**（谁提供什么给谁）：
  `Inbound → Identity；Inbound.channel_id → Routing；Identity.id + Routing.connection_id
  + Inbound.external_conversation_id → Session → Dispatch → Delivery`
- **Runtime Execution Order**：`Identity → Routing → Session → Session Creation? →
  Dispatch → Delivery`

以后不再把 `Inbound → Identity → Session → Routing → Dispatch → Delivery` 写成执行
顺序——它只是语义组成。

## 五、Fail Closed 顺序（一起冻结）

```
Identity failure → STOP
Routing failure → STOP
Session failure → STOP
Session Creation failure → STOP
Dispatch failure → NO Delivery
Delivery failure → processing 仍 SUCCESS、delivery = FAILED（processing ≠ delivery）
```

## 六、组件纪律（Ordering Architecture Tests 锁定）

1. 顺序：IDENTITY → ROUTING → SESSION → SESSION_CREATION(conditional) → DISPATCH → DELIVERY
2. Routing 必须先于 Session（Session 必填 connection_id；Routing 产出它）
3. Session 不得自己 Routing（不得调用 RoutingExecutor / Connection 仓储）
4. Routing 不得创建 Session（不得用 SessionRepository / SessionCreationExecutor）
5. CREATE_NEW 才允许 Session Creation（REUSE → NO CREATE）
6. Dispatch 必须位于 Session 之后（AgentRequest.control_plane_session_id 已获得）
7. Delivery 必须最后（无 Delivery→Dispatch / Delivery→Routing）
8. Fail Closed 顺序冻结

## 冻结

- `runtime.py`：`RUNTIME_STAGES` 正式更名并升级为 **`RUNTIME_EXECUTION_ORDER`**
  （6 段，含条件阶段 session_creation）；docstring 记录 Contract Graph 与
  Execution Order 的区分、裁决理由、Fail-Closed 表、组件纪律；
- `tests/test_step9_1_runtime_execution.py`：同步到新常量与 6 段顺序；
- `tests/test_step9_8_1_execution_ordering.py`：Ordering Architecture Tests（①~⑧，
  含证据签名检查与「Session 自路由 / Routing 建 Session」mutation 报红）。

全仓 `pytest -q` = 550 passed，lint 0。生产执行组件（9.2~9.7）本步**未修改**。
