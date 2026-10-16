# Step 10.7.2 — Evidence Read & Response Persistence Semantics Audit

> **审计步，生产代码改动：0 行。**
> 交付物 = 本报告 + 审计快照测试
> （`tests/test_step10_7_2_evidence_read_response_persistence_audit.py`）。
>
> 只审计两个问题：
>
> **A. Read Path**：`Interaction → 管理员最小查询 → Activity` 需要哪些
> **精确查询契约**？
> **B. Response Persistence**：`AgentResponse → Interaction.response_payload`
> **到底记录什么**？

---

## 0. 范围与不做

**做**：从 10.7.1 的「数据面够用、读取面空白」出发，把**最小读契约**与
**response_payload 记录语义**裁决清楚（都是语义/契约层，不写实现）。

**不做（本步明确排除）**：

- 不实现任何 read repository 方法 / list / 分页；
- 不写 response_payload（不触碰 9.1 冻结边界）；
- 不引入 Read Model / QueryService / CQRS（10.7.1 Q5 已裁决不需要）；
- 不加 interactions 新列（conversation 不上列，见 A3）；
- 不拍板「证据读端点归哪个 API 面」（10.7.1 Q6 张力延续）。

---

## A. Read Path —— 需要哪些精确查询契约

### A1 管理员的两条定位入口（从 10.7.1 的 12 问推导）

| 入口 | 管理员手里有什么 | 要得到什么 |
| --- | --- | --- |
| ① 排查单条请求 | 交互身份 `(channel, external_interaction_id)`（如告警/webhook 日志里） | 这一次请求的完整答案 + Evidence 时间线 |
| ② 看会话历史 | conversation / identity（「这个用户/这个群聊发生过什么」） | 该会话的请求列表 → 逐条下钻 Evidence |

### A2 最小查询契约集

| # | 契约 | 回答什么 | 现状 |
| --- | --- | --- | --- |
| 1 | `interactions.get_by_id(id)` | 按主键取单条 | ✅ 已有（10.4.2） |
| 2 | `interactions.find_by_channel_and_external_id(channel, external_id)` | 入口①：交互身份精确查 | ✅ 已有（10.5.2） |
| 3 | `interactions.find_by_session(session_id)` | 入口②：会话 → 请求列表 | ❌ **缺失**（数据面可行：`interactions.session_id` FK + index） |
| 4 | `activities.find_by_interaction(interaction_id)` | 一次请求的 Evidence 时间线 | ❌ **缺失**（activity port 只有 `append`；数据面可行：`interaction_id` index） |
| 5 | （候选，暂不冻结）按 channel + 状态 + 时间窗的运营列表 | 「最近 delivery 失败的请求」类 | 等真实用例拍板，不预设 |

**最小集 = 恰好补 2 个读方法（#3 / #4）**：#1/#2 已存在；#5 无证据不设计。

### A3 为什么 `find_by_session` 是最小契约（而不是按 conversation 查）

- `external_conversation_id` **不在 interactions 列上**（只在 `request_payload`
  JSON 里）——按 conversation 直接查 interactions 需要 JSON 扫描或加列（schema
  演进，无必要）；
- 但 `sessions` 表已有 `(identity_id, external_conversation_id)` 可达路径
  （10.4.2 弱键读路径），且 conversation 归 session 所有（8.3 冻结）；
- 因此**管理员入口② 的正确形状是两步链**：
  `conversation → session（既有读路径）→ find_by_session(session_id)`；
- REUSE 语义下同一 session 天然聚合多条 interactions——**会话历史免分组**
  （这就是 10.6.1/10.6.2 的粘性语义在读侧的红利）。

### A4 查询键 / 排序 / 分页语义

- **查询键纪律**：所有读键都在 Channel scope 内解释（延续 10.5.x 身份纪律）；
  无 `user_id` 维度读键（User 在 Runtime 不可达，10.6.1）；
- **排序**：`interactions` / `activities` 均按 `(created_at, id)` 单调
  （append-only 事实，10.7.1 B5 已证）——最小契约按此升序返回时间线，**无需
  排序参数**；
- **分页**：list 类查询（#3/#5）需要 `limit` + 游标（`created_at/id`）——形状
  记录在案，实现等真实消费场景；单条类（#1/#2/#4）无需分页（单请求的
  Evidence 数量有界）；
- **落点纪律**：扩**既有 Port** 的最小读方法（与 9.4 `find_active_by_channel`
  给 Connection 补最小读路径同一模式）——**不建** QueryService / ReadModel /
  CQRS。

---

## B. Response Persistence —— `response_payload` 到底记录什么

### B1 裁决：AgentResponse 的持久化快照（Evidence）

> **`response_payload` 的语义 = AgentResponse 的持久化快照。**
> 形状 = AgentResponse 字段面**原样**（三字段，不解释、不加工、不截断——
> Evidence = 事实）：

```
response_payload = {
    "content":             AgentResponse.content,
    "external_session_id": AgentResponse.external_session_id,
    "metadata":            AgentResponse.metadata,
}
```

- `content`：Agent 产出的内容——补上 10.7.1 的缺口「Agent 回了什么不可见」；
- `external_session_id`：作为**响应级事实**记录（这次响应携带的 Agent 侧
  引用）。它 ≠ `sessions.external_session_id` 的会话级绑定——主链路今天
  从不写后者（Coordinator 的 session_creation 调用不传它，探针 B4 实证
  session 行该列为 NULL）。响应级快照同时为未来 CP↔Agent 跨系统串联留下
  第一手证据（10.3.1 §4 / 10.7.1 Q9 的真实触发点）；
- `metadata`：protocol-neutral 结构化附加信息，原样保留。

### B2 何时有值 / 何时 NULL（状态矩阵）

| processing | delivery | response_payload | 语义 |
| --- | --- | --- | --- |
| FAILED（dispatch 前失败） | pending | **NULL** | 响应从未诞生（不可伪造） |
| SUCCEEDED | delivered | **有值** | 正常成功 |
| SUCCEEDED | FAILED（**Case B**） | **有值** | 响应诞生了，只是投递失败 |

**关键推论（连接 10.5.3）**：response_payload 一旦按此物化，10.5.3 记录的
**「Delivery Retry 无承载物」就有了承载物候选**——Agent 已成功的那条响应被
持久化，未来 Delivery Retry 可以只重投它，而**绝不重跑 Agent**。
本步仍不实现 Retry 机制（10.5.3 未决问题不变）。

### B3 谁来写（9.1 边界张力，记录不修）

- `RuntimeCoordinator.execute() -> None`（9.1 冻结）：AgentResponse 在
  Coordinator 内部被消费（content/metadata 流入 OutboundResponse）后**不外泄**
  ——PersistingCoordinator **观察不到它**（探针 B3 静态证明 wrapper 无
  AgentResponse 引用）；
- 因此写入需要以下之一：stage 级回调 / RuntimeResult（对 9.1 的修订）——
  **「记录什么」本步已裁决；「谁来写」是对 9.1 边界的接线裁决**；
- 它与 10.7.1 Q2（成功路径中间段 Evidence）是**同一个接线点**：未来一次
  边界修订可同时解决两件事（观察 AgentResponse 的诞生即同时获得 dispatch
  段的 Evidence 锚点）。

### B4 不记录什么

- **不记录 OutboundResponse**（投递产物）——它的 content/metadata 与
  AgentResponse 重复，投递成败已在双状态列；「渠道实际收到的 payload」若
  未来出现改写场景（今天不存在）再单独裁决；
- **不做脱敏 / 截断决策**——AgentResponse 无凭据字段（credential 只以
  credential_ref 形式存在于 Connection）；大小策略属运营决策，等真实数据
  规模证据。

---

## 验收（探针钉住的事实）

| # | 条件 | 测试 |
| --- | --- | --- |
| 1 | 既有读面 = get_by_id + find_by_channel_and_external_id（identity 精确查可用） | A1 |
| 2 | find_by_session 缺失但数据面可行（session_id FK+index；REUSE 聚合请求列表） | A2 |
| 3 | activity 读方法缺失但数据面可行（interaction_id index；时间线有序） | A3 |
| 4 | 入口②两步链真实可行（conversation → session → interactions） | A4 |
| 5 | Port 无分页形状（limit/offset 偷渡为零） | A2/A3 静态 |
| 6 | response_payload 三种终态下全部 NULL（缺口实证：FAILED / succeeded×delivered / succeeded×failed） | B1 |
| 7 | AgentResponse 字段面 = {content, external_session_id, metadata}（记录形状的来源） | B2 |
| 8 | wrapper 观察不到 AgentResponse（execute() -> None，9.1 边界） | B3 |
| 9 | sessions.external_session_id 主链路不写（响应级事实今天无处落地） | B4 |
| 10 | 契约成文 | C1 |

---

## 一句话版

| 问题 | 答案 |
| --- | --- |
| Read Path 需要哪些契约？ | 最小集 = 恰好补 2 个读方法：`find_by_session(session_id)`（会话→请求列表）+ `activities.find_by_interaction(interaction_id)`（Evidence 时间线）；单条身份查询已有；运营列表等真实用例 |
| 为什么按 session 而不是按 conversation 查？ | conversation 不在 interactions 列上、归 session 所有；入口② = conversation → session（既有弱键读路径）→ find_by_session，REUSE 天然免分组 |
| 排序/分页？ | 排序天然（created_at, id 单调）；list 需 limit+游标——形状记录，实现等场景 |
| response_payload 记录什么？ | **AgentResponse 的持久化快照**：{content, external_session_id, metadata} 三字段原样（不解释/不加工/不截断） |
| 何时 NULL？ | 仅 dispatch 前失败（响应未诞生）；succeeded×delivered 与 succeeded×failed（Case B）都有值 |
| 谁来写？ | 未裁决——wrapper 观察不到 AgentResponse（9.1 execute() -> None）；与 10.7.1 中间段 Evidence 是同一个接线点 |
| 对 10.5.3 的意义？ | response_payload 物化后，Case B 的「Delivery Retry 无承载物」获得承载物候选（Retry 机制仍不做） |
