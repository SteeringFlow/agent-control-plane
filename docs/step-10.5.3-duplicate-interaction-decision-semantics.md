# Step 10.5.3 — Duplicate Interaction Decision Semantics

> **决策语义步，生产代码改动：0 行。**
> 交付物 = 本报告 + 决策矩阵快照测试
> （`tests/test_step10_5_3_duplicate_interaction_decision_semantics.py`）。
>
> **本步不设计 Redis、锁、租约、队列、幂等服务。** 只做一件事：回答
> 「判断出重复之后，Control Plane 应该做什么」——把 Duplicate 判定的
> **决策语义**先定义清楚，不替三个产品语义问题拍板（§6），不做最小
> Runtime 行为编码。

---

## 0. 范围与不做

**做**：

- 把问题从「怎么判断重复」（10.5.2 已解决）推进到「判断之后做什么」；
- 建立 Duplicate Interaction Decision Matrix，明确四种**内部决策结果**；
- 区分两个被混为一谈的问题：**Delivery Retry ≠ Agent Retry**；
- 记录第三个危险状态 PROCESSING 的真实语义（不是锁）；
- 用真实 DB 行为证明：决策矩阵的**输入状态全部可观察、Case A/B 可区分**。

**不做（本步明确排除）**：

- 不新增 DUPLICATE 数据库状态 / 新列 / 新约束 / 迁移；
  DB 仍只有 `processing_status ∈ {pending, processing, succeeded, failed}`
  与 `delivery_status ∈ {pending, delivered, failed}`；
- 不引入 Redis / 分布式锁 / DB row lock / lease / worker / queue / 幂等服务
  （PROCESSING 不是锁，§6）；
- 不动 `PersistingCoordinator`：不做 lookup→decide、不 try/except IntegrityError、
  不加 suppress / retry / replay 分支；
- 不给 Repository 加 create-or-get / upsert / 决策语义方法；
- 不为三个未决问题（in-flight / Case A / Case B，§6）拍板——
  它们是**产品语义**，不是数据库技术；
- 不实现任何 Delivery Retry / Agent Retry 机制。

---

## 1. 问题重定义：怎么判断已解决，判断之后做什么

10.5.2 把身份物化成 `UNIQUE(channel_id, external_interaction_id)` 后，
**重复检测在 DB 层已经解决**：

```
同 Channel + 同 MsgId 的第二次 create
        ↓
PersistingCoordinator._start
        ↓
uow.interactions.create(...)   ← INSERT 与 UNIQUE 冲突
        ↓
IntegrityError（在 inner Runtime 执行之前抛出）
```

因此得到一个先于本步就成立、但值得明确写下的地基事实：

> **今天，任何状态下（终态或 in-flight）的同 MsgId 重复请求，都不会产生第二次副作用**——
> create 先于 execute，重复请求在 create 一步就被 UNIQUE 拦下。

但 **IntegrityError ≠ Suppress 语义**。它缺两层：

1. **语义层**：命中后 CP 该做什么（抑制？重新执行？只重试 Delivery？）
   没有任何决策——本步定义的正是这一层；
2. **响应层**：重复请求今天以错误形态暴露（IntegrityError → 上游 5xx），
   渠道（WeCom）会因此继续重推——真正 Suppress 需要让上游感知「已处理」。
   这属于 10.5.3 之后的「最小 Runtime 行为」，本步不编码。

---

## 2. 决策矩阵（输入 = 行状态，输出 = 内部决策）

> 矩阵按「已存在 Interaction 的持久化行状态」读入。行状态是真实可观察的
> （§4 探针），决策结果是**内部裁决 token，不落库**（§3）。

| 已存在 Interaction | processing_status | delivery_status | retryable | 语义判定 | 第二次请求的决策 |
| --- | --- | --- | --- | --- | --- |
| 否 | — | — | — | 新事件 | **NEW → execute**（已实现，C1） |
| 是 | PENDING | PENDING | — | in-flight 早期 | **DUPLICATE_IN_FLIGHT**（未决） |
| 是 | PROCESSING | PENDING | — | 执行中 / 可能卡死 | **DUPLICATE_IN_FLIGHT**（未决） |
| 是 | SUCCEEDED | DELIVERED | False | 成功终态 | **DUPLICATE_TERMINAL → suppress**（语义已定） |
| 是 | FAILED | PENDING | False | 契约/身份类失败 | **DUPLICATE_TERMINAL → suppress**（语义已定） |
| 是 | FAILED | PENDING | True | **Case A**：Agent 未成功 | **RETRYABLE_FAILURE**（未决） |
| 是 | SUCCEEDED | FAILED | True | **Case B**：Agent 已成功 | **DELIVERY_FAILURE**（未决） |

---

## 3. 四种决策结果（内部 token，非数据库状态）

| token | 含义 | 状态 |
| --- | --- | --- |
| `DUPLICATE_TERMINAL` | 已存在的行处于**不可重试的终态** → 第二次请求是已处理事件的回声，正确动作是 **suppress**（不执行；最终让上游感知「已处理」） | 语义已定，最小 Runtime 行为未编码 |
| `DUPLICATE_IN_FLIGHT` | 已存在的行处于 **PENDING / PROCESSING**（in-flight）→ 无所有权、无租约；第二次请求无法与「执行中的首次请求」安全区分 | **未决**（§6） |
| `RETRYABLE_FAILURE` | 已存在的行 = **Case A**：`FAILED + delivery=PENDING + retryable=True`（Agent 执行未成功）→ 渠道重推可能是给 CP 一次重新执行的机会 | **未决**（§5/§6） |
| `DELIVERY_FAILURE` | 已存在的行 = **Case B**：`SUCCEEDED(processing) + FAILED(delivery) + retryable=True`（Agent 已成功）→ 重跑 Agent 会重复业务副作用；应重试的是 **Delivery** 不是 Agent | **未决**（§5/§6） |

**关键纪律：这些 token 是 CP 内部的裁决结果，不是 interactions 的数据库状态。**
数据库仍然只有 PENDING / PROCESSING / SUCCEEDED / FAILED（精确地说，
processing_status 四值 × delivery_status 三值）。不增加 DUPLICATE 行、不增加
duplicate 列——「重复」由 `(channel_id, external_interaction_id)` 唯一键表达，
不需要在数据里再复制一份「它是重复的」事实。

---

## 4. 输入状态空间：决策矩阵能读到什么

10.4.2 冻结的双状态机决定了**可达的行状态组合**是有限且可枚举的：

| processing_status | delivery_status | retryable | 何时可达 | 探针 |
| --- | --- | --- | --- | --- |
| pending | pending | False | `_start` 事务①（瞬态；进程死于此即卡住） | E2/E3 |
| processing | pending | False | `_start` 事务②（执行中；进程死于此即**卡死阴影**） | E2/E3 |
| succeeded | delivered | False | 成功路径终态 | B1 |
| failed | pending | False | Session 建立前失败（identity/routing/session） | B2 |
| failed | pending | True | **Case A**：dispatch transport 失败 | B3 |
| succeeded | failed | True | **Case B**：delivery 失败 | B4 |

**不可达**（双状态独立、不同时制造假象）：

- `(succeeded, pending)`：终态由 `record_outcome` 一次性同时写入，从不单独提交
  processing=SUCCEEDED；
- `(failed, delivered)`：delivered 只在成功路径出现；
- `(failed, failed)`：delivery 失败恒映射 processing=SUCCEEDED（10.4.2 冻结的
  「Delivery 失败 ≠ Processing 失败」）。

**最值得注意的输入事实：Case A 与 Case B 在行上天然可区分**——差在
processing_status 一个维度：

```
Case A   Agent 从未成功   → processing = failed     → 重跑 Agent 也许可以（副作用未知）
Case B   Agent 已成功     → processing = succeeded  → 重跑 Agent 必然重复副作用（禁止）
```

这是 10.5.3 最值钱的一层语义：**不需要新列，DB 已经回答了「Agent 到底成没成功」。**

---

## 5. Case A vs Case B —— Delivery Retry ≠ Agent Retry

这两个情况都必须单独讨论，绝不能混在一起。

### 情况 A：Agent 执行失败（RETRYABLE_FAILURE）

```
Agent
  ↓  timeout / transport
  ↓
processing = FAILED
retryable  = TRUE
delivery   = PENDING        ← Agent 从未成功，没有东西可投递
```

WeCom 再次发送同一个 MsgId。

> 这很可能意味着：**渠道重试正在给 CP 一次重新执行机会。**

但注意「很可能」三个字：`AgentDispatchFailedError`（transport 类）覆盖的
场景既包括「请求根本没到 Agent」（重跑安全），也包括「Agent 已开始执行业务、
响应在回传路上丢失」（重跑会重复副作用）。**行本身分不出这两种**——
`retryable=True` 只表达「transport/timeout 类，值得重试」，不承诺
「重试无副作用」。

### 情况 B：Agent 已经成功，但 Delivery 失败（DELIVERY_FAILURE）

```
Agent
  ↓  SUCCEEDED（业务副作用已产生）
  ↓
WeCom delivery failed
  ↓
delivery = FAILED
retryable = TRUE           ← 值得重试的只剩投递，不是 Agent
```

这时候如果重新执行 Agent：

```
Agent
  ↓  业务副作用
  ↓  再次执行
  ↓  ... 重复副作用
```

**必然造成业务副作用重复。**

### 结论：两个 Retry 是两个不同的问题

| | Case A | Case B |
| --- | --- | --- |
| processing | FAILED | SUCCEEDED |
| Agent 业务副作用 | **未知**（transport 歧义） | **已发生** |
| 第二次请求暗示 | 渠道给了重新执行的机会？ | 渠道在催「那条回复怎么还没到」 |
| 若重跑 Agent | 可能安全，可能重复（无法由行判定） | 必然重复（禁止） |
| 真正需要重试的 | Agent 执行（待产品裁决） | **Delivery**（不是 Agent） |

因此：

> **Delivery Retry 与 Agent Retry 是两个不同的问题。**
> Case B 的唯一合法重试对象是 Delivery；任何「因为 Delivery 失败所以重跑
> Agent」的代码路径都是语义错误。

### Case B 的今日现实：Delivery Retry 没有承载物

Delivery Retry 需要「把上次那条 Outbound 再投一次」。今天它不存在：

- `response_payload` 全树**零写入方**（10.5.1 §A 缺口，至今未关）——
  成功的 AgentResponse 没有以任何可重放形式持久化；
- `OutboundResponse` 不含 `external_interaction_id`（8.6 冻结）——
  投递出去的消息无法回链来源 Interaction。

即：**今天即使想只重试 Delivery，也没有可重试的东西。** 这是 Case B 真正
需要未来补的能力（出站可重放载体），而不是在本步拍「重跑 Agent」。
重跑 Agent 在任何情况下都不是 Case B 的答案。

---

## 6. PROCESSING 不是锁；三个未决问题

### 第三个危险状态：PROCESSING

10.5.1 G1（10.5.2 前）证明过「Agent 执行中再进同 MsgId → 当前实现会再次执行」。
10.5.2 物化身份后这个行为**变了**：同 MsgId 的第二次 create 在任何时刻都先撞
UNIQUE——包括首次执行仍在 in-flight 时（E2 探针）。因此今天：

- **同 MsgId 的并发/重复请求：DB 身份即拦截，无第二次执行**；
- **不同 MsgId 的并发请求：互不拦截**（两行独立，各自执行）。

但 **PROCESSING 不是锁**：

- interactions 行**没有** owner / lease / claim 列（E1：schema 无归属标记）；
- 一个行停在 PROCESSING 时，**无法区分「真的还在执行」与「进程死了」**；
- 卡死的 PROCESSING 行会变成该 MsgId 的**永久阴影**：此后同 MsgId 的每一次
  到达都冲突（E3），而这个交互永远没有恢复路径（没有租约过期来回收它）。

所以现在**不要**为了处理 in-flight 立刻引入 Redis lock / DB row lock /
distributed lock / lease / worker / queue。这些是真实并发证据出现后、
配合真实部署拓扑再裁决的事。本步只把语义钉住：

> **PROCESSING = 执行中的可观察事实 + 无所有权；卡死阴影的回收需要租约类
> 机制，那是未来裁决，不是本步。**

### 决策树

```
Channel 事件（MsgId）到达 Runtime 调用边界
        ↓
按 (channel_id, external_interaction_id) lookup
        ↓
   ┌────┴──────────────┐
   │                   │
 absent             exists
   │                   │
 execute          inspect 行状态
   │            (processing × delivery × retryable)
   │                   │
   │      ┌────────────┼──────────────┬──────────────┐
   │      │            │              │              │
   │  terminal     in-flight       Case A         Case B
   │  不重试      PENDING/       FAILED+rt       SUCCEEDED+
   │             PROCESSING      delivery=       delivery=
   │                              PENDING          FAILED
   │      suppress          ?            ?              ?
   │       （语义已定）    （未决）     （未决）        （未决）
```

### 三个未决问题（本步不拍板）

| # | 问题 | 需要的输入 | 拍板时机 |
| --- | --- | --- | --- |
| Q1 | `DUPLICATE_IN_FLIGHT`：第二次到达时应做什么？等待 / 拒绝 / 排队？ | 真实并发到达的部署证据 + 渠道超时窗口语义 | 引入锁/租约前 |
| Q2 | `RETRYABLE_FAILURE`（Case A）：同 MsgId 重推 = 重新执行 Agent 吗？ | Agent transport 歧义的业务后果（Agent 侧是否有幂等 / 是否可感知已执行） | 产品语义裁决 |
| Q3 | `DELIVERY_FAILURE`（Case B）：Delivery 重试的承载物是什么？ | 出站响应是否需要持久化（response_payload 写入方 / 可重放载体） | 出站可重放能力裁决 |

把这三个问题留在 10.5.3 之后的语义裁决是**有意为之**——它们已经开始涉及
产品语义（渠道重试的含义、Agent 副作用的可重试性、出站可靠投递的承诺），
不是数据库技术。本步先让真实状态语义出来。

---

## 7. 本步探针钉住的真实行为快照

测试把「当前真实状态」钉成可回归断言（静态快照 + 真实运行时探针）：

| # | 事实 | 探针 |
| --- | --- | --- |
| 1 | 决策 token 是内部语义，不是 DB 状态：DB 仍只有 4+3 个状态值，无 DUPLICATE 值/列 | A1/A2 |
| 2 | 决策 token 不出现在生产代码（只活在本文档） | A2 |
| 3 | 决策矩阵输入状态全部真实可观察；Case A 与 Case B 在行上可区分 | B1~B5 |
| 4 | absent → NEW → execute（1 行 + 1 次副作用） | C1 |
| 5 | 成功终态重复：同 MsgId 再达 → IntegrityError，无第二次副作用（suppress 的效果已被 DB 身份实现） | D1 |
| 6 | 不可重试失败终态重复：同 MsgId 再达 → IntegrityError | D2 |
| 7 | Case A 重复：同 MsgId 再达 → IntegrityError，行保留原失败，**渠道重推今天无法重新执行** | D3 |
| 8 | Case B 重复：同 MsgId 再达 → IntegrityError，行保留 (succeeded, failed)，**Agent 绝不重跑** | D4 |
| 9 | PROCESSING 无所有权列（schema 无 lease/claim） | E1 |
| 10 | in-flight 行存在时同 MsgId 再达 → IntegrityError（无第二次执行）；行停留在 PROCESSING | E2 |
| 11 | 卡死 PROCESSING 无恢复路径：同 MsgId 永远冲突；不同 MsgId 不受影响（阴影是 per-identity 的） | E3 |
| 12 | 决策层无 lock/lease/queue/幂等基础设施偷渡；Runtime/Repository 面零变化 | F1/F2 |
| 13 | Delivery Retry 无承载物：response_payload 零写入方、OutboundResponse 不回链 | F3 |
| 14 | 契约成文 | G1 |

---

## 8. 验收（16 条 → 测试 17 项）

| # | 条件 | 测试 |
| --- | --- | --- |
| 1 | DB 状态不增：processing 四值 + delivery 三值，无 DUPLICATE 值/列 | A1 |
| 2 | 决策 token 只属内部语义、不落库、不出现在生产代码 | A2 |
| 3 | 决策矩阵已按四种结果成文 | G1 |
| 4 | 输入状态空间真实可观察（成功 / 不可重试失败 / Case A / Case B） | B1~B4 |
| 5 | Case A 与 Case B 行上可区分（processing 维度） | B5 |
| 6 | absent → NEW → execute | C1 |
| 7 | 成功终态重复 → 无副作用冲突（suppress 的 DB 层效果） | D1 |
| 8 | 不可重试失败终态重复 → 无副作用冲突 | D2 |
| 9 | Case A 重复今天不重新执行（渠道重推被 UNIQUE 拦截） | D3 |
| 10 | Case B 重复今天不重跑 Agent（行保留） | D4 |
| 11 | PROCESSING 无所有权列（不是锁的 schema 证据） | E1 |
| 12 | in-flight 同 MsgId 再达无第二次执行 | E2 |
| 13 | 卡死 PROCESSING 阴影无恢复路径；per-identity 不全局阻塞 | E3 |
| 14 | 决策层零基础设施偷渡、Port 面零变化 | F1/F2 |
| 15 | Delivery Retry 无承载物（10.5.1 缺口仍开放，为 Case B 的现实） | F3 |
| 16 | 全量回归 | 820+ 全绿 |

---

## 9. 下步边界（10.5.3 之后）

本步**不进入**最小 Runtime 行为编码。后续裁决的两个候选方向（都需先答 §6
三问）：

1. **Suppress 的响应语义**：在 Runtime 调用边界把「命中已存在的 Interaction」
   从 raw IntegrityError 转成对上游「已处理」的显式语义（决定 WeCom 5s
   重推窗口的行为）；
2. **Case A/B 的执行语义**：只有当产品裁决允许「重新执行」/「只重试
   Delivery」时，才在调用边界引入对应的最小动作——它们各自需要真实
   in-flight / 出站可重放证据，而不是凭架构对称性先做。
