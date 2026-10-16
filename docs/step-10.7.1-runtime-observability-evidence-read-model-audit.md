# Step 10.7.1 — Runtime Observability / Evidence Read Model Audit

> **审计步，生产代码改动：0 行。**
> 交付物 = 本报告 + 审计快照测试
> （`tests/test_step10_7_1_runtime_observability_evidence_audit.py`）。
>
> **不是做 Observability Platform。** Prometheus / Grafana /
> OpenTelemetry / Elasticsearch / distributed tracing 一律不要。
> 只回答一个问题：
>
> **现有 Interaction + Activity 数据，是否已经足够让管理员回答
> 「一次请求发生了什么」？**

---

## 0. 范围与不做

**做**：用真实数据 / 真实探针回答 10 个审计问题；给用户给出的 7 跳实例做
逐跳走查。

**不做（本步明确排除）**：

- 不写任何生产代码：不加 read repository 方法、不加 list/分页、不加
  Read Model / Projection / CQRS、不加 tracing ID、不加 API 端点；
- 不引入 Prometheus / Grafana / OpenTelemetry / Elasticsearch / 任何
  可观测基础设施；
- 不改 Interaction / Activity schema；
- 不为「证据读端点归哪个 API 面」拍板（§Q6 记录张力）。

---

## 1. 实例走查：管理员的 10 个问题 vs 现有数据

用户给出的场景（也是真实可复现的探针场景）：

```
09:30:01  WeCom inbound
      ↓   Identity resolved
      ↓   Connection selected
      ↓   Session reused
      ↓   Agent dispatched
      ↓   Agent succeeded
      ↓   WeCom delivery failed
```

### 1.1 逐问映射（管理员问题 → 数据来源）

| 管理员问题 | 数据来源 | 可答？ |
| --- | --- | --- |
| Who? | `interactions.request_payload.external_identity_id` →（经 session）`identities → users` | ✅（payload 直达外部身份；内部 User 需 session join） |
| Which Channel? | `interactions.channel_id`（10.5.2 起一等 FK 列） | ✅ 直达 |
| Which Identity? | `request_payload.external_identity_id`（外部）；`sessions.identity_id`（内部） | ✅（内部 Identity 需 session 存在） |
| Which Conversation? | `request_payload.external_conversation_id`；`sessions.external_conversation_id` | ✅ |
| Which Connection? | **只能经 `sessions.connection_id`**（interaction 行无 connection 列） | ⚠️ 有条件：session 存在才可达 |
| Which Agent? | **只能经 `sessions.agent_id`**（同上） | ⚠️ 有条件：同上 |
| Which Session? | `interactions.session_id`（FK） | ✅（Session 建立前失败为 NULL——本身就是事实） |
| Which Interaction? | `interactions.id` + 身份 `(channel_id, external_interaction_id)` | ✅（10.5.2 物化的身份） |
| What happened? | `processing_status × delivery_status`（双状态独立）+ `request_payload` 快照 | ✅ |
| Where failed? | `failure_stage / failure_code / retryable`（typed 三元组） | ✅（成功时为 NULL = 没有失败点） |
| Was processing successful? | `processing_status` | ✅ |
| Was delivery successful? | `delivery_status` | ✅（本例：`succeeded × failed`——「Agent 成功但 Delivery 失败」精确表达） |

### 1.2 本例的落库事实（探针复现）

对该请求，管理员在两张表里能看到：

```
interactions（1 行）
  channel_id            → Which Channel
  session_id            → Which Session（REUSE 的那条）
  external_interaction_id + (channel_id) → Which Interaction（身份）
  request_payload       → Who / Which Conversation / 原始内容
  processing_status = succeeded     ← Agent 确实成功
  delivery_status   = failed        ← WeCom 投递失败
  failure_stage     = delivery
  failure_code      = CHANNEL_DELIVERY_FAILED
  retryable         = True
  created_at / updated_at           ← 09:30:01 起止时间

activities（该 interaction 名下）
  interaction_received   stage=NULL   status=recorded   created_at=…
  stage_failure          stage=delivery  status=failed
                         message=str(exc)
                         metadata={failure_code, retryable}
  （没有 interaction_completed —— 失败路径不写完成 Evidence）
```

**结论：这 12 个问题，10 个可无条件回答；2 个（Which Connection / Which
Agent）有条件**——必须经 `sessions` join，且要求该请求已建立 Session。
Session 建立之前的失败（identity / routing）与 in-flight 请求的行
`session_id IS NULL`，此时 Connection / Agent / 内部 Identity **不可达**
（数据里就没有）。

### 1.3 诚实缺口清单（数据面）

| 缺口 | 影响 | 出处 |
| --- | --- | --- |
| interaction 行无 identity/connection/agent 冗余列 | 前段失败 / in-flight 的请求无法定位 Agent/Connection（只能答到 channel + 失败 stage） | 4.2 schema 冻结 |
| `response_payload` 全树零写入方 | 「Agent succeeded」只可从 status 读出；**Agent 回了什么内容不可见** | 10.5.1 §A 记录至今 |
| 成功路径中间段零 Evidence | 「Identity resolved / Connection selected / Agent dispatched」这些中间跳不可见（§Q2） | 10.4.2 已知边界 |

---

## Q1. Interaction 当前能不能完整定位一次请求？

**能——带两个条件。** 一行 interaction 携带：渠道（一等 FK）、交互身份
（(channel, external_interaction_id)，10.5.2 物化）、原始请求快照、双状态、
失败三元组、session FK、时间戳。「一次请求发生了什么」的问题面（What /
Where / 成功性 / Who / Conversation）全部在行内或 payload 内。

条件：**Connection / Agent / 内部 Identity 不在行上**，只能经
`session_id → sessions` join 获得。该 join 在 Session 建立前失败的请求上
断裂——但注意这不是数据 bug：那些请求**确实还没有** Session / Connection /
Agent（失败就发生在建立之前），行内的 `failure_stage=identity_resolution`
本身就是最准确的答案。

## Q2. Activity 是否足够表达 execution path？

**不足以表达「完整执行路径」，足以表达「重大事实」。**

今天的 Evidence 类型面（4 种，append-only）：

| type | 何时写 | 对应实例中的哪一跳 |
| --- | --- | --- |
| `interaction_received` | 每次执行起点 | `WeCom inbound` ✅ |
| `session_created` | 仅 CREATE_NEW | `Session reused` → **REUSE 零 Evidence（不可见）** |
| `stage_failure` | 六段任一失败 | `WeCom delivery failed` ✅ |
| `interaction_completed` | 仅成功路径 | 成功终点 ✅ |

- 实例中的 `Identity resolved / Connection selected / Agent dispatched /
  Agent succeeded` **在 Activity 里不存在**——成功路径的中间段（identity /
  routing / session_resolution / agent_dispatch）没有任何 Evidence；
- 这是 10.4.2 的冻结边界（wrapper 观察不到段间边界：Coordinator 只有
  `execute() -> None`，9.1 不引入 RuntimeResult），不是遗漏；
- 失败路径的「执行到哪」由 `failure_stage` 精确回答（到 delivery 才失败 =
  前面各段必然成功）——**失败场景的路径可反推，成功场景的路径不可见**。

补齐路径 Evidence 需要 stage 级回调或 RuntimeResult——那是对 9.1 冻结边界
的修订，属未来裁决；本步不改。

## Q3. Session → Interaction → Activity 查询链是否完整？

**数据层完整，读取层空白。**

- 数据层（FK 已冻结）：
  `interactions.session_id → sessions.id`、
  `activities.interaction_id → interactions.id`、
  `activities.session_id → sessions.id`；sessions 携带
  identity/channel/connection/agent。任何一次执行的全部答案都在
  最多 3 次 join 内可达（探针 C3 用裸 join 复现）。
- 读取层（Port）：**零支持**——
  - Interaction port 只有 `create / mark_processing / record_outcome /
    get_by_id / find_by_channel_and_external_id`，无 list / find_by_session；
  - Activity port **只有 `append`**，没有任何读方法。

即：查询链在数据上成立、在代码上不存在。

## Q4. 是否缺少必要 read repository？

**是——缺口真实存在，但不是现在的生产需求。** 当前没有任何消费者按
session / channel / 时间范围查证据（管理员 UI / 排查 API 都还不存在）。
按项目纪律（「无查询需求不扩方法面」，与 Interaction Repository 不做 list
同一裁决），本步**不加**；触发条件记录在 §Q10 之后。

## Q5. 是否需要专门的 Read Model？

**不需要。** 两张表 + FK join 已能回答全部 10 问；没有查询压力、没有聚合
需求、没有跨源合并（不存在第二个数据源）。现在引入 Read Model / Projection
/ CQRS 属于无证据的设施偷渡——与「不做 Observability Platform」同一条纪律。
触发条件：出现真实的多维查询用例（如「某 Channel 最近 N 小时 delivery 失败
列表」）且裸 join 语义不够时，先在 Repository 层加最小读路径，仍不够再谈
Read Model。

## Q6. Management API 与 Runtime API 的边界？

现状（探针 D2 复现）：`api/` 只有 health 与 runtime webhook（
`/api/channels/{id}/wecom/webhook`）；Management API 尚未建立；证据读端点
不存在于任何面。

记录的边界张力（不拍板）：

- Evidence 是 **Runtime 数据面的产物**，但读它的是**管理员（Management 面
  的用户）**；
- 无论将来端点落在哪个面，读路径都不经过 Runtime Coordinator（读不改执行，
  与 PersistingCoordinator 的写边界对称）；
- 候选形态：Management 面新增只读 Evidence 端点（依赖 read repository 缺口
  先关闭）。留待真实管理场景裁决。

## Q7. 是否需要分页/排序？

**当前无 list 即无分页；排序天然存在。** activities append-only +
`created_at`（无 updated_at）使时间线按 id / created_at 单调可排（探针 B5）。
未来证据 list 端点出现时按需设计（时间游标 / limit），本步不设计。

## Q8. 是否需要真正的 Audit 概念？

**不需要。** 10.4.1 已裁决并沿用：Activity = **append-only Evidence**
（事实记录），不是 Audit Platform（无可抵赖链 / 合规 / 防篡改需求证据）。
append-only + 无 update/delete + status 显式声明，已经给出当前需要的全部
「审计属性」。升级为真 Audit 概念需要新的需求证据，本步不预设。

## Q9. 是否需要 tracing ID？

**不需要新 ID——身份已经存在。**

- `(channel_id, external_interaction_id)` 是渠道请求的天然 trace key
  （10.5.2 物化的 Interaction Identity）；
- `interactions.id` 是 CP 内部关联键；`activities.interaction_id` 是
  Evidence 关联键；
- 再发明 `trace_id` = 对同一事实的第二次表达（与「agent_id 不是 Session
  的第二个选择键」（10.6.2）同一纪律：不重复表达）。

真实的相关缺口（已记录、未来触发）：`external_session_id` 不在 Runtime
主链路回流（10.3.1 §4）——CP 与 Agent 侧**跨系统**无法串联。那是将来真正
需要 tracing 概念时的第一个用例，不是现在。

## Q10. 是否只是现有数据已经够用？

**是——数据面基本够用，读取面为空白，基础设施一律不需要。**

- 12 个管理员问题：10 个无条件可答、2 个经 session join 可答（有条件的
  请求本身就是「还没有 Session」这一事实的准确表达）；
- 数据面缺口（§1.3 三项）都有明确出处与触发条件，均属未来演进；
- 本步结论：**现有 Interaction + Activity 数据足够让管理员回答一次请求
  发生了什么**；缺的只是将来的读路径（read repository / API），而那需要
  真实消费者场景先出现。

---

## 验收（探针钉住的事实）

| # | 条件 | 测试 |
| --- | --- | --- |
| 1 | interaction 行携带 channel/session FK + 身份 + payload 快照（Who/Channel/Conversation 直达） | A1 |
| 2 | interaction 无 identity/connection/agent/user 列；定位经 sessions join | A2 |
| 3 | response_payload 零写入方（Agent 回复内容不可见——记录缺口） | A3 |
| 4 | 双状态 + 失败三元组回答 What/Where/两段成功性（成功、delivery 失败、identity 失败三态） | A4 |
| 5 | Evidence 类型面冻结 4 种 | B1 |
| 6 | 成功路径中间段零 Evidence；REUSE 无 session_created（「Session reused」不可见） | B2 |
| 7 | 失败路径 = received + stage_failure，无 completed | B3 |
| 8 | Activity append-only：port 只有 append；表无 updated_at | B4 |
| 9 | 时间线可排序（created_at 单调） | B5 |
| 10 | 数据链完整（三条 FK） | C1 |
| 11 | 读取层空白（interaction 无 list、activity 无读方法） | C2 |
| 12 | 裸 join 可达全部答案（数据面够用的实证） | C3 |
| 13 | 无 Read Model / Projection / tracing 符号偷渡 | D1 |
| 14 | API 面无证据读端点（health + webhook 之外无 route） | D2 |
| 15 | 契约成文 | E1 |
