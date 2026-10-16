# Step 10.5.1 — Interaction Identity & Idempotency Boundary Audit

> **审计步，生产代码改动：0 行。**
> 交付物 = 本报告 + 审计快照测试 19 项
> （`tests/test_step10_5_1_interaction_identity_audit.py`）。
>
> **本步不设计 Redis、锁、队列、幂等服务。** 只做一件事：让 `external_interaction_id`
> 与「重复请求」的**真实状态语义**先出来。
>
> **状态快照时效**：本文记录 10.5.2 之前的真实状态。Q2（DB 约束）、Q4
> （duplicate 行为）、Q8（channel_id 缺口）已由 **Step 10.5.2 — Interaction
> Identity Materialization** 改变；对应快照断言已在测试中就地翻转并标注
> （10.4.1→10.4.2 同一模式），审计史实保留于此。

---

## 0. 范围与不做

**做**：回答 10 个问题（Q1~Q10），每个答案都必须有真实代码 / 真实数据库行为作证。

**不做（本步明确排除）**：

- 不引入 idempotency key / dedup 状态列 / DUPLICATE 状态；
- 不设计 Redis / 分布式锁 / 队列 / 幂等服务 / Outbox；
- 不改 ORM schema、不加索引、不加唯一约束；
- 不改 `PersistingCoordinator` 的创建逻辑（不实现去重）；
- 不引入 `RuntimeResult`（9.1 冻结）；
- 不动 Adapter 层（10.2.2 已裁决：去重不在 wire 层解决）。

---

## Q1. `external_interaction_id` 当前完整生命周期

七跳，全部来自真实代码：

| # | 位置 | 形态 | 证据 |
| --- | --- | --- | --- |
| 1 | WeCom wire XML `<MsgId>` | 渠道分配的消息 ID | `adapters/channels/wecom.py:345` |
| 2 | `WeComAdapter.parse_message` | 必填校验；缺失 → `InboundFormatError`（**不产生 Interaction 行**） | `wecom.py:345-349` |
| 3 | `InboundInteraction.external_interaction_id` | frozen 值对象，**不落库**（无 id / status / created_at） | `domain/inbound_interaction.py:62` |
| 4 | `ChannelInboundService.handle_message` | 透传给 Runtime，不读不改 | `application/channel_inbound.py:117` |
| 5 | `PersistingCoordinator._start` | 写入 InteractionRecord 的**列**，同时**再写一份进 `request_payload` JSON**（双写） | `application/interaction_recording.py:135,138` |
| 6 | `interactions.external_interaction_id` | `String(255) NULL`，无索引、无唯一 | `persistence/models/interaction.py:35` |
| 7 | **终点** | 没有任何代码按它查询；不传给 `AgentRequest`；不在 `OutboundResponse` 里 | 全树检索 |

**结论：`external_interaction_id` 目前是 record-only identity——它有身份的形状，没有身份的用途。**

8.1 契约里写的是「渠道侧交互 ID（**幂等 / 渠道去重依据**）」，10.4.1 也确认「未来入站幂等去重的落点已备好」——
但到 10.4.2 落地为止，**落点备好了，读取方一个都没有**：

- Repository Port 只有 `create / mark_processing / record_outcome / get_by_id`，
  **没有** `find_by_external_id`（`repositories/interaction_record.py`）；
- 全生产代码无任何一处按该列查询。

### 连带发现：结果侧是空白的

- `response_payload` 列存在（model / mapper / domain 三处定义），但 **`interaction_recording.py`
  全文不出现 `response_payload`** —— 没有任何写入方，永远为 NULL；
- `OutboundResponse` 只有 `channel_id / external_conversation_id / content / metadata`，
  **不含 `external_interaction_id`**（8.6 冻结：outbound 不复述归属决策）。

⇒ 投递出去的消息**无法与来源 MsgId 关联**，事后对账只能靠 session / conversation 间接推断。

---

## Q2. DB unique constraint 当前状态

| 对象 | 约束 | 判定 |
| --- | --- | --- |
| `interactions` 表级约束 | **无**（无 `__table_args__`，无 `UniqueConstraint`） | ❌ |
| `interactions.external_interaction_id` | `nullable=True`、`index=False` | ❌ 无去重、无索引 |
| `identities` | `uq_identities_channel_external_id (channel_id, external_id)` | ✅ |
| `connections` | `uq_connections_channel_agent_id (channel_id, agent_id)` | ✅ |

**结论：DB 层零去重能力。同一个 MsgId 到达 N 次 → N 行。**

值得注意的对照：项目里**已有两处**「外部 ID 必须带 channel 作用域」的复合唯一约束纪律
（`identities` / `connections`）——`interactions` 是唯一还没应用这条纪律的表。

---

## Q3. Interaction 创建发生在哪个边界

**唯一构造点 = `PersistingCoordinator._start`（`application/interaction_recording.py:133`）。**

```
HTTP 端点（只提取事实 + 异常映射，不碰 DB）
   ↓
ChannelInboundService.handle_message
   ├─ _load_channel          ← 协议/配置失败 → 抛错，不产生行
   └─ adapter.parse_message  ← 验签/格式失败 → 抛错，不产生行
   ↓
PersistingCoordinator.execute          ← 【创建边界】
   ├─ 事务① create(PENDING) + interaction_received Activity  → commit
   ├─ 事务② mark_processing(PENDING→PROCESSING)              → commit
   ├─ inner.execute(inbound)  六段 Runtime（失败 → 记录终态后 re-raise）
   └─ record_outcome(终态)
```

三条边界事实：

1. **入站协议失败不产生行**（验签 / 格式 / channel 配置）—— 尚未进入创建边界；
2. **Runtime 语义失败也产生行**（identity/routing/session/dispatch/delivery 全部会留下终态记录）；
3. **`_start` 在 `create` 之前没有任何查询或判断**——创建是无条件的。

这个位置也是**未来去重判定唯一合理的落点**——它是全链路中唯一同时具备
「稳定的 external id」与「持久化能力」的边界：

| 候选 | 可否做去重判定 | 理由 |
| --- | --- | --- |
| Channel Adapter（wire 层） | ❌ | 10.2.2 已裁决：去重不在 wire 层解决；Adapter 不碰 DB |
| HTTP 端点 | ❌ | 10.2.1 冻结：不直接读写数据库、不碰 Repository |
| RuntimeCoordinator / 六 Executor | ❌ | 9.8.2 / 10.4.1 冻结：零 persistence 接触 |
| **Runtime 调用边界（PersistingCoordinator）** | ✅ | 既有 external id，又有 UoW；且是所有入口的必经点 |

---

## Q4. duplicate create 当前行为

同一个 `external_interaction_id` 连续到达两次（= 渠道重推），真实行为：

- **2 条独立 Interaction 行**，各自完整生命周期，均为 `succeeded / delivered`；
- **2 次 Agent 调用**；
- **2 次渠道投递**（用户收到两条相同回复）；
- 无异常、无告警、无 Evidence 标记。

**这不是「记账重复」，是「副作用重复」。** 证据多一行无害；Agent 被多调一次、
用户多收一条消息有害。且从数据上**无法区分**「第二次是渠道重推」与「用户真的发了两条消息」——
两行的字段形态完全一致。

---

## Q5. PENDING / PROCESSING / SUCCEEDED / FAILED 下重复请求分别意味着什么

| 状态 | 状态表达的事实 | 重复请求到达意味着 | 当前实际行为 |
| --- | --- | --- | --- |
| **PENDING** | 已落库，尚未 `mark_processing`（已接收、未开工） | 同一事件的第二次通知，第一次还没开始执行 | 新建第二行，完整执行第二次 |
| **PROCESSING** | 已进入 Runtime 执行（Agent 可能已被调用，Delivery 未发生） | **最危险的重叠**：第一次仍在进行 | 新建第二行，并发地再跑一遍全部副作用 |
| **SUCCEEDED** | processing 终态成功（delivery 可能 DELIVERED 或 FAILED） | 工作已完成 → 典型的渠道超时重推 | 新建第二行并重做全部副作用 |
| **FAILED** | 终态失败，带 `failure_stage / failure_code / retryable` | 渠道重放或运维重试 | 新建第二行；**旧行永不更新** |

三条必须冻结的语义：

1. **终态不可逆**：`record_outcome` 是终态一次性落定，Repository 不提供任意字段 update
   （Port docstring 明写「刻意不做 update 任意字段」）；
2. **重试 = 新行，不是更新旧行**——append-oriented evidence，与 Activity append-only 同纪律。
   历史失败行永久保留，不会被「后来的成功」覆盖；
3. **`retryable` 是记录字段，不是控制字段**：全生产代码中**没有任何 `if` 分支读它**
   （AST 断言 E3）。它的语义是「给运维/未来看的 transport-vs-契约分类标签」，
   **不是自动重试开关**，也没有任何 retry/replay 消费者。

---

## Q6. Delivery 已成功但 webhook retry 的情况

这是**最需要幂等、且当前最无力**的场景：

1. WeCom 5s 超时重推是渠道既定行为（10.2.2 已知限制，已写入代码注释），不是假设；
2. CP 同步处理，若整条 Runtime > 5s：渠道判定超时并**重推同一 MsgId**，
   而 CP 的第一个请求仍在执行、最终成功投递；
3. 重推到达 → 第二次完整执行 → **用户收到两条相同回复**；
4. 数据上：两行都是 `succeeded / delivered`，**没有任何字段能标明第二行是重复**。

**语义结论：`delivery=DELIVERED` 是不可逆的副作用边界。**
一旦投递成功，同一 `external_interaction_id` 的后续请求**只能是「确认」，不能是「重做」**。

加剧因素（Q1 连带发现）：

- `OutboundResponse` 不含 `external_interaction_id` → 投递出去的消息无法与来源事件关联；
- `response_payload` 无写入方 → 连「上次的结果是什么」都没有存。

---

## Q7. Agent 执行中 retry 的情况

MVP 是同步模型：webhook 线程内跑完整个 Runtime。但渠道 5s 超时重推时，
第一个请求可能**仍在 Agent 调用中**（`AgentDispatchExecutor` 在退出 UoW 之后才
调用 `adapter.send`，外部 I/O 不占事务），第二个请求会并发进入。

真实探针（`test_g1_inflight_retry_has_no_protection`）证明三件事：

1. 首次 Agent 调用执行期间，数据库里能读到该行 `processing_status == "processing"`
   —— **PROCESSING 是真实持久化的 in-flight 事实**（10.4.2 特意用两个事务让它可观察）；
2. 此刻模拟重推（同 MsgId 再走一次完整 Runtime）→ **执行成功**；
3. 结果：2 行 Interaction、2 次 Agent 调用、两次都跑完 → **无任何 in-flight 保护**。

**语义结论**：PROCESSING 可以作为未来「in-flight」判定的**事实依据**，
但它表达的是「有一条正在执行的 Interaction」，**不是「持有一把锁」**。
把它变成互斥手段 = 引入锁语义，属本步明确不做的范围。

---

## Q8. 同一 MsgId 是否可能跨 Channel

**是，可能，且 CP 不能假设 `external_interaction_id` 全局唯一。**

- 结构上：`channel_id` 由**回调 URL path** 决定（`/api/channels/{channel_id}/wecom/webhook`），
  `external_interaction_id` 由**渠道 payload** 决定——两者完全独立；
- MsgId 是渠道各自分配的 ID 空间（数字型 ID 在不同渠道撞车完全可能），CP 无法验证其跨渠道唯一性；
- 真实探针（F1）：同一 external id 在两个 channel 下 → 2 行，语义上确实是两次不同交互。

⇒ **任何去重键必须是 `(channel_id, external_interaction_id)` 复合键**，与
`identities` / `connections` 已有的复合唯一纪律一致。

**但这里有个真实缺口**：`interactions` 表**没有 `channel_id` 列**——
channel_id 只存在于 `request_payload` JSON 快照里。
要做复合键查询，必须先裁决：升为列（schema 演进 → Alembic 触发条件）
还是接受 JSON 查询（SQLite / PostgreSQL 语法差异）。

---

## Q9. 是否需要返回已有 Interaction 的结果

**结论：入站 webhook 场景不需要。**

三条硬事实：

1. **没有返回通道**：`RuntimeExecutionCoordinator.execute()` 返回 `None`
   （9.1 冻结：不引入 RuntimeResult）；`ChannelInboundService.handle_message` 返回 `None`；
   HTTP 层统一 200 空体（10.2.1）；
2. **没有结果可返回**：`response_payload` 列零写入方，永远 NULL；
3. **请求方不需要它**：webhook 是 **ack 协议**——请求方是渠道，只关心是否 200，
   不消费响应体。

**语义区分**：「返回已有结果」是**结果回放**；真正需要的是**副作用抑制**。
这两件事常被混为一谈，但在 webhook 场景只有后者成立。

若未来出现**同步入口**（如 Management API 触发一次 Interaction 并要结果），
那时才需要 `RuntimeResult`——那是 9.1 解冻级别的决策，不属于本步。

---

## Q10. 是否需要真正引入 idempotency key / deduplication state

**需求真实，但本步不引入，且理由不是「以后再说」。**

### 需求是真的

Q4 / Q6 / Q7 三个场景都会产生**副作用重复**，其中 Q6（渠道 5s 超时重推）
是渠道既定行为，不是理论风险。

### 但不引入独立 idempotency key

已有天然幂等键 = **`(channel_id, external_interaction_id)`**。
它不是「新引入的 key」，而是**已经落库、但还没被读取的现有字段**。
8.1 已冻结「`external_interaction_id` = 幂等 / 渠道去重依据」——
再引入一个 `idempotency_key` 会制造**第二个身份来源**，直接与该冻结冲突。

### 也不新增 deduplication state

`processing_status` / `delivery_status` 双状态**已经能回答**「是否已达终态」。
新增 `DUPLICATE` 状态属于 **Execution semantics** 决策（10.2.2 已明确定位：
「deduplication 一旦要做将进入 Interaction persistence / Execution semantics 领域」）。

### 真正缺的不是 key、也不是 state，而是两个前提裁决

| 前提 | 为什么必须先行裁决 |
| --- | --- |
| **P1：`channel_id` 是否升为列** | 复合幂等键的一半当前只在 JSON 里（Q8） |
| **P2：命中重复时做什么** | 静默跳过 / 记 duplicate Evidence / 抛错——三者对 Evidence 语义影响完全不同；且 **FAILED + retryable=True 的重推是否允许真正重放**，是「幂等」与「重试」的直接冲突点，必须先拍板 |

P2 尤其重要：如果无差别抑制所有重复请求，那么 transport 类失败
（`AGENT_DISPATCH_FAILED` / `CHANNEL_DELIVERY_FAILED`，retryable=True）
就永远失去了通过渠道重推**自愈**的机会——这会把「幂等」变成「永久失败」。

### 本步冻结的最小语义（留给 10.5.2+ 实现步）

1. 幂等键 = **`(channel_id, external_interaction_id)`**，复用现有字段，不新增 key；
2. 判定边界 = **Runtime 调用边界**（PersistingCoordinator，Q3 表格已排除其余四处候选）；
3. 已达**终态**命中 → **不产生第二次 Runtime 执行**（副作用抑制，不是结果回放）；
4. **不新增 DUPLICATE 状态**；是否 append 一条「收到重复请求」的 Activity 属实现步裁决；
5. **不做** in-flight 互斥 / 等待 / 锁（Q7 的 PROCESSING 只作事实依据）。

---

## 11. 缺口清单（记录，不在本步修）

| # | 缺口 | 影响 |
| --- | --- | --- |
| G1 | `interactions` 无 `channel_id` 列 | 复合幂等键的一半只在 JSON 快照里 |
| G2 | `external_interaction_id` 无索引、无唯一 | DB 层零去重保障 |
| G3 | 无任何按 `external_interaction_id` 的查询 | 身份无消费方（record-only） |
| G4 | `response_payload` 零写入方 | 结果侧空白，结果回放无数据基础 |
| G5 | `OutboundResponse` 不含 `external_interaction_id` | 投递与来源事件不可关联 |
| G6 | PROCESSING 无消费者 | in-flight 事实存在但无人读 |
| G7 | `retryable` 无消费者 | 记录字段，非控制字段（刻意） |
| G8 | 无 duplicate / replay 语义 | 重推在数据中不可辨识 |

---

## 12. 验收

| 项 | 结果 |
| --- | --- |
| 审计快照测试 | **19 passed**（A1-A4 / B1-B3 / C1-C3 / D1 / E1-E3 / F1 / G1 / H1-H3） |
| 全量回归 | **801 passed**（782 + 19） |
| 生产代码改动 | **0 行**（`git status` 仅两个新增文件：本 doc + 快照测试） |
| 运行时探针 | D1（同 MsgId 双执行）/ E1-E2（终态不可逆）/ F1（跨 Channel）/ G1（in-flight 重推） |
| 边界回归 | H2：Runtime 全链仍零 persistence 接触；H1：无幂等基础设施偷渡 |
