# ADR-11 — Conversation Grant Authorization（授权状态持久化与其运行时消费）

> 状态：**FROZEN**
> 性质：**文档补签（retroactive record）** —— 本文件**不设计任何新架构**。
> 它是 Package 2-C §1 已冻结内容 + 已上线实现的**忠实落盘**，使「新增编号 + 显式取代关系」
> 这条取代台账不再存在空洞（`authorization-and-access-model.md:702-703`）。
>
> 上游：`docs/architecture/authorization-and-access-model.md`（ADR-1 ~ ADR-10，FROZEN）
> 　　　`docs/architecture/implementation-package-2-c-authorization-contract-and-boundary-freeze.md`
> 　　　`docs/architecture/implementation-package-2-b-authorization-persistence-and-rollout-design.md`
> 　　　`docs/architecture/implementation-package-2-step-1/2/3/4/5-*`（实现记录）
>
> **与 ADR-12 的关系（性质不同，不可混读）**：
> ```
> ADR-11 = 既有授权架构（connection_conversations Grant）的文档补签 —— 记录已经成立的事实
> ADR-12 = First Message Bootstrap Arm 的新增正式架构决策 —— 授予一个新的授权来源
> ```

---

## 0. 为什么现在才补签（缺口登记，必须留痕）

```
事实 1  Package 2-C §1「D-1 — ADR-11 签发内容冻结」已把 ADR-11 的**全部内容**冻结；
        §9 NOT STARTED 明写「✗ ADR-11 文件本身（本 Gate 只冻结其内容）」；
        §8 收尾表记「ADR-11 待签发 ⏳ 唯一剩余的前置」。
事实 2  Package 2 的实现（migration 0007 / ORM / Runtime Authorization 阶段 / 管理面端点）
        已全量落地并运行；即：**架构实际上依赖一个从未被创建的文件**。
事实 3  CP-1 §2.1 复查确认：`docs/architecture/` 下**无 ADR-11 文件**。
后果    S1 ~ S4 四条取代事实（见 §2）在台账上**无处可查** —— 取代链断裂、不可审计。
处置    本文件按 Package 2-C §1 冻结内容**原文补签**，不增不减。
```

**本补签不改变任何实现、任何测试、任何迁移。** 若本文与运行代码冲突，**以运行代码与
Package 2-C 为准并回到 Gate 裁决**（本文是记录，不是权威来源）。

---

## 1. 编号与槽位

```
ADR-11 被 Package 2-C §1 冻结为「authorization persistence」专用槽位。
ADR-12 不得占用该编号（CP-1 §11.4 D-2 已明确）。
```

---

## 2. 显式取代关系（Package 2-C §1.2 原文冻结）

| # | 被取代物 | 原文本 | 取代理由 |
|---|---|---|---|
| **S1** | Gate 2 §3.2 的 `Permit` = 全量（恒 ALLOW） | 「不查任何 deny/grant 记录 → 判定恒 ALLOW」 | 被明确禁止的 placeholder；且使 `UNAUTHORIZED` 永不可达 |
| **S2** | Gate 2 §6 的 0-table assumption | 「0 new tables / 0 new columns / NO migration ✅」 | 该结论成立于「接受代价 #2（DENY 不可达）」；前提已被否 → 依其 §6.2 代价 #3 的升级触发条件，升级为 Persistence = YES |
| **S3** | Gate 2 §7 第 8 项「Routing 零改动」 | 「Routing … 全部不变 / 锁定测试零更新」 | 唯一性语义与 `agent_id` 派生**保持不变**；但候选集必须从 `active(channel)` 收窄为 `active(channel) ∩ granted(conversation)`（ADR-8） |
| **S4** | ADR §八「任何 schema 变更（C2 已裁决：不需要）」 | 禁止 schema 变更 | ADR C2 原文自带出口：「**若实现步发现确有……需求，须单独裁决**」；ADR-11 走该出口 |

**顺带记录**：Package 1（`0006`，`identities.user_id` 放宽为可空）已走过同一出口
（Package 2-C §1.3）—— ADR-11 是**第二次**，不是例外之外的特例。

---

## 3. 对 ADR-6：补充，而非取代（Package 2-C §1.4）

```
ADR-6 的 default-deny 语义完整保留；
其「缺授权记录 = 不允许」所**预设但未言明**的持久化前提，由 ADR-11 补齐。

即：被取代的是 Gate 2 对 ADR-6 的**实现承诺**（恒 ALLOW），
    不是 ADR-6 本体语义。
```

ADR-6 在本设计中**是依据而非障碍**：

| ADR-6 条款 | 状态 |
|---|---|
| 「授权默认 = deny（缺授权记录 = 不允许）」 | ✅ 正是本设计实现（`granted == ∅ → DENY`） |
| 「允许必须显式成立」 | ✅ 由 grant 行提供 |
| 「Discovery 可以发生…但不产生授权状态」 | ✅ known-conversations 严格遵循（纯读投影） |
| ❌ 不得「首次收到消息即写入授权」 | ✅ 继续有效（回填是**一次性迁移**，不是运行时路径） |
| ❌ 不得把「Routing 选中」当授权结论 | ✅ 继续有效（授权在 Routing 之前独立成阶段） |

---

## 4. 窄范围 persistence exception（Package 2-C §1.5 原文）

```
APPROVED（窄范围且唯一）：
    connection_conversations

它不是 ACL / 权限表（无 effect / 优先级 / principal；作用域 = conversation；deny = 缺行）。
它是 V0 唯一的授权状态存储（此唯一性应由新增守卫机器化）。
```

---

## 5. 本 ADR 所记录的已运行架构

### 5.1 存储面

```
表       connection_conversations（V0 唯一的授权状态存储）
主键     id
作用域   channel_id + external_conversation_id      （ADR-3 授权作用域键）
目标     connection_id                              （ADR-2 Access Path）
归属     tenant_id（**自有列**，RLS 谓词列；绝不从 Connection 推导）

约束
  UNIQUE(tenant_id, channel_id, external_conversation_id, connection_id)   四元组唯一（幂等）
  复合 FK (tenant_id, channel_id, connection_id) → connections(tenant_id, channel_id, id)
     ↑ 一个约束同时消灭「跨租户」与「同租户跨 channel」两种错配（后者 RLS 盖不住）
  connections 需 supporting UNIQUE(tenant_id, channel_id, id)（纯结构性）
  FK 删除语义 = NO ACTION（阻断式；无 cascade）

明确不存在的列（红线）
  ❌ status / effect / priority / expires_at / conditions
  ❌ identity_id / user_id / agent_id / decision
证据：app/persistence/models/connection_conversation.py:36-39、:59-70
```

### 5.2 运行时判定面

```
组件     AccessAuthorizationExecutor（app/application/access_authorization.py）
位置     Identity Resolution 之后、Routing 之前、Session Resolution 之前（ADR-7 / ADR-8）
输入     恰好两项：(channel_id, external_conversation_id)
输出     frozenset[int]（许可集合；顺序无意义）
判定     granted == ∅ ⇔ DENY（deny 由缺行表达）    granted ≠ ∅ ⇔ ALLOW
纪律     不消费 Identity / User / content；不查 connections.status；**不创建任何东西**
证据     access_authorization.py:42-70；identity_resolution.py:28-32
```

### 5.3 路由收窄

```
Routing 只消费「已授权候选」：active(channel) ∩ granted(conversation)
eligible 数量 != 1（0 条或 >1 条）→ ROUTING_FAILED（Fail Closed，不猜、不 fallback）
证据     app/application/routing.py:64-92
```

### 5.4 失败语义

```
AccessAuthorizationDeniedError → (stage=authorization, code=UNAUTHORIZED, retryable=False)
Interaction 仍被记录；不创建 Session / 不调用 Agent / 不产生 Delivery（ADR-7）
证据     app/application/interaction_recording.py:104-118（STAGE_FAILURE_MAP）
        app/application/runtime.py:78-86（RUNTIME_EXECUTION_ORDER 含 authorization）
```

### 5.5 管理面

```
POST   /api/connection-conversations              201 / 409 / 422
GET    /api/connection-conversations?connection_id 200 / 404
DELETE /api/connection-conversations/{id}          204 / 404
GET    /api/channels/{channel_id}/known-conversations  （只读投影；Discovery ≠ Authorization）
❌ 无 Update（行不可变：改授权 = 删 + 建）
证据     app/api/management/connection_conversations.py:89-144
        app/application/connection_conversations.py:55-154
```

### 5.6 迁移与隔离

```
migration 0007：connections 加性 UNIQUE → CREATE TABLE → 索引 → PG RLS（NULLIF 形态）
                → 历史 Session 回填（按租户循环 + 每租户自检）
OSS SQLite 不跑 Alembic，schema 由 Base.metadata.create_all 承载（两条路径不分叉）
证据     alembic/versions/0007_connection_conversations_and_backfill.py:131-199
```

---

## 6. 明确不做（红线）

```
❌ 第二张授权状态表 / 策略表
❌ ACL 化（effect / priority / principal / 继承 / 条件组合）
❌ 把授权作用域下沉到 Identity / User
❌ 新建 Conversation 实体（Conversation 只是 channel-side 会话键）
❌ 从 known-conversations 自动创建 Grant（Discovery ≠ Authorization）
❌ 运行时链路创建授权行（唯一创建点 = 管理面 Service）
```

---

## 7. 机器化守卫（Package 2-C §9 授权范围，已落地）

| # | 守卫 | 目的 |
|---|---|---|
| G-1 | `connection_conversations` 是**唯一**授权状态表 | 防第二张授权表 / 策略表 creep |
| G-2 | 该表无 `status` / `effect` / `priority` / `expires_at` 列 | 防 ACL 化 |
| G-3 | `deny` 不落库（deny = 缺行） | 防 effect 模型 |
| G-4 | 授权状态表唯一性 | 落实 §4 窄豁免的「唯一」 |
| G-5 / G-6 | 授权行唯一创建点 = 管理面 Service；运行时零创建 | 防运行时「顺手」补写 |

已落地证据（**全仓扫描式**守卫，是 ADR-5 守卫升级的既有先例）：

```
app 全仓扫描 "connection_conversations.create"
   → creators == ["connection_conversations.py"]
   （tests/test_package_2_step_4_authorization_contract_closure.py:324-329）
```

> 该守卫证明：本仓**已经**用「全仓扫描 + 显式允许清单」表达「唯一创建点」，
> 而不是「固定文件清单」。ADR-12 §11 要求 ADR-5 守卫采用同一形态。

---

## 8. 与 ADR-12 的关系

```
ADR-11 的窄豁免范围（connection_conversations = V0 唯一的授权状态存储）**不变**。
ADR-12 新增的 bootstrap_arms：
    · 不是授权状态表（不参与 find_granted_connection_ids）
    · 不进入 G-1 的「授权状态表」集合
    · 因此**不扩张** ADR-11 的窄豁免（CP-1 §4.3 已论证）
如需在守卫中表达该区分，属 ADR-12 §11 的守卫增补范围。
```

---

> 本文件为 **FROZEN** 补签件。任何修改须以「新增 ADR 编号 + 显式取代关系」方式提交，
> 不得原地改写（`authorization-and-access-model.md:702-703`）。
