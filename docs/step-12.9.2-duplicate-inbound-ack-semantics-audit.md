# Step 12.9.2 — Duplicate Inbound ACK Semantics Audit

> 只读审计（承接 12.9.1 的边界审计与 10.5.3 的决策矩阵）。**不改生产代码、不开新状态、
> 不引入 lease/queue/幂等基础设施。** 只回答一个问题：
> **同 `external_interaction_id` 再次进入 CP 时，CP 应如何根据已有 Interaction 的行状态决定 ACK？**

引用事实面：`app/application/interaction_recording.py`（`_start`/`_record_failure`/
`_record_success`）、`app/persistence/models/interaction.py`（UNIQUE）、
`app/domain/interaction_record.py`（双状态机）、`app/adapters/channels/wecom.py`
（response_url single-use）、`docs/step-10.5.3`（决策树 + Q1/Q2/Q3 + 探针 D1~D4/E1~E3/F3）、
`docs/step-12.9.1`（ACK==执行结束、无 async 边界）。

---

## 1. 前置事实（12.9.1 / 10.5.3 已确立，不重复论证）

1. **执行次数已被 DB 钉死**：`PersistingCoordinator._start` 的 `create` 先于
   `inner.execute`，`UNIQUE(channel_id, external_interaction_id)`
   （`interaction.py:43-47`）保证**每个 MsgId 至多一次 Agent 执行**——任何状态下
   （终态或 in-flight）同 MsgId 第二次到达都在 create 一步撞 `IntegrityError`
   （10.5.3 §1/D1~D4/E2）。
2. **当前 duplicate 的唯一行为 = `IntegrityError` → 冒泡 → HTTP 500**（12.9.1 §B）。
   WeCom 收不到 2xx → 持续重推 → 重推风暴，且风暴期间 CP 不做任何决策。
3. **当前无 async 边界**：ACK==执行结束；无 queue/worker/lease（12.9.1 §E）。
4. **10.5.3 已定（不重开）**：`DUPLICATE_TERMINAL → suppress` 语义已定（但响应层
   未编码）；Q1（in-flight 怎么办）、Q2（Case A 重推=重执行 Agent 吗）、Q3（Case B
   Delivery 重试承载物）**三问未决**。
5. **Case A 与 Case B 在行上天然可分**（10.5.3 §4 最值钱一层）：差在
   `processing_status` 一个维度——`failed` 表示 Agent 从未成功，`succeeded` 表示
   业务副作用已发生。

---

## 2. 实际可达状态空间

双状态机 `processing(4) × delivery(3)` 的 12 种组合中**只有 6 种可达**、3 种不可达
（10.5.3 §4）：

| # | processing | delivery | retryable | 何时可达 | 到达方式 |
| --- | --- | --- | --- | --- | --- |
| S1 | pending | pending | False | `_start` 事务①（瞬态窗口；进程死于①→②则永久卡住） | 进程崩溃阴影 |
| S2 | processing | pending | False | `_start` 事务②（执行中；进程死于此后则**卡死阴影**） | **慢执行热路径**：Agent >5s，WeCom 在 5s 重推时首推仍在执行 |
| S3 | succeeded | delivered | False | 成功终态 | 慢执行但已完工：CP 在 5s 后返回 200，WeCom 已超时重推 |
| S4 | failed | pending | False | Session 建立前失败（identity/routing/session/config） | 快失败：首推 <5s 返回 500（Runtime typed error），行已终态 |
| S5 | failed | pending | **True** | **Case A**：dispatch transport/timeout 失败 | 快失败：Agent 未成功但可重试类 |
| S6 | succeeded | failed | **True** | **Case B**：delivery 失败（message/send 路径） | 快失败：Agent 已成功、投递失败 |
| — | succeeded | failed | **False** | **Case B 的 response_url 变体**（`wecom.py` `_deliver_via_response_url` 恒抛 `retryable=False`） | 智能机器人投递失败（single-use 已消费） |

**不可达**（防未来漂移）：`(succeeded, pending)`、`(failed, delivered)`、
`(failed, failed)`（`record_outcome` 一次性写终态；delivered 只出现在成功路径；
delivery 失败恒映射 processing=SUCCEEDED，10.4.2 冻结）。

**结构性观察（本审计新增）**：真实世界里 duplicate 的形态由首推耗时决定——
- 快成功（<5s）→ **根本没有 duplicate**（WeCom 已收 200）；
- 慢执行（>5s）→ duplicate 到达时首推行多为 **S2 PROCESSING**（执行未完），或
  若已完工则为 **S3 SUCCEEDED**；
- 快失败 → duplicate 落在 **S4 / S5 / S6** 终态行上。

→ **S2/S3（慢世界）与 S4/S5/S6（快失败世界）都是真实热路径；A 行天下不存在
SUCCEEDED duplicate。** 这与 ACP 真实执行 ~16s（10.1.3/10.2.1）直接相关。

---

## 3. 决策表

> 列含义：**第二次 Agent 执行** = 这个 duplicate 是否应触发第二次 `inner.execute`；
> **ACK** = 审计建议 HTTP 应答语义（对 WeCom 的含义是「停止重推」还是「继续等」）；
> **是否需要后续动作** = duplicate 处理后 CP 侧是否还有未尽的恢复责任。
> 「当前行为」列 = 今天代码对每个 duplicate 的唯一事实。

| 已有状态 | 当前行为 | 第二次 Agent 执行 | **ACK（审计建议）** | 是否需要后续动作 | 依据 / 语义 |
| --- | --- | --- | --- | --- | --- |
| **S1 pending×pending**（死窗口阴影） | IntegrityError → 500 | **❌**（无法证明首推已死——lease 缺席时重执行有双跑风险） | **200「已接收」** | 有（未来租约机制区分死/活；10.5.3 E3） | Q1 未决；无 lease 前任何「重执行」都不可证安全。200 止损重推风暴；死阴影回收属未来 |
| **S2 processing×pending**（执行中/卡死阴影） | IntegrityError → 500 | **❌**（UNIQUE 已保证首推是唯一一次执行；活着会自己跑完） | **200「已收到，正在处理」** | 有（同上：租约区分死/活） | Q1 未决。慢世界热路径：首推大概率活着（同一线程仍在跑），200 让其跑完即达 S3 |
| **S3 succeeded×delivered**（成功终态） | IntegrityError → 500 | **❌**（已完成） | **200（suppress）** | **无** | `DUPLICATE_TERMINAL → suppress`（10.5.3 语义已定）。WeCom 的重复=「没等到 200」的回声，工作已完成 |
| **S4 failed×pending retryable=False**（契约/身份类终态） | IntegrityError → 500 | **❌**（根因是配置/身份，重跑确定性再失败） | **200（suppress）** | **无**（失败 Evidence 已留，走运营告警） | `DUPLICATE_TERMINAL → suppress`（10.5.3 矩阵「FAILED+PENDING+False → suppress」） |
| **S5 failed×pending retryable=True（Case A）** | IntegrityError → 500 | **❌ 至 Q2 裁决前**（唯一语义上可能允许「重新执行」的格子，但 transport 歧义=可能重复业务副作用） | **⚠ 产品未决（Q2）**：见下方展开 | 有（若裁决重执行→改执行语义；若不→明示放弃） | 10.5.3 §5A/Q2：`retryable=True` 只表达「值得重试」，不承诺「重试无副作用」 |
| **S6 succeeded×failed retryable=True（Case B, message/send）** | IntegrityError → 500 | **❌❌ 绝对禁止**（业务副作用已发生，重跑必然重复） | **200 + 交付恢复走带外** | 有（Delivery 恢复，见展开） | 10.5.3 §5B 铁律：Case B 唯一合法重试对象是 Delivery，不是 Agent |
| **S6' succeeded×failed retryable=False（Case B, response_url single-use）** | IntegrityError → 500 | **❌❌ 绝对禁止** | **200 + 交付恢复走带外** | 有（仅带外恢复） | 同上 + `wecom.py:585` 官方「每个 response_url 只能调用一次」→ 本交互投递已永久不可重放 |

**三行关键判断与用户问句对齐：**

### Case B（S6/S6'）：duplicate ≠ retry whole interaction，甚至 ≠ retry delivery

- **为什么第二次 Agent 执行恒 ❌**：`processing=succeeded` 已由 DB 回答「Agent 成没
  成功」（10.5.3 §4 最值钱一层）。重跑 Agent = 必然重复业务副作用，任何代码路径都是
  语义错误（10.5.3 §5B）。
- **「简单 200 还是尝试恢复 Delivery」**：理想动作是「只重试 Delivery」，但今天**没有
  可重试的东西**——`response_payload` 零写入方（成功路径虽在 10.8.4 落
  `interaction_recording._record_success` 的快照，但那是**全树快照**非可重放 Outbound；
  且 10.5.3 F3 记录 OutboundResponse 不回链 interaction）、Case B 的
  `response_payload` 仍 NULL（10.8.4 诚实修订）、无 delivery-queue。
- **response_url single-use 的追加结论**：即使将来有了可重放载体，智能机器人这一渠道
  形态也无法「对旧 response_url 重投」——官方 101138 明确每个地址仅一次调用
  （`wecom.py:585`）。**Case B 的恢复责任天然不在 inbound-dup 路径**：
  该条回复的投递机会已永久失去，真正的恢复 = 用户下一条**真实消息**（新 MsgId，
  REUSE 同一 active session，Agent 靠会话上下文补齐）+ 一条新鲜 response_url。
- **ACK 裁决**：200（suppress 形态），**不是** 5xx——5xx 只会让 WeCom 无限重推一个
  永远无法由本路径修复的投递，风暴无价值；200 让行停在 `(succeeded, failed)` 这个
  可观测事实（10.5.3 B4 探针），供运营/下次真实消息恢复。

### Case A（S5）：唯一可能「重执行」的格子，但被两个东西挡住

- **被 UNIQUE 挡住（架构事实）**：create-before-execute 使重推在 create 一步即炸，
  **任何状态下都不存在「第二次执行」的实现路径**（10.5.3 D3：渠道重推今天无法重新
  执行）。所以 S5 的「❌」今天不是裁决，是事实。
- **被 Q2 挡住（语义事实）**：即便拆掉 UNIQUE 的先决顺序，重执行是否安全取决于
  `AgentDispatchFailedError` 覆盖的歧义——请求没到 Agent（重跑安全）vs 已开始执行业务、
  响应回传丢失（重跑重复副作用）。**行本身分不出**（10.5.3 §5A）。解锁条件 = Agent
  侧幂等 / 可感知已执行的**产品语义裁决**。
- **ACK 因此是开放的**：裁决前若回 200 = 明示放弃这条消息（可能丢掉一次本来能成功的
  恢复机会）；若回 5xx = WeCom 无限重推但 CP 永不重执行（风暴无进展）。**两者都不
  好——这正是 Q2 必须产品拍板的原因**，不是本审计能代答的。

### In-flight（S1/S2）：200 是最不坏的选择，且与「一次执行」自洽

由于 UNIQUE 已保证**每个 MsgId 至多一次执行**，duplicate 的 ACK **不可能改变执行
次数**——它只表达「如何让渠道停止重推」。慢世界热路径（S2）的首推大概率还活着
（同请求线程仍在跑），200 让 WeCom 停止、首推自然跑完到 S3；唯一的残余风险是「进程
死后 PENDING/PROCESSING 阴影被 200 静默 ack、消息永无回复」——这正是 10.5.3 Q1/
E3 留白给未来租约机制的部分，本审计不引入 lease。

---

## 4. 审计结论

1. **重复入站 ≠ 重试整个 interaction**，这与状态无关，是铁律：**重试对象的裁决只看行
   状态**——
   `succeeded` → 绝不重跑 Agent（最多 Delivery）；`failed+retryable=False` →
   suppress；`failed+retryable=True` → 产品裁决（Q2）；`in-flight` → 无 lease 不可裁决
   （Q1）。**没有一行支持「简单重跑整个 interaction」。**
2. **ACK 的目标语义是「按行状态让渠道停止/继续重推」，而不是今天的「一律 500」**：
   6 个可达态里 4 个（S3/S4/S6/S6'）语义已确定应回 200（suppress 或带外恢复），
   2 个（S1/S2 in-flight）在无 lease 前提下 200 是最不坏选择，1 个（S5 Case A）ACK
   依赖 Q2 产品裁决。**当前把所有 duplicate 一律暴露为 500 = 重推风暴，且风暴期间
   CP 零决策——既无益于恢复、又制造无意义负载。**
3. **代码面含义（不实现，仅记录）**：要让 ACK 按状态分化，duplicate 检测必须从
   「create 先于 execute、命中即炸」（`interaction_recording.py:152`）前移到
   「**先 lookup 已有行 → 按状态决策 → 再决定 execute/200/500**」（10.5.3 §9 候选
   方向 1 的形状）。这是未来最小 Runtime 行为的前置条件；本步不动。
4. **Case B 的恢复责任明确不在 inbound-dup 路径**（response_url single-use +
   response_payload 无可重放载体 + OutboundResponse 不回链），其恢复走带外：
   下一条真实消息（会话续接）或运营侧。**因此 inbound-dup 对 Case B 的 ACK 恒为
   200/suppress 形态，不存在「借重推重投」的可能。**
5. **移交品**：S5（Case A）的 ACK/重执行语义移交 Q2 产品裁决；S1/S2 的 200 决策在
   引入租约机制时复审（Q1）。本审计不闭合任何 10.5.3 未决项。
