# Step 10.9 — Post-10.8 Architecture / Step Selection Audit

> **审计步，生产代码改动：0 行。**
> 交付物 = 本报告 + 审计测试
> （`tests/test_step10_9_post_10_8_step_selection_audit.py`）。
>
> 目标：盘点 10.8 收口后仍然存在的真实缺口，判断哪些 **NOW**、哪些
> **DEFER**、哪些 **WATCH**、哪些 **DROP**——而不是把 G5~G10 当成待办清单。
> 它们只是已知观察结果；本步判断它们**有没有资格变成工程任务**。

---

## 0. 事实基线（不再重审）

10.8 已关闭并冻结的四项（10.8.5 关闭矩阵 G1~G4），本步只做**回归确认**、
不重新设计：

- G1 response_payload 成功路径写入（原样快照）；
- G2 external_session_id 回流（bind_external_session）；
- G3 成功路径 session 弱键推导删除（result.session_id 直取）；
- G4 CREATE_NEW Evidence 由 session_mode 直产。

---

## 1. 裁决标准：每个候选回答五个问题

| 问题 | 判别要点 |
| --- | --- |
| **Q1 真实存在吗？** | 有真实运行路径 / 真实测试 / 真实数据 / 真实调用方证明问题存在？（Architecture Gap ≠ Bug；Future Capability Gap ≠ MVP Requirement） |
| **Q2 已经造成错误了吗？** | 区分「缺口存在」与「已造成错误」——存在不等于错误 |
| **Q3 现在不解决会阻塞什么？** | 「不解决 → 某个**当前** MVP 能力无法实现」才算高优先级；「将来可能需要」不能自动升级成当前任务 |
| **Q4 有没有更小的解决方式？** | 若方案需要 3 个新表 + Queue + RetryService + Worker，继续追问更窄的解 |
| **Q5 是否应该现在做？** | 结论只能是 **NOW / DEFER / WATCH / DROP**，而不是 TODO |

---

## 2. 逐候选五问裁决

### G5 中间段 Evidence（identity / routing 无直接 Evidence）→ **DROP**

- Q1：部分真实（成功段不可见是事实）；
- Q2：未造成错误——诊断由 `failure_stage` 反推足够（失败即 STOP，反推无损）；
- Q3：不解决不阻塞任何当前能力；补齐的唯一收益是「视觉上的完整链路」；
- Q4：更小方案 = 不补（补 `identity_resolved` / `routing_completed` 就是
  10.7.1 明确避免的 **tracing 化**）；
- Q5：**DROP**——没有真实用户/管理员问题要求「Identity Resolution 的成功
  证据」，这就是正确的 MVP 状态。

### G6 Case B response_payload（Delivery 失败时未持久化）→ **DEFER**

- Q1：真实存在（10.8.5 F1 真实失败探针：succeeded × failed 且 payload NULL）；
- Q2：未造成错误——这是 10.8.3 冻结的失败模型（异常通道）的**诚实后果**，
  10.7.2 B2 矩阵已修订在案；
- Q3：不解决只阻塞一件**尚不存在**的事：Case B 的 Delivery Retry。
  MVP 当前没有任何 Retry 需求；
- Q4：有——失败路径事实通道（typed error 携带 session/response 上下文），
  一次边界修订即可，且与 G8 触发时**可合并为同一次修订**；
- Q5：**DEFER**——「Delivery 失败时是否必须持久化 AgentResponse」的答案：
  **MVP 没有提出这个要求**；没有 Retry 需求，就不为 Retry 建持久化语义。

### G7 Retry carrier → **DEFER**

- Q1：**否**——当前没有任何调用方要求 Delivery Retry（重跑 10.7.2 探针：
  无 retry/replay 入口、无需求记录）；
- Q2：未造成错误（有承载数据 ≠ 实现 Retry，10.8.4 已锁死）；
- Q3：不解决不阻塞任何当前能力——「将来可能无法做 Retry」不能自动升级成
  当前任务；
- Q4：触发时自然获得（G6 的失败路径事实通道 = carrier 的来源）；
- Q5：**DEFER**——与 G6 绑定：Retry 需求真实出现之日一起回来。

### G8 失败路径弱键 → **WATCH**

- Q1：真实存在（弱键查询仍在 `_record_failure`，源码级）；
- Q2：**未造成错误**——10.8.5 G1 探针重证：Capability Gap 使
  「同 (identity, conversation) 两条 active session」的歧义状态在 Runtime
  行为中不可达；歧义只能人工仓储写入制造；
- Q3：不解决不阻塞任何当前能力；阻塞点在**未来**（多 ACTIVE Connection
  合法化之日，弱键立即歧义化——10.6.2 B5 已证明该行为）；
- Q4：有——typed error 携带 session 上下文（与 G6 同一次边界修订）；
- Q5：**WATCH**——触发条件已明确记录：**多 ACTIVE Connection 成为合法
  运行场景之时**。OPEN ≠ NOW。

### G9 interaction 冗余列（identity/connection/agent）→ **DROP**

- Q1：**否**——「冗余」根本不存在：当前零冗余；存在的是「前段失败的请求
  无法定位 Agent/Connection」这一有条件可答状态；
- Q2：未造成错误——前段失败时 Agent/Connection **确实没有参与**，
  `failure_stage` 才是准确答案；「有条件可答」是诚实语义而非缺陷；
- Q3：不解决不阻塞任何当前能力（无任何消费者要求前段失败的 Agent 归因）；
- Q4：若真要做也是「加列+回填」的 schema 演进——比不解决更重；
- Q5：**DROP**——没有明确的 schema 冗余需求，只有「看起来可能冗余」；
  MVP 不需要。

### G10 Read Repository → **WATCH**

- Q1：真实存在（`find_by_session` / `find_by_interaction` 在 Port 上不存在，
  事实探针：全树零命中）；**但**真实消费者同样不存在：
  - frontend 仅占位（无任何 UI）；
  - Management API 未建立（仅路线图条目）；
  - 无运维/排查工具消费证据表；
- Q2：未造成错误——10.7.2 已把最小读契约**定义在案**（恰好两个读方法），
  随时可落地；
- Q3：不解决不阻塞任何当前能力——当前没有任何页面/端点需要
  Session Detail → Interactions → Activities；
- Q4：已有最小方案（10.7.2 A2：恰两个读方法，扩既有 Port，不做
  list_sessions / filter_by_status / pagination / search 的提前建设）；
- Q5：**WATCH**——**触发条件：证据消费场景真实出现**（Management API 或
  Session Detail 类 UI 立项）。届时 G10 从 OPEN 升级 NOW，且实现被
  10.7.2 契约预先限定为最小面。

---

## 3. 最终账本

```
Candidate
   │
   ├── NOW    → （无——0 项）
   ├── DEFER  → G6、G7     （Retry 需求真实出现时回来；G6 与 G8 触发时
   │                          合并为同一次失败路径事实通道修订）
   ├── WATCH  → G8、G10    （触发条件已明确记录在案）
   └── DROP   → G5、G9     （MVP 不需要；补齐反而引入被明确避免的 tracing 化
                              / 投机性 schema 演进）
```

**NOW = 0 项**：10.8 收口后，没有任何缺口满足「当前真实消费者被阻塞」
（Q3 全部为否）。下一正式 Step **不由缺口清单驱动**，应由产品需求
（Management API / Session Detail 类 UI / 新渠道接入）驱动——届时 G10
是最可能第一个升级 NOW 的候选（其最小实现契约已预先冻结在 10.7.2）。

---

## 4. 与既往裁决的一致性

- 「不做 Observability Platform」（10.7.1）→ G5/G9 的 DROP 是它的延续；
- 「Idempotency ≠ Retry」「有数据 ≠ 实现 Retry」（10.5.3 / 10.8.4）→
  G6/G7 的 DEFER 是它的延续；
- 「OPEN ≠ NOW」「先让真实状态语义出来」（10.5.3 / 10.8.1 §4）→
  G8 的 WATCH 是它的延续；
- 「无查询需求不扩方法面」（10.4.2 / 10.7.2）→ G10 的 WATCH 是它的延续。

---

## 5. 验收（→ 测试 7 项）

| # | 条件 | 测试 |
| --- | --- | --- |
| 1 | 基线回归：G1~G4 关闭状态不回退（response_payload / bind / 直取 / mode Evidence，源码 + 运行时） | A1 |
| 2 | 能力断言：Capability Gap 仍在（双 ACTIVE → Fail Closed）——G8 判定的前提 | B1 |
| 3 | 能力断言：无 Retry/幂等/lease/lock/outbox 符号 + Port 无 read 方法/分页（G7「不实现」与 G10「未实现」的事实面） | B2 |
| 4 | 缺口存在性：G6 Case B payload NULL（真实失败探针） | C1 |
| 5 | 缺口存在性：G8 失败路径弱键存在 + 成功路径无弱键 + 失败回填真实发生（dispatch 后失败） | C2 |
| 6 | 缺口存在性：G9 零冗余（无 identity/connection/agent 列） | C3 |
| 7 | 分类断言：账本 G5 DROP / G6 DEFER / G7 DEFER / G8 WATCH / G9 DROP / G10 WATCH；NOW = 0；五问标准成文 | D1 |

全量回归：943 + 7 = **950 passed**。
