# Step 10.8.5 — Runtime Evidence / Read Closure Audit

> **审计步，生产代码改动：0 行。**
> 交付物 = 本报告 + 关闭探针测试
> （`tests/test_step10_8_5_runtime_evidence_read_closure_audit.py`）。
>
> 核心问题只有一个：**10.8 这一轮到底消除了多少原来被证明存在的
> Evidence / Read 缺口？** ——回到 10.7.1 / 10.7.2 的问题，重跑探针。

---

## 0. 范围与不做

**做**：A~F 六项重跑探针（全部真实运行时验证，不从代码结构推断）+
失败路径弱键的歧义判定（成功路径弱键 ≠ 失败路径弱键，分开回答）。

**不做**：不修失败路径弱键（先回答「是否已造成实际歧义」，结论见 §4）；
不补 Case B 的 response_payload（需失败路径事实通道，未来裁决）；
不实现 read repository。

---

## 1. 缺口关闭矩阵（10.7.1 / 10.7.2 → 10.8 之后）

| # | 缺口（出处） | 10.8 前 | 10.8 后 | 判定 |
| --- | --- | --- | --- | --- |
| G1 | response_payload 零写入方（10.5.1 / 10.7.1 / 10.7.2） | 永远 NULL | 完整成功路径落三字段**原样快照** | ✅ **已关闭**（A1/A2） |
| G2 | external_session_id 不回流（10.3.1 §4 / 10.7.2 B4） | sessions.external_session_id 从不写 | wrapper 从 agent_response 绑定（None 不清空） | ✅ **已关闭**（B1/B2） |
| G3 | 成功路径 session 归属弱键推导（10.6.1 N2 / 10.6.2 / 10.8.1 ③） | 前后快照推导 | `result.session_id` 直接；快照推导源码级删除 | ✅ **已关闭（成功路径）**（C1） |
| G4 | CREATE_NEW Evidence 靠推导（10.4.2 / 10.7.1） | before/after snapshot | `result.session_mode` 直接观察 | ✅ **已关闭**（D1） |
| G5 | 成功路径中间段零 Evidence（10.4.2 / 10.7.1） | 仅 received/completed/session_created | dispatch 锚点（agent_response 即 dispatch 成功事实）+ session 直证；identity/routing 段维持 `failure_stage` 反推 | ◐ **部分关闭（有意维持）**——Evidence = 事实，不为对称制造 |
| G6 | Case B 的 response_payload（10.7.2 B2 矩阵） | NULL | **仍 NULL**（失败走异常通道，Result 不存在）——B2 矩阵已修订 | ❌ **未关闭**（F1/F2 真实探针；需失败路径事实通道，未来裁决） |
| G7 | Delivery Retry 承载物（10.5.3） | 无 | 完整成功交互**有**；Case B（恰需重投的一类）**无** | ◐ **部分成立**——「有数据 ≠ 实现 Retry」仍守住 |
| G8 | 失败路径弱键（10.8.1 §4 开放点） | 存在 | 仍存在；MVP 真实边界下**无实际歧义**（§4 判定） | ⏸ **开放项（不修）** |
| G9 | interaction 无 identity/connection/agent 列（10.7.1 §1.3） | 只能经 session join（前段失败不可达） | 未改（schema 冻结）——「有条件可答」本身是准确事实 | ⏸ **维持** |
| G10 | read repository / Read Model 缺失（10.7.1 Q4/Q5） | 缺失 | 未实现（等真实消费场景） | ⏸ **维持** |

**总账**：被证明的缺口中 **4 项关闭**（G1~G4）、2 项部分关闭（G5/G7）、
1 项诚实修订后仍开放（G6）、3 项按 evidence-first 维持开放（G8~G10）。

---

## 2. A~E 逐项重跑探针结论

### A. Runtime Result → response_payload

- 完整成功路径：三字段快照 `{content, external_session_id, metadata}`
  **原样**落 `interactions.response_payload`（A1）；
- 无损性：快照键面 == AgentResponse 字段面，metadata 结构原样传递、
  不加工不截断（A2）。

### B. Session mapping

- CREATE_NEW 与 REUSE_ACTIVE 两条路径都会把
  `agent_response.external_session_id` 绑定到 Result 指向的那条 Session
  （B1）；
- Agent 后续不返回引用（None）→ 不写入、**不清空**既有映射（B2）。

### C. Session identity（弱键推导是否消失？）

- **成功路径弱键：已消失**——源码级（无 `session_existed_before`、
  `result.session_id` 直取）+ 运行时（interaction.session_id ==
  result.session_id，两条路径）（C1）；
- **失败路径弱键：仍存在**（无 Result，`_record_failure` 仍走
  `find_active_by_identity_and_conversation`）——与成功路径**严格区分**，
  见 §4（C2）。

### D. Session creation Evidence

- `session_created` Evidence 现由 `result.session_mode == CREATE_NEW`
  **直接产生**；源码中 before/after 推导已不存在（D1 静态 + 运行时双证：
  Evidence 的 session_id == Result 的 session_id）。

### E. REUSE_ACTIVE

- 返回正确 session_id（== 首条，复用）；**零** session_created Evidence
  ——「Session reused」仍不制造 Evidence（E1）。

---

## 3. F. Delivery failure：response_payload 到底在什么时间点持久化？（真实探针）

不用代码结构推断，用两个真实探针回答：

| 探针 | 时点 | 观察到的 response_payload | processing_status |
| --- | --- | --- | --- |
| 探针 1（A3）：dispatch 进行中（AgentAdapter.send 内部查库） | Delivery 之前 | **NULL** | processing |
| 探针 2（F2）：Delivery 进行中（ChannelAdapter.deliver 内部查库） | Delivery 当刻 | **NULL** | processing |
| —— execute 返回后 | 终态事务已提交 | **有值** | succeeded |
| 探针 3（F1）：Delivery **失败**（deliver 内抛错） | 失败后 | **仍 NULL** | succeeded × failed |

**结论（时点）**：

> response_payload 的持久化时点 = **Delivery 成功之后、execute 返回之前的
> 终态事务**（`record_outcome` 一次性落定）。执行全程（dispatch / delivery
> 进行中）不可见；**Delivery 失败 → 该终态事务以异常收场 → response_payload
> 从未落库（NULL）**。

**含义**：response_payload 是「已成功送达的响应」的证据，不是「已产生、
待送达」的响应的证据。Case B（Agent 成功但 Delivery 失败）要获得承载物，
必须让失败路径也能观察到 AgentResponse——那是对失败事实通道的修订
（typed error 携带上下文 / 失败也返回部分结果），属未来裁决，本步不修。

---

## 4. 失败路径弱键：成功路径解决 ≠ 整个问题消失

### 现状区分（不得混淆）

| | 成功路径 | 失败路径 |
| --- | --- | --- |
| 事实来源 | `RuntimeExecutionResult.session_id`（直接） | 无 Result → `_find_session` 弱键（`(identity, conversation)`，省 connection） |
| 状态 | **已消除**（C1） | **仍存在**（C2） |

### 歧义判定：当前 MVP 真实边界下，无实际歧义

推理链（全部有探针背书）：

1. 弱键歧义需要「同 (identity, conversation) 存在 ≥2 条 active session」；
2. 两条 active session 只能落在同 Channel 的两条不同 Connection 上
   （10.6.2：同键唯一 / 换 Connection = 新 Session）；
3. 但 Runtime 的 **Capability Gap**（10.3.1，本步重跑探针 G1）：单 Channel
   多 ACTIVE Connection → `RoutingFailedError`（Fail Closed）——
   **Runtime 永远无法制造歧义状态**；
4. 歧义只能靠**直接仓储写入**人工制造（10.6.2 B5 正是这么做的）——那不是
   Runtime 的真实行为。

**判定：未造成实际歧义 → 记录为开放项，不修。**

**触发条件（何时必须修）**：单 Channel 多 ACTIVE Connection 真实落地时
（Capability Gap 关闭之日），失败路径弱键立即歧义化（10.6.2 B5 的
`MultipleResultsFound` 会真实发生）。解决方向 = 失败路径的事实通道
（typed error 携带 session 上下文），与 G6 同一次边界修订可一并解决。

---

## 5. 验收（探针清单 → 测试）

| # | 探针 | 测试 |
| --- | --- | --- |
| 1 | A：完整成功 → 快照完整 | A1 |
| 2 | A：快照无损（字段面 == AgentResponse，metadata 原样） | A2 |
| 3 | B：两条路径的会话映射 | B1 |
| 4 | B：None 不清空 | B2 |
| 5 | C：成功路径弱键消失（源码 + 运行时） | C1 |
| 6 | C：失败路径弱键仍在（与成功路径区分） | C2 |
| 7 | D：Evidence 由 session_mode 直产（静态 + 运行时） | D1 |
| 8 | E：REUSE 正确 session_id + 零虚假 Evidence | E1 |
| 9 | F：dispatch 中 payload NULL（时点探针 1） | A3 |
| 10 | F：Delivery 中 payload NULL（时点探针 2） | F2 |
| 11 | F：Delivery 失败真实探针 → payload 从未落库 | F1 |
| 12 | 弱键歧义判定：Runtime 无法制造歧义状态（Capability Gap 重跑） | G1 |
| 13 | 开放项成文（成功/失败弱键区分 + 触发条件） | G2 |
| 14 | 关闭矩阵成文 | H1 |
