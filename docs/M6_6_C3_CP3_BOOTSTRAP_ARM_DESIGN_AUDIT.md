# M6.6-C3-CP3 — Bootstrap Arm Design & Semantics Audit

> 性质：**READ-ONLY 设计审计**。未修改任何代码 / DB / migration / 测试 / 配置 / ADR /
> Package 2-C / Gate 2.6；未部署；未创建生产 Arm / 测试数据；未 commit / tag。
> 本轮唯一产出物 = 本文件。全部结论来自源码 / ORM / migration / 仓储 / UoW /
> 管理面 API / Runtime 边界 / 既有测试的静态阅读。
>
> 上游：`docs/M6_6_C3_CP1_FIRST_MESSAGE_BOOTSTRAP_SEMANTIC_DECISION.md`（CP-1：语义裁决）
> 　　　`docs/M6_6_C3_CP2_INBOUND_TRUST_GATE_AUDIT.md` + `..._CP2_IMPLEMENTATION.md`（CP-2：Trust Gate 已实现）
> 　　　`docs/M6_6_C3_R_FIRST_MESSAGE_RUNTIME_REAUDIT.md`（C3-R：运行时可行性）
>
> **结论先行：CONDITIONAL PASS。**
> Arm 的**核心语义模型在本轮已完整定义并可安全实现**（持久化形态 / 生命周期 /
> 最小 scope / TTL / one-shot / 消费原子时刻 / 同事务 / 并发 / 重复点击 / 过期 /
> 审计 / 身份与授权语义 / 组件落点 / 失败语义 全部有明确答案）；
> 但仍有 **5 项需要下一个 Gate 显式裁决的架构选择**（见 §21），其中
> 「新增窄表需 Cloud-0.1.5 显式豁免」「Arm 的操作入口面」「OSS 无鉴权路径下的 initiator」
> 三项属于**必须先裁决再实现**的类别。

---

## 1. Executive Summary

### 1.1 Arm 是什么（一句话）

> **Bootstrap Arm = 由已认证 Tenant Admin 显式发起的、短时、一次性、精准作用域、
> 可归属、可审计的「首次授权建立许可」**（Bootstrap Context）。
> 它**不是**授权本身，而是「**某一条**尚未知晓的会话，获得**一次**建立授权的机会」的许可。

### 1.2 二十二问速答

| # | 问题 | 结论 |
|---|---|---|
| 1 | Arm 是否必须持久化？ | **是**（§7）。理由不是「方便」，而是「one-shot + TTL + 审计」三件事都必须由**可原子判定的事实**承载 |
| 2 | 推荐 persistence shape？ | **A：新增窄表 `bootstrap_arms`**（复用 0007 的形状：tenant 自有列 + 复合 FK + RLS + 四元组式 UNIQUE） |
| 3 | 最小 scope？ | **(tenant_id, channel_id, connection_id)**；**不含 agent_id**（由 Connection 不可变绑定派生） |
| 4 | TTL 如何实现？ | `expires_at` 列 + **DB 时钟**（`now()`）；判定与消费在**同一条条件 UPDATE** 内完成；app 绝不生成绝对时刻 |
| 5 | one-shot 如何实现？ | **条件 UPDATE + `rowcount == 1`**（`claim_processing` 既有范式），非 Python 锁 |
| 6 | consume 的原子时刻？ | **Identity + Grant 事务成功提交的那一刻**（方案 B），与二者**同事务** |
| 7 | Arm 与 Identity/Grant 同事务？ | **是**（同一短事务：校验 → CAS 消费 → find-or-create Identity → find-or-create Grant → COMMIT） |
| 8 | 并发首条消息？ | DB CAS 裁决出**唯一赢家**；输家**回读**复用既有 Identity/Grant 后**继续正常管线**（不报错） |
| 9 | 重复 Verify？ | **Supersede**：新 Arm 取代旧 Arm（旧 ARMED 行同事务置 REVOKED）；同一 scope 至多一条 ARMED |
| 10 | expired Arm？ | **逻辑过期**（谓词判定），**不删除**（审计留存）；不 Bootstrap、零授权写入 |
| 11 | 审计字段？ | initiator_user_id / armed_at / expires_at / consumed_at / consumed_by_interaction_id / **consumed_external_identity_id + consumed_external_conversation_id**（两个裸键可精确反查 Identity 与 Grant，**不加 FK**，以免阻断授权撤销） |
| 12 | S-1 / S-2？ | **S-1 为 baseline**（含明确 residual risk 与边界）；**S-2 为可选加固，留独立 Gate**（Arm 行预留扩展点） |
| 13 | Identity `user_id`？ | **恒为 NULL**。绝不能绑定成 Tenant Admin |
| 14 | Grant 是否普通？ | **是**。普通 `ConnectionConversation` 行，无新列、无有效期、无特殊类型 |
| 15 | Bootstrap 组件在哪？ | **执行边界上的独立组件**，由 Worker 在 `runtime.execute(inbound)` **之前**调用 |
| 16 | Runtime 管线是否不变？ | **完全不变**。`RUNTIME_EXECUTION_ORDER` 仍 7 段，无第八段、无新 `STAGE_*` |
| 17 | CP-4 需要什么？ | M1/M2（find-or-create 冲突回读）、M3（Session 冲突回读）、Arm CAS、并发/重放/reclaim/多实例验证（§20） |
| 18 | 仍需哪些架构决策？ | §21 的 O-1 ~ O-5 |

### 1.3 Arm 不是什么（逐条对应"不得成为"清单）

| 不是 | 为什么（证据 / 守卫） |
|---|---|
| Authorization Grant | Grant = `ConnectionConversation`（`connection_conversation.py:36-42`）。Arm 不含 `effect / status 授权语义`，不参与 `find_granted_connection_ids`（`access_authorization.py:63-69`） |
| Identity | Arm 不写 `identities`（除 Bootstrap 事务内的 find-or-create） |
| User / ACP Account | Bootstrap 创建的 Identity `user_id = NULL`（§15） |
| Connection | Arm **引用** Connection，不创建 / 不改绑定（`connection.py:22-25` 绑定不可变） |
| Session | Arm 不读不写 `sessions`；Session 仍由既有阶段决定 |
| Interaction | Arm 只在被消费时记录 `consumed_by_interaction_id`（追溯），自身不是交互记录 |
| Discovery | Arm 不得由 `known-conversations` 推导（`connection_conversations.py:138-154` 红线复刻） |
| Runtime Stage | 不在 `RUNTIME_EXECUTION_ORDER`（`runtime.py:78-86`）内，不产生 `STAGE_*` |
| Agent State | 不落在 Agent / Connection.config（`connection.py:17-19`：config 不是万能配置容器） |

---

## 2. Current Architecture Evidence（逐项审查结果）

### 2.1 十八项现有能力清单

| # | 项 | 事实（证据） | 对 Arm 的意义 |
|---|---|---|---|
| 1 | Channel | `domain/channel.py:11-18`：name/type/status/config；`models/channel.py` tenant 自有列 | Arm 引用；type 由 CP-2 路由守卫 |
| 2 | Agent | `models/agent.py`；经 Connection 绑定 | **不进 Arm scope** |
| 3 | Connection | **UNIQUE(tenant_id, channel_id, agent_id)** + supporting **UNIQUE(tenant_id, channel_id, id)**（`models/connection.py:20-38`）；绑定不可变（`domain/connection.py:22-25`） | Arm 可用**复合 FK** 锁死 (tenant, channel, connection) 三元组错配（与 Grant 同形） |
| 4 | Identity | **UNIQUE(tenant_id, channel_id, external_id)**（`models/identity.py:30-35`）；`user_id` 可空 FK **NO ACTION**（`:38-40`）；`user_id=None` = 未绑定，禁 sentinel / 禁自动 Linking（`domain/identity.py:10-13`） | find-or-create 的幂等键已存在；`user_id=NULL` 是**已验证**的运行时路径（`tests/test_step9_2_identity_execution.py:91-96`） |
| 5 | User / Tenant | `AuthenticatedUser(user_id, tenant_id)`（`core/browser_auth.py:31-40`）；`browser_sessions` 冗余存 tenant_id（`models/browser_session.py:35-37`） | **initiator 事实已存在**，只是未接入管理面（见 §2.2） |
| 6 | Grant | 四元组 UNIQUE + 复合 FK（tenant,channel,connection）→ connections（`models/connection_conversation.py:59-70`）；**❌ status / expires_at / identity_id / user_id**（`:36-39`）；deny = 缺行（`domain/connection_conversation.py:14-15`）；行不可变（`:20-21`） | Grant 的**临时性只能来自 Arm**，不能来自 Grant 自身——本审计据此定稿 |
| 7 | Interaction | `processing: pending→processing→succeeded/failed`；`delivery: pending→delivered/failed`（`domain/interaction_record.py:35-42`） | Arm 不引入新状态机 |
| 8 | Activity | append-only、无 updated_at、字段 = type/status/stage/message/metadata/interaction_id/session_id（`domain/activity_record.py:18-48`）；Port 只有 `append` + `list_by_interaction`（`repositories/activity_record.py:17-35`） | 它是 **Runtime Evidence（互动维度）**，不是管理面审计 → §7 方案 C 被否的关键证据 |
| 9 | UnitOfWork | 共享 Session + 每 repo 注入 tenant_id + PG `set_config('app.tenant_id', …, true)`（`persistence/unit_of_work.py:88-119`）；`commit/rollback` 归 UoW（`:126-132`） | Arm 事务天然落在 **tenant-scoped UoW**（RLS + 显式过滤双层） |
| 10 | 事务边界 | 每个 phase 一个 `with uow` = 一次 COMMIT；执行（含 Agent 调用）**不在事务内**（`interaction_recording.py:152-217`） | 「短事务 COMMIT → Agent 执行」是**既有范式**，Arm 复用即可 |
| 11 | repository 模式 | Port（`app/repositories/`）+ Adapter（`app/persistence/repositories/`）；`create` 只 flush，commit 归 UoW；tenant 过滤显式追加（`repositories/identity.py:61-71`、`repositories/channel.py:35-45`） | Arm 仓储按同一 Port/Adapter 二分新增 |
| 12 | 管理面 API | `build_*_api(service_provider=...)`；`main.py:255-313` 每请求由 `TenantScope` 派生 service | Arm 的「发起」面应复用同一注入纪律（§21 O-3） |
| 13 | authorization 检查 | `granted == ∅ ⇔ DENY`；**不创建任何东西**（`access_authorization.py:22-23, 42-70`） | Bootstrap 在**管线之前**建立事实，Authorization 阶段照常执行 → 仍是 Authorization establishment，非 bypass |
| 14 | Runtime 执行边界 | `Worker.process`：claim(CAS) → `runtime.execute` → record（`worker.py:62-87`）；G1：claim 失败绝不执行 | **Bootstrap 的唯一合理落点 = claim 之后、execute 之前** |
| 15 | audit / activity | 无管理面审计层；`activities` 只服务 Runtime Evidence | Arm 自身即审计记录（§14） |
| 16 | Alembic 约定 | `0001..0007` 线性链；`revision/down_revision`；新表 = `op.create_table` + 索引 + **RLS（NULLIF 形态）**；**OSS SQLite 不跑 Alembic，由 `Base.metadata.create_all` 承担**（`0007:44-46, 61-64, 140-183`） | Arm 表必须**同时**落 ORM 与 migration（两条路径不分叉） |
| 17 | 唯一约束风格 | 全部 **tenant 前导**：identities(tenant,channel,external)、connections(tenant,channel,agent)、connection_conversations(tenant,channel,conv,conn) | Arm 沿用：tenant 前导 + 复合 FK |
| 18 | 时间戳 / DB 时钟 | `created_at/updated_at` = `server_default=func.now()`（全表一致）；**并发决策一律用 DB 时钟**：claim 写 `processing_started_at=func.now()`（`interaction_record.py:41-67`）、reclaim cutoff 由 DB 计算且按方言等价（`:177-193`）；app 侧 `datetime.now()` 仅见于**展示性** `checked_at`（`api/management/connections.py:213`） | **TTL 与消费判定必须用 DB 时钟** —— 这不是偏好，是已冻结的纪律 |

### 2.2 关键发现 1：initiator 事实已存在，但未接入管理面

```
api/auth.py:191-201  tenant_scope_from_request(request)
    proof = cookie → service.verify(proof) → AuthenticatedUser(user_id, tenant_id)
    return TenantScope(tenant_id=user.tenant_id)      ← user_id 在此被丢弃
main.py:255-313      管理面 service 只接收 TenantScope
```

⇒ `initiator_id` **不需要新机制**：只需在组合根新增一个「暴露 `AuthenticatedUser`」的依赖
（与 `build_tenant_scope_provider` 同形），管理面即可获得操作者身份。
（实现期工作量小，但属 `app/api/auth.py` + `main.py` 改动 → §21 O-5 需裁决 OSS 无鉴权路径。）

### 2.3 关键发现 2：并发正确性的既有范式（Arm 直接复用）

```
interaction_record.py:53-67   UPDATE ... WHERE id=? AND processing_status='pending'
                              → rowcount == 1 即为唯一赢家（CAS）
                              注释明写：刻意不用「读回再赋值」，也不用 SELECT FOR UPDATE（SQLite 不支持）
interaction_record.py:177-193 cutoff 由 DB 计算（sqlite: datetime('now','-N seconds')
                              / PG: now() - interval），app 只传阈值
```

### 2.4 关键发现 3：Grant 明确禁止 expires_at（决定了"临时性在哪"）

```
models/connection_conversation.py:36-39
    ❌ status / effect / priority / expires_at / conditions（纯 allow；deny = 缺行）
```

⇒ 因此 **Arm 的临时性绝不能下沉到 Grant**（否则与 ADR-9 / ADR-1 / ADR-3 红线冲突），
必须停留在 Arm Context 上。这条证据直接支撑 §16 的定稿。

---

## 3. Arm Semantic Model

### 3.1 定义

```
BootstrapArm = (
    scope        : (tenant_id, channel_id, connection_id)      # 精准作用域
    initiator    : initiator_user_id                           # 可归属
    window       : armed_at → expires_at                       # 短时
    one-shot     : status ∈ {ARMED, CONSUMED, REVOKED}         # 一次性
    trace        : consumed_at / consumed_by_interaction_id
                   consumed_external_identity_id / consumed_external_conversation_id
)
```

### 3.2 语义边界（三句话，不可互换）

1. **Arm 是「许可一次授权建立」，不是「授权」**：授权仍由 `connection_conversations` 行表达。
2. **首条消息是 Trigger，不是 Source**：Authorization Source 恒为 Admin 的 arm 动作（CP-1 裁决）。
3. **Trust Gate 是 Arm 的前提**：未过闸门 → DROP，Arm 存在与否都不得生效（CP-2 已实现并机器化）。

### 3.3 两段式模型（CP-2 冻结后，本轮在此基础上定义第二段）

```
入站消息
  │
  ├─ 段 1 Trust Gate（CP-2 已实现）── FAIL ──► 403 / 零 Interaction / 零 Identity / 零 Grant / 不入 Bootstrap
  │
  ▼ PASS
  ├─ 段 2 Bootstrap Eligibility（本轮设计）
  │     无 Arm / 已过期 / 已消费 / scope 不符 ──► no-op ──► 既有 Fail Closed（IDENTITY_RESOLUTION_FAILED）
  │     有有效 Arm ──► 同事务：CAS 消费 + Identity + Grant ──► COMMIT
  ▼
  Authorization（照常）→ Routing → Session → Agent → Delivery
```

---

## 4. Lifecycle

### 4.1 状态机（4 态，无中间态）

```
                 ┌──────────────── arm(admin) ────────────────┐
                 ▼                                            │
             UNARMED ──────────► ARMED ──── consume ────► CONSUMED
                                   │  │                        ▲
                                   │  └── expires_at ≤ now ─┐  │
                                   │         （逻辑过期）    │  │
                                   │                        ▼  │
                                   └── supersede/revoke ──► REVOKED
```

| 跃迁 | 触发 | 事务 | 可逆 |
|---|---|---|---|
| → ARMED | Admin 显式 Verify Connection | 独立短事务 | 否（只能 REVOKED 或过期） |
| ARMED → CONSUMED | Bootstrap 事务成功提交 | **与 Identity+Grant 同一事务** | **否**（终态） |
| ARMED → REVOKED | 新 Arm supersede / Admin 撤销 | 独立短事务 | 否（终态） |
| ARMED → (expired) | `expires_at ≤ now()` | **无写入**（谓词判定） | 逻辑状态，行保留 |

### 4.2 失败后 Arm 的四问（用户 §4 要求）

| 问题 | 答案 | 机制 |
|---|---|---|
| 失败后 Arm 是否仍存在？ | **是**（行永不被删除） | 审计留存 |
| 是否仍然有效？ | **是**（除非已过期或被 supersede/撤销） | 事务回滚 → `status` 未被改动 |
| 是否可以 retry？ | **可以**：下一条可信消息再次触发；或 Admin 重新 Verify（supersede） | 因为「消费」只在**成功提交**时发生 |
| 什么条件下 consumed？ | 见 §9：① 事务内确实建立了授权事实；② CAS 赢；③ 未过期；④ scope 匹配 | 四条同时成立 |

### 4.3 禁止状态（用户明令）

> **「Arm consumed 但 Bootstrap 完全没有建立授权」不可出现。**

保证方式（结构性，不靠自觉）：

```
CONSUMED 与 Identity/Grant 的创建写在**同一个事务**里；
事务内任一步失败 → ROLLBACK → status 回到 ARMED（未消费）。
唯一能产生 CONSUMED 的路径 = Identity 与 Grant 至少其一被创建（§9.4 消费条件）。
⇒ "CONSUMED 且无授权" 在状态空间上不可达。
```

---

## 5. Scope（最小可验证 Bootstrap Scope）

### 5.1 结论

```
最小 scope = (tenant_id, channel_id, connection_id)
```

### 5.2 为什么不含 agent_id

- `Connection` 已把 (channel_id, agent_id) 绑定，且**绑定不可变**（`domain/connection.py:22-25`）；
  `UNIQUE(tenant_id, channel_id, agent_id)`（`models/connection.py:20-24`）保证 (tenant, channel, connection) ⇒ agent **唯一确定**。
- 再存 agent_id = **第二个事实源**，且它是可漂移的冗余（语义上不可能漂移，但模型上多一个字段就要多一条一致性论证）。
- Routing 之后仍会重新从 Connection 读 `agent_id`（`routing.py:90-92`），Arm 不参与选择。

### 5.3 防错配（用户要求的两条）

| 要求防的错配 | 机制 |
|---|---|
| **Channel A 的 Arm → Connection B** | 复合 FK `(tenant_id, channel_id, connection_id) → connections(tenant_id, channel_id, id)`（与 Grant 同形，`models/connection_conversation.py:65-69`）。同租户跨 channel 也一并锁死（RLS 盖不住的那一种） |
| **Tenant A 的 Arm → Tenant B** | ① `tenant_id` 自有列 + RLS（与全表同纪律）；② UoW 注入 tenant_id 后的显式过滤；③ 复合 FK 前导列；④ 消费判定 `WHERE tenant_id = :tid`（`claim_processing` 同形） |

### 5.4 scope 内还需要的运行时校验（不属于 scope，属前置校验）

- Connection 必须存在（FK 保证）；
- **Connection 是否必须 active？** → **建议：arm 时要求 active**，但**消费时不再复核**。
  理由：若消费时 Connection 已 inactive，Bootstrap 建立的 Grant 会在 Routing 处落到
  `ROUTING_FAILED`（`routing.py:84-88`：active ∩ granted ≠ 1）→ 排障方向正确且无害；
  与 `access_authorization.py:17-19`（授权不消费 Connection 状态）同一裁决。
  （此项为建议，非 scope 组成；纳入 §21 O-2 一并裁决。）

---

## 6. Existing Objects Only（原则与落地）

| 约束 | 落地机制 |
|---|---|
| 不得创建 Tenant / Channel / Agent / Connection | Arm 只**读**它们做存在性 + 归属校验（同 `connection_conversations.py:74-93` 的既有校验顺序）；Arm 表不提供任何 create |
| 不得创建 ACP User | Bootstrap 创建的 Identity `user_id = NULL`（§15）；Arm 表不写 `users` |
| 允许 find-or-create External Identity | `identities` 既有 UNIQUE(tenant,channel,external)（`models/identity.py:30-35`） |
| 允许 find-or-create Conversation Grant | `connection_conversations` 既有四元组 UNIQUE（`:59-63`） |
| 「find-or-create」不等于「先查再插」 | 并发下必须捕获 `IntegrityError` → 回读复用（CP-4 的 M1/M2，见 §20；当前管理面是「预检→create」且不捕获，`identities.py:128-139`、`connection_conversations.py:84-98`） |

---

## 7. Persistence Alternatives（本轮最重要设计问题）

### 7.1 逐项分析（9 个维度）

| 维度 | **A. 新表 `bootstrap_arms`** | **B. Connection 状态字段** | **C. Activity / metadata** | **D. 纯内存** | **E. Channel.config JSON** |
|---|---|---|---|---|---|
| crash recovery | ✅ 行即事实，重启后一致 | ⚠️ 在，但语义混用 | ⚠️ 在（append-only），但无法表达「当前是哪一个」 | ❌ 重启即失 | ⚠️ 在，但无约束 |
| multi-worker（同进程多线程） | ✅ DB CAS | ❌ 无原子消费原语 | ❌ append-only 无 update → 无法「消费」 | ❌ 需进程内锁，跨线程可但脆弱 | ❌ JSON 无 CAS |
| multi-instance（多进程） | ✅ DB CAS（PG 成立） | ❌ | ❌ | ❌ **完全失效** | ❌ |
| TTL | ✅ `expires_at` 列 + DB 时钟谓词 | ❌ 无处安放（Connection.status 是可用性语义） | ⚠️ 只能靠插入一条「expired」事件推断 | ⚠️ 可但重启即失 | ⚠️ 可存但无约束、无索引 |
| one-shot（原子消费） | ✅ 条件 UPDATE `rowcount==1` | ❌ **无法原子表达**（改 status 会污染 Router 输入） | ❌ **结构上不可能**（Port 无 update，且刻意 append-only） | ❌ 非 DB 原子 | ❌ |
| audit（谁/何时/针对什么） | ✅ 同行承载全部字段 | ❌ 只能表达「是/否」 | ⚠️ 可 append，但无主键 → 无法精确回答「当前窗口」 | ❌ 重启即失 | ⚠️ 可存但不可精确查询 |
| concurrency | ✅ 与 `claim_processing` 同范式 | ❌ | ❌ | ❌ | ❌ |
| transactional consumption | ✅ 与 Identity+Grant 同事务 | ❌ 会与 Router 状态耦合 | ❌ | ❌ | ❌ |
| restart recovery | ✅ | ⚠️ | ⚠️ | ❌ | ⚠️ |
| observability | ✅ 可列/可查（管理面读路径） | ⚠️ 语义重叠 | ⚠️ 需扫描全表推断 | ❌ | ❌（还要过脱敏逻辑） |

### 7.2 判定

**Recommended = A（新增窄表 `bootstrap_arms`）。**

决定性理由（**不是**「因为想加就加」，而是语义必要性）：

1. **one-shot 只能由「可原子变更的行状态」承载**。本仓已有明确范式
   （`claim_processing` 的条件 UPDATE），而 B/C/D/E **都不提供可被条件 UPDATE 的行**
   ——C 的 Port **刻意**没有 update（`repositories/activity_record.py:17-22`：append-only，
   「历史事实不允许被改写」），这一条就是 C 的结构性否决点。
2. **TTL 判定必须与消费判定在同一条 SQL 内**（否则出现 TOCTOU：读到未过期 → 写入时已过期）。
   只有 A 能写出 `WHERE status='armed' AND expires_at > now()`。
3. **审计必须持久且可归属**：CP-1 冻结 P4（可归属可审计）与「Authorization Source 是
   Admin 的显式动作」——内存态（D）在重启后无法回答「这个 Grant 是谁允许的」。
4. **本仓对「新增窄表」有成功先例**：`connection_conversations` 正是以
   「Package 2-C §1.5 窄范围例外 + ADR-11 授权」的方式新增，并同时落 ORM + migration + RLS
   （`0007`）。Arm 依同一路径，不发明新机制。
5. **成本已被既有模式摊薄**：1 个 ORM 模型 + 1 个 migration + 1 个 Port/Adapter，
   全部照抄 `0007` / `connection_conversation.py` 的形状。

**明确说明**：新表**不是**「为了少写持久化而妥协」，也**不是**回避 migration；
本审计已确认它**必须同时**落 ORM（OSS/SQLite `create_all`）与 Alembic（Cloud/PG），
两条路径不分叉（`0007:44-46`）。

### 7.3 Rejected Alternatives（含否决点）

| 方案 | 否决点 |
|---|---|
| **B. Connection 状态字段** | ① `Connection.status` 是**可用性**语义，被 Routing 直接消费（`routing.py:80`）；把 "armed" 混进去 = 让 Router 的输入承载授权窗口语义。② 装不下 `expires_at / initiator / consumed_by`。③ 无法表达 CONSUMED vs EXPIRED vs REVOKED。④ `connection.py:17-19`：config 不是万能配置容器（同纪律） |
| **C. Activity / metadata** | ① Port **无 update**（append-only 是刻意的）→ **one-shot 在结构上不可实现**。② 无唯一键 → 无法回答「当前有几个有效窗口」。③ `activities` 是 **Runtime Evidence**（互动维度，`activity_record.py:18-31` 的 STAGE_* 常量），塞管理面授权上下文会污染 Evidence 语义 |
| **D. 纯内存** | ① 重启即失 → 不可审计、不可恢复。② 多实例完全失效。③ 与「授权来源必须是显式、可持久归属的动作」冲突 |
| **E. Channel.config JSON** | ① 无约束、无索引、无 CAS。② `channels.config` 承载 WeCom 凭据且**响应经脱敏**（`api/management/channels.py:26-28`）+ **merge 语义**（`application/channels.py:171-190`）→ 追加授权窗口进凭据容器是最差归属。③ CP-1 §10.3 已登记该张力 |

### 7.4 推荐表形状（设计，不是实现DDL）

```
bootstrap_arms
  id                                PK
  tenant_id          NOT NULL  FK tenants.id       索引 + RLS 谓词列
  channel_id         NOT NULL  FK channels.id
  connection_id      NOT NULL
  status             NOT NULL  ∈ {armed, consumed, revoked}
  initiator_user_id  NULL      FK users.id（NO ACTION）
  armed_at           NOT NULL  DB 默认 now()
  expires_at         NOT NULL
  consumed_at        NULL
  consumed_by_interaction_id   NULL  (int, 无 FK —— 见 §14.3)
  consumed_external_identity_id      NULL  (str)
  consumed_external_conversation_id  NULL  (str)
  created_at / updated_at     DB 默认（全表同纪律）

约束（照抄 0007 的形状）
  UNIQUE(tenant_id, channel_id, connection_id) WHERE status='armed'   ← 部分唯一（见 §12 / O-1）
  复合 FK (tenant_id, channel_id, connection_id) → connections(tenant_id, channel_id, id)
  RLS：ENABLE + FORCE + tenant_isolation（NULLIF(current_setting('app.tenant_id', true),'')::integer）
  索引：ix_bootstrap_arms_tenant_id、（tenant_id, channel_id, connection_id）由唯一约束前导覆盖
```

> `consumed_*` 三个追溯字段**刻意不加 FK**（§14.3 给出理由：避免"审计"反过来阻断授权撤销）。

---

## 8. TTL 语义

### 8.1 TTL 是否必需？**必需。**（三条理由）

1. **P2（有界）是 CP-1 冻结的授权前提属性**：无 TTL 的 Arm = 长期敞开的「首次接触即可建立授权」，
   正是 CP-1 §4.2 明确否决的形态（会把 C 静默退化成 A）。
2. **Arm 是授权来源的一部分**：授权来源必须有时限（否则"Admin 的某次点击"永久有效，
   违背「显式、有界」两条属性）。
3. **S-1 baseline 下 TTL 是唯一的"窗口"定义**（§15）：窗口越短，"陌生人抢先"的 residual risk 越小。

### 8.2 时间源裁决

| 候选 | 判定 | 理由 |
|---|---|---|
| **DB 时钟（`now()` / `server_default=func.now()`）** | ✅ **推荐** | 既有铁律：`claim_processing` 写 `func.now()`（`interaction_record.py:49-64`），reclaim cutoff 由 **DB** 计算、app 只传阈值（`:139-151`）；多 worker / 多实例下语义一致（单一时钟） |
| application 时钟（`datetime.now(timezone.utc)`） | ❌ | 多实例时钟偏移 → 同一 Arm 在不同进程判定不一致；本仓仅**展示性**字段用它（`api/management/connections.py:213` 的 `checked_at`），从不用于决策 |
| server clock / 事务时间戳混合 | ❌ | 引入两套语义；本仓全表 `created_at/updated_at` 已统一为 `server_default=func.now()` |

### 8.3 落地语义（规避 skew / timezone）

```
1. arm 时：expires_at 由 **DB** 计算（now() + interval），app 只传「阈值间隔」
           —— 与 _stale_cutoff 的「app 只传间隔」纪律同形（interaction_record.py:177-193）
2. 判定时：不读回 expires_at 与 app 时间比较；
           直接写进消费 SQL：WHERE status='armed' AND expires_at > now()
           —— 判据与写入同语句，无 TOCTOU
3. 列类型：DateTime(timezone=True)（全表同纪律）
4. 方言：SQLite 用 datetime('now', ...) / PG 用 now() - interval 的既有等价写法
```

⇒ **多 worker / 多实例语义一致**（唯一时钟 = DB；判定与写入原子）。

---

## 9. One-Shot Consumption

### 9.1 四个候选时刻的比较

| 方案 | 消费时刻 | 失败后果 | 判定 |
|---|---|---|---|
| **A** | 收到 Trust-Gated message 就消费 | 第一条消息**任何**下游失败（Agent/Delivery/Session）→ Arm 已消耗、授权可能已建也可能未建 → **用户无法重新验证** | ❌ 命中用户明令禁止的形态 |
| **B** | **Identity + Grant 事务提交成功时消费（同事务）** | 事务失败 → 回滚 → Arm 仍 ARMED → 可 retry；事务成功 → 授权已存在，Arm 终结 | ✅ **推荐** |
| **C** | Interaction processing 成功后消费 | Agent 执行期间 Arm 仍 ARMED → 第二条消息可能**再次** Bootstrap（N 次）；且消费发生在外部 I/O 之后，跨事务 | ❌ 破坏 one-shot |
| **D** | Agent delivery 成功后消费 | 同 C，窗口更长（含渠道投递等待），N 次风险最大 | ❌ |

### 9.2 精确定义

> **消费 = 「在 Bootstrap 事务内，CAS 把 `status` 从 `armed` 改为 `consumed` 且 `rowcount == 1`」，
> 并且该事务同时完成了 Identity / Grant 的 find-or-create。**
> 消费与授权建立**原子同生**（同事务），因此二者不可能分离。

### 9.3 「最多一次 Bootstrap」的准确含义

```
1 个 Arm  ⇒ 至多 1 次「授权建立事务成功提交」
         ⇒ 至多 1 条新 Identity（该 (channel, external_id)）+ 至多 1 条新 Grant（该 conversation）
         ⇒ 第二及后续消息：find-or-create 命中既有行（幂等），照常进入管线
```

### 9.4 消费条件（四条同时成立才写 CONSUMED）

```
① CAS 赢：UPDATE ... WHERE id=? AND status='armed' AND expires_at > now() → rowcount==1
② scope 匹配：(tenant_id, channel_id, connection_id) 与入站 (channel_id) 及 Arm 目标一致
③ 未过期：同一条 SQL 的 expires_at > now()（DB 时钟）
④ 该事务确实建立了授权事实：Identity 与 Grant 至少其一由本事务创建
   —— 若二者**都已存在**（例：Admin 再次 arm 一条已授权的会话），本事务 **不消费**
      （Arm 保持 ARMED 直至自然过期），避免「消费了却什么都没建立」
```

### 9.5 两个必须防止的形态（逐条对照）

| 用户要求防止 | 由哪条保证 |
|---|---|
| 第一条消息失败 → Arm 永久消耗 → 无法重新验证 | ① 消费只在**成功提交**时发生（§9.1-B）；② 事务失败回滚保留 ARMED |
| 第一条消息失败 → Arm 永远有效 → 多次 Bootstrap | ① **TTL 无条件生效**（过期即失效，与成功失败无关）；② CAS 保证至多一次；③ 成功即 CONSUMED（终态） |

---

## 10. Transaction Boundary

### 10.1 结论：Identity + Grant + Arm 消费 **同事务**

基于当前 UoW / repository / 事务架构的判断（不是预设）：

- `SqlAlchemyUnitOfWork` 一个 `with uow` = 一个 Session = 一次 COMMIT（`persistence/unit_of_work.py:88-132`）；
- Repository 只 flush，commit 归 UoW（`repositories/identity.py:35-41`）；
- 既有「短事务 COMMIT → 无事务执行 → 短事务落终态」是 Worker/PersistingCoordinator 的范式
  （`worker.py:74-86`、`interaction_recording.py:152-217`）；
- 因此「Bootstrap 短事务」与既有形状**同形**，不引入新事务机制。

### 10.2 推荐事务形状

```
BEGIN（tenant-scoped UoW：RLS set_config + repo 显式过滤）
  ├─ 校验 Arm：读 Arm（同一 scope、status=armed、expires_at > now()）
  ├─ CAS 消费：UPDATE ... SET status='consumed', consumed_at=now(),
  │                          consumed_by_interaction_id=:iid, consumed_external_*=:keys
  │            WHERE id=:arm_id AND status='armed' AND expires_at > now()
  │            → rowcount == 0 ⇒ 未取得 ⇒ 走 §11 输家路径（不抛、不消费）
  ├─ find-or-create Identity（UNIQUE 冲突 → 回读复用，M1）
  ├─ find-or-create Grant   （UNIQUE 冲突 → 回读复用，M2）
  └─ COMMIT
（事务外）Agent 执行 → Delivery
```

**顺序要点**：CAS **必须**在 Identity/Grant 写入**之前**（先取得许可，再写授权）；
但若 Identity/Grant 写入失败 → 回滚 → CAS 一并回滚 → Arm 自动回到 ARMED。

### 10.3 「Identity/Grant/Arm 已提交，随后 Agent 执行失败」怎么办？

**判定：允许，且是正确的。** 三条理由：

1. **授权建立是事实，不是待回滚的副作用**。它已经通过了 Trust Gate 与 Arm 两道门，
   是 Admin 显式许可的产物。把它跟一次 Agent 调用的成败绑定，等于让「一次外部系统抖动」
   回滚「一次人工授权决策」——语义上不成立。
2. **不会破坏用户可继续**：Grant 已存在 ⇒ **下一条**消息直接进入正常管线并得到回复，
   且**不需要重新 Verify**（Arm 已终结但授权已建立）。
3. **不产生"消耗但无授权"**：授权**确实**建立了（Identity+Grant 已 COMMIT），
   与 §4.3 的禁止状态无关。

**必须诚实记录的 UX 事实**：

> Cloud-0.1.5 冻结的 First Success = `processing=succeeded × delivery=delivered`。
> 因此**第一条消息本身仍可能失败**（`AGENT_DISPATCH_FAILED` / `CHANNEL_DELIVERY_FAILED`）。
> Arm 保证的是「**授权已建立**」，**不是**「第一次调用必然成功」。
> 现存重试能力：`AGENT_DISPATCH_FAILED` 与 `CHANNEL_DELIVERY_FAILED` 的
> `retryable=True`（`interaction_record.py:104-118`），但既有 reclaim **只回收 stale PROCESSING**
> （`:129-175`），**不重放已终态的行** ⇒ 首条消息不会自动重跑。

⇒ 因此「首条即得回复」的**可达性**由 Agent/Delivery 的既有可靠性决定，
Arm 只负责让首条消息**有资格**进入这条路径。这个区分必须写进 CP-3 实现文档，
以免被误解为「Arm 保证首条成功」。

### 10.4 明确不做

```
❌ 不在事务内调用 Agent（既有纪律：外部 I/O 不占事务）
❌ 不为"补偿"而删除已提交的 Identity/Grant（无 saga）
❌ 不因 Agent 失败而把 Arm 回滚成 ARMED（那是"让外部系统决定授权成败"）
```

---

## 11. Concurrency

### 11.1 场景：消息 A / B 几乎同时到达，两者 Trust Gate PASS、Arm = ARMED

```
A: BEGIN → 读 Arm(armed) → CAS UPDATE → rowcount?
B: BEGIN → 读 Arm(armed) → CAS UPDATE → rowcount?
```

数据库把两条 UPDATE **串行化**：先到者把 `status` 改为 `consumed`，后到者的
`WHERE status='armed'` **不再匹配** → `rowcount == 0`。

### 11.2 赢家 / 输家路径

| | 行为 |
|---|---|
| **赢家**（rowcount==1） | 继续 find-or-create Identity / Grant → COMMIT → 正常管线 → Authorization 读到非空许可集合 |
| **输家**（rowcount==0） | **不消费、不报错**；进入**回读分支**：再查一次 Identity / Grant（find，不 create）→ ① 命中（赢家已提交）→ **照常继续正常管线**；② 未命中（赢家尚未提交/已回滚）→ **回落既有 Fail Closed**：`IDENTITY_RESOLUTION_FAILED`（不新造错误码） |

> **输家不抛 UNEXPECTED_FAILURE** —— 这是与 CP-1 §6.3（M3 会把并发一侧打成失败）
> 同一类问题，必须在实现时按"回读复用"处理，否则并发首条消息会命中
> 「用户可见失败」这一缺陷。

### 11.3 三种部署形态

| 形态 | 结论 | 依据 |
|---|---|---|
| **single worker** | ✅ 安全 | CAS 在 DB，与线程/协程无关 |
| **multi worker**（同进程多线程，如 per-tenant Driver + BackgroundTasks） | ✅ 安全 | 同一条条件 UPDATE；不使用进程内锁 |
| **multi instance**（多进程 / 多副本） | ✅ 安全 | PostgreSQL 串行化同一行的 UPDATE；SQLite(OSS) 单文件写入亦串行。**注意**：`wecom_bot` 的活连接前提使 Cloud 实际是单实例（CP-1 §9.3 W-2），但 Arm 的正确性**不依赖**该前提 |

### 11.4 机制选择（用户要求）

```
✅ 数据库原子条件（条件 UPDATE + rowcount）
✅ 既有 UNIQUE（identities / connection_conversations）
✅ find-or-create（冲突 → 回读复用，CP-4 的 M1/M2）
❌ Python 内存锁（跨进程无效）
❌ SELECT ... FOR UPDATE（SQLite 不支持，且会把锁持有到执行结束 —— 见 claim_processing 注释）
```

### 11.5 仍需 CP-4 处理的并发面（本轮不实现）

```
P-1  Arm CAS                    → 本设计已给出（条件 UPDATE）
P-2  Identity find-or-create    → M1（IntegrityError → 回读）
P-3  Grant    find-or-create    → M2
P-4  Session 唯一约束冲突       → M3（既有缺陷，CP-1 §6.3；非 Bootstrap 引入）
```

---

## 12. Repeated Verify Clicks

### 12.1 四方案比较

| 方案 | UX | Security | Concurrency | Audit | 判定 |
|---|---|---|---|---|---|
| **A 拒绝第二次（409）** | ❌ 用户改主意/手滑后无法重开 | ✅ 最保守 | ✅ 简单 | ⚠️ 无新记录 | ❌ |
| **B 覆盖旧 Arm** | ✅ | ⚠️ 旧窗口被"静默"替换，无痕 | ⚠️ 覆盖需先写后写，顺序不定 | ❌ 丢历史 | ❌ |
| **C 复用旧 Arm** | ⚠️ 用户点了却"没反应"（窗口没刷新） | ✅ | ✅ | ⚠️ 无法体现"我点过" | ⚠️ |
| **D 允许多个 Arm** | ✅ | ❌ 多条 ARMED → 可能多次 Bootstrap（破坏 one-shot） | ❌ | ✅ | ❌ |
| ✅ **Supersede（推荐）** | ✅ 最后一次点击决定窗口 | ✅ 同一 scope 至多一条 ARMED | ✅ 两次事务串行，末次胜出 | ✅ **每次点击都留行** | ✅ |

### 12.2 推荐语义：Supersede

```
arm(tenant, channel, connection, initiator, ttl):
    同一事务内：
      ① 把该 scope 上所有 status='armed' 的行置为 REVOKED（含未过期者）
      ② INSERT 一条新的 status='armed'（expires_at = now() + ttl）
    ⇒ 同一 scope 恒至多一条 ARMED；每次点击都是一条独立、可审计的 Arm 行
```

- **双击/并发点击**：两事务串行（或唯一约束冲突→重试/幂等）→ 结果仍是**至多一条 ARMED**；
- **审计**：每次点击留一行（initiator + armed_at），REVOKED 行保留 → 可回答"谁点了几次、哪次生效"；
- **安全性**：不放大窗口数量（one-shot 不被稀释）。

### 12.3 唯一性实现（见 §21 O-1）

```
首选：部分唯一索引 UNIQUE(tenant_id, channel_id, connection_id) WHERE status='armed'
      （PG 与 SQLite 均支持部分索引）
若方言/迁移摩擦不可接受：退化为「同事务 revoke-then-insert」（应用层保证，弱于 DB 约束）
```

---

## 13. Expired Arm

### 13.1 语义

```
ARMED 且 expires_at <= now()
  ⇒ 不 Bootstrap
  ⇒ 不创建 Identity
  ⇒ 不创建 Grant
  ⇒ 不产生任何授权写入
  ⇒ 该条入站消息走既有 Fail Closed：Identity Resolution 未命中 → IDENTITY_RESOLUTION_FAILED
```

机制：**消费 SQL 的 `expires_at > now()` 谓词直接让 CAS 落空**（`rowcount == 0`），
无需先"读状态再判断" → 无 TOCTOU，也不需要后台清理任务。

### 13.2 是否需要删除？**不需要。**

- 保留理由：**审计留存**（可回答"这条会话的授权是何时、由谁的哪次 arm 建立的"）；
- 过期是**逻辑状态**（谓词判定），不需要物理删除；
- 过期行**不阻塞**新 Arm（supersede 只动 `status='armed'` 的行）；
- 可选：为 UI 显示加一个非权威的"标记 expired"清扫（**不参与任何安全判定**），
  若做，必须明确它只是展示优化。

⇒ 先做 **logical expiration + audit retention**，不做物理删除。

---

## 14. Initiator / Audit

### 14.1 必须成为 persistence fields（Arm 行上）

| 字段 | 理由 |
|---|---|
| `tenant_id` | 隔离 + RLS 谓词列 + 复合 FK 前导（全表纪律） |
| `channel_id` | scope 组成 + 复合 FK |
| `connection_id` | scope 组成 + 复合 FK |
| `initiator_user_id` | **CP-1 冻结：initiator 是合法性的组成部分**（P4）。来源已存在（`AuthenticatedUser.user_id`，§2.2） |
| `armed_at`（created_at） | 「什么时候发起」 |
| `expires_at` | 「何时过期」 |
| `status` | 「是否消费 / 是否被取代」 |
| `consumed_at` | 「何时消费」 |
| `consumed_by_interaction_id` | 「被哪个 Interaction 消费」 |
| `consumed_external_identity_id` | 与 (tenant, channel) 一起 = Identity 的 **UNIQUE 键** ⇒ 可精确反查"创建了哪个 Identity" |
| `consumed_external_conversation_id` | 与 (tenant, channel, connection) 一起 = Grant 的 **四元组 UNIQUE 键** ⇒ 可精确反查"创建了哪个 Grant" |

### 14.2 不必成为字段（可推导 / 明确不存）

| 候选 | 处理 | 理由 |
|---|---|---|
| `agent_id` | **不存** | 由 Connection 不可变绑定派生（§5.2） |
| `identity_id` / `grant_id` | **不存**（改用两个外部裸键反查） | 见 §14.3 |
| `revoked_at` / `revoked_by` | 可选（supersede 场景） | 建议用 `updated_at` + status 表达；若需"谁撤销的"再单独裁决 |

### 14.3 为什么 `identity_id` / `grant_id` **不加外键**（关键裁决）

```
若 bootstrap_arms.identity_id / grant_id 加 FK 且 ondelete 为默认（NO ACTION，本仓风格）：
    ⇒ 管理员删除（撤销）那条 Grant 会被 FK 阻断
    ⇒ 违反 ADR-9「允许的集合必须可增可减」/ ADR-6「授权必须可撤销」
```

⇒ **采用"裸键追溯"**：`consumed_external_identity_id` + `consumed_external_conversation_id`
（两个 `str`，无 FK）⇒ 既能精确反查 Identity（UNIQUE(tenant,channel,external_id)）与
Grant（四元组 UNIQUE），又**不妨碍**授权的正常撤销。这与本仓
「外部世界的 ID 都是裸键，只在自己的 Channel Scope 内解释」的纪律一致
（`interaction_record.py:19-28`）。

### 14.4 Activity 的角色

- **不写入 `activities`**。理由：它是 Runtime Evidence（互动维度、`STAGE_*` 常量、append-only），
  把管理面授权上下文塞进去会污染 Evidence 语义，且无法表达"当前窗口"（§7.3-C）。
- **Arm 行本身就是审计记录**：一行即可回答
  谁启动 / 何时 / 针对什么 / 何时过期 / 是否消费 / 被哪个 Interaction 消费 / 建立了哪个 Identity 与 Grant。
- 可选加固（非本轮）：为 `armed / revoked` 事件追加 Evidence；若要做，需先明确
  「管理面 Evidence 的 type 词表」——属独立裁决。

**注意**：`initiator_user_id` 的 FK 到 `users.id` **不强制同租户**（`users` 是全局 PK）。
同租户性由 ① Arm 行 `tenant_id`、② UoW tenant 作用域（RLS + 显式过滤）、
③ arm 时 `initiator` 来自**已认证会话**（其 `tenant_id` 与 TenantScope 一致，`core/browser_auth.py:31-40`）
共同保证；建议在消费/展示路径再加一条"initiator 的 tenant_id == arm.tenant_id"的断言测试。

---

## 15. S-1 vs S-2

### 15.1 比较

| 维度 | **S-1 Operator-in-the-loop** | **S-2 Challenge** |
|---|---|---|
| Security | ⚠️ **residual risk**：窗口内**任意**首个会话可获得授权（陌生人抢先） | ✅ 只有**持有 challenge 的人**能完成 Bootstrap（消除抢先） |
| UX | ✅ 点一下 → 发消息 → 成功 | ⚠️ 需展示 token → 用户复制 → 第一条消息必须是 token（"第一条消息不是真实对话"） |
| Complexity | ✅ 低（Arm 行数 = scope+window+trace） | ⚠️ 中：challenge 生成/存储（建议 hash）、比较逻辑、过期与重试、UI 面 |
| Channel compatibility | ✅ 与渠道无关 | ⚠️ 要求"用户能把文本发进该会话"；`wecom` 单聊/群聊均可，但未来渠道（语音/卡片）需逐个裁决 |
| Audit | ✅ 同等（Arm 行全字段） | ✅ 同等（+ challenge 已核销的事实） |
| Future channels | ✅ 中立 | ⚠️ 每个新渠道都要重新确认"能否承载 challenge 文本" |
| First Success experience | ✅ 第一条真实消息即得到真实回复 | ⚠️ 第一条消息被 challenge 占用；真实回复落在**第二条** |

### 15.2 Recommendation

> **Baseline = S-1**（本轮设计定稿，CP-4 实现）。
> **S-2 = Optional hardening，留独立 Gate**（不在本轮实现，但设计必须预留扩展点）。

选择 S-1 的理由：

1. Arm 的**合法性来自 Admin 的显式动作**（CP-1 已裁决），S-1 已满足 P1~P5 全部五条属性；
2. S-2 的成本落在**每一次**验证（UX 与实现），而它消除的风险**可由 S-1 的其他属性显著收敛**（见下）；
3. S-1 是"首条真实消息即得真实回复"这一目标的**唯一**承载形态（S-2 会把回复推到第二条）。

### 15.3 S-1 的 residual risk（必须明确登记）

```
R-S1  在 Arm 窗口内，第一个触达该 Channel 的**任意**会话（未必是 Admin 预期的用户）
      将获得一次 Identity + Grant 建立。
```

收敛措施（全部为本设计的既有属性，不额外发明）：

| 措施 | 效果 |
|---|---|
| 短 TTL（建议 10 分钟量级，实现期定常数） | 窗口极小 |
| one-shot | 至多 1 条授权 |
| 精准 scope（单条 Connection） | 不波及其它路径 |
| Supersede | Admin 可随时重开/覆盖窗口 |
| 完整审计（initiator + consumed_external_conversation_id） | 事后可精确回答"哪个会话被建立了授权、谁允许的" |
| Grant 是**普通且可撤销**的 | 误授权可按 ADR-9 正常撤销 |

**残留**：无法在**事前**证明"首个触达者就是预期用户"。这是 S-1 的固有代价，
必须以文字形式登记在 CP-3 实现文档中，不得被后续文档淡化。

### 15.4 S-2 的实现成本（若未来裁决采纳）

```
Arm 行新增（可选扩展点，本轮不加）：
    challenge_hash      NULL   （建议存 hash，不存明文）
    challenge_algorithm NULL
消费逻辑新增：入站 content 与 challenge 比对 → 匹配才允许 Bootstrap（否则 no-op）
UI：Verify Connection 展示 challenge；首条消息引导
```

⇒ 本设计**预留字段位**（不实现），使 S-2 成为"加字段 + 加比对"，而非模型重构。

---

## 16. Bootstrap 与 Identity

### 16.1 结论：Bootstrap 创建的是 **External Identity**，`user_id` **恒为 NULL**

### 16.2 为什么不能 `user_id = Tenant Admin`

| 理由 | 证据 |
|---|---|
| Identity 是**外部主体**（WeCom userid），Admin 是**ACP 主体**；两者语义分离 | `domain/identity.py:3-8, 10-13` |
| 绑定后 `IdentityResolution` 会返回 `user = Admin`（`identity_resolution.py:70-73`）⇒ 该外部用户在 Runtime 中被当作 Admin | 直接后果：任何基于 User 的后续语义都会把外部动作归属给 Admin |
| `user_id = None` 是**已冻结的正常状态**，不是匿名/访客 | `domain/identity.py:10-13`；`models/identity.py:7-10` |
| 禁止 sentinel user_id、禁止自动 Linking | `domain/identity.py:13` |
| 未绑定身份已有**验证过的运行时路径** | `tests/test_step9_2_identity_execution.py:91-96`（播种 `user_id=None` 并断言解析成功） |

### 16.3 若要绑定怎么办

⇒ 属 **Identity Linking**（未来 Slice），必须是**独立的、显式的管理动作**
（`domain/identity.py:21-24`：把两个外部身份判定为同一人需管理员显式执行）。
Bootstrap **绝不**顺带完成绑定。

---

## 17. Bootstrap 与 Grant

| 断言 | 依据 |
|---|---|
| Grant 仍是 **Conversation → Connection** | `domain/connection_conversation.py:3-8` |
| Bootstrap 只创建**普通 Grant** | 复用 `uow.connection_conversations.create(ConnectionConversation(channel_id, external_conversation_id, connection_id))`（`connection_conversations.py:94-97`） |
| **不新增列 / 不新增表 / 不新增 grant 类型** | `models/connection_conversation.py:36-39`：❌ status / effect / priority / **expires_at** / conditions |
| 不创建 temporary / bootstrap grant | 同上；Bootstrap 的**临时性只来自 Arm**（§2.4 的证据） |
| 成功后 Grant 与普通授权**不可区分** | 这是**要求**，不是妥协：Authorization/Routing 不得因来源不同而区别对待（`access_authorization.py:42-70` 只认会话键） |
| 可审计性不由 Grant 承载 | 由 Arm 行的 `consumed_external_conversation_id` 承载（§14） |

⇒ **「Bootstrap 的临时性来自 Arm Context，不是 Grant 本身」** 在本设计里是**结构性**的：
Grant 表**没有**任何可表达"临时"的列，因此不可能把临时性下沉。

---

## 18. Bootstrap Component Boundary

### 18.1 五个候选落点

| 候选 | 判定 | 理由 |
|---|---|---|
| **执行边界：Worker 在 `runtime.execute(inbound)` 之前调用独立组件** | ✅ **推荐** | ① 七段管线**一行不改**；② 与 `PersistingCoordinator` 同形（"执行边界上的独立组件，同 Protocol 形状"）；③ 语义干净：Bootstrap 是管线的**前置条件**，不是其中一段；④ 失败可回落既有 Fail Closed |
| `before pipeline.execute`（同上，只是措辞） | ✅ 同上 | — |
| **IdentityResolver 内** | ❌ | ADR-5 + `identity_resolution.py:28-32` 明写「**绝不创建** Identity / User」；有 AST 守卫与测试锁定 |
| **AccessAuthorizationExecutor 内** | ❌ | Package 2 冻结「**不创建任何东西**」（`access_authorization.py:22-23`） |
| **RuntimeCoordinator 内 / 新增第八段** | ❌ | `RUNTIME_EXECUTION_ORDER` 冻结 7 段（`runtime.py:78-86`）；新增 = 有序变更，会污染全部依赖顺序常量的守卫（CP-1 §10.1） |

### 18.2 推荐形状（设计，非实现）

```
Worker.process(interaction_id)
   ├─ load snapshot                        （既有）
   ├─ claim_processing（CAS，短事务）       （既有）
   ├─ ★ Bootstrap.ensure(inbound, interaction_id)   ← 新增：独立短事务；不满足即 no-op
   │     NOT_APPLICABLE（无 Arm / 过期 / 已消费 / scope 不符）→ 什么都不做
   │     APPLIED     （CAS 赢 + Identity/Grant 建立）→ COMMIT
   │     ALREADY     （CAS 输，但回读命中既有授权）→ 什么都不做（继续管线）
   └─ runtime.execute(inbound)             （七段完全不变）
```

- 注入方式：`Worker` 新增一个**可选** collaborator（缺省 None = 当前行为），
  与既有「依赖全部构造注入」纪律一致（`worker.py:45-60`）；
- 组合根（per-tenant Runtime 装配）决定是否注入 → **Arm 能力可按租户/阶段开关**。

### 18.3 Runtime pipeline 是否完全不变？**是。**

```
RUNTIME_EXECUTION_ORDER 仍为 7 段（identity_resolution → authorization → routing →
  session_resolution → session_creation → agent_dispatch → delivery）
STAGE_* 常量集合不变（7 个）
STAGE_FAILURE_MAP 键集合不变
```

### 18.4 ADR-5 Guard 必须扩展（防止 `bootstrap.py` 成为守卫盲区）

现有守卫范式（`tests/test_step7_3_connection_management_audit.py:69-83`）：

```python
ORCHESTRATION = {
    "identity": {
        "reads": ("uow.users.get_by_id", "uow.channels.get_by_id"),
        "writes": ("uow.identities.create", "uow.identities.update"),
        "must_not_write": ("uow.users.create", "uow.channels.create"),
        "must_not_construct": ("User", "Channel"),
    }, ...
}
```

要求扩展为（CP-4 实现期）：

```
G-1  白名单新增唯一运行时创建点：app/application/bootstrap.py
       允许写：uow.bootstrap_arms.*、uow.identities.create、uow.connection_conversations.create
G-2  黑名单保持（并新增 bootstrap 模块路径）：
       identity_resolution / access_authorization / routing /
       session_resolution / session_creation / agent_dispatch / delivery / coordinator
       ⇒ 这些模块中 **零** identities.create / connection_conversations.create
G-3  「运行时创建点唯一」机器化：全仓（app/ 下，排除 app/api/management/** 与 tests/）
       `uow.identities.create` / `uow.connection_conversations.create` 的出现点
       == {app/application/bootstrap.py, app/application/identities.py,
           app/application/connection_conversations.py}
G-4  Bootstrap 不在 RUNTIME_EXECUTION_ORDER 内；RUNTIME_EXECUTION_ORDER 长度/集合不变
G-5  Bootstrap 不新增 STAGE_* / 不新增 failure_code（§19）
```

---

## 19. Failure Semantics（本轮不新增 `STAGE_*`）

### 19.1 映射表

| 失败 | 处理 | 落到既有 |
|---|---|---|
| Trust Gate failure | CP-2：403、**零 Interaction**、零 Identity/Grant、不入 Bootstrap | HTTP 403（不入 Runtime Failure Model） |
| Arm not found | **no-op**（不消费、不创建）→ 管线照常 | `IDENTITY_RESOLUTION_FAILED`（既有） |
| Arm expired | 消费 SQL 谓词落空 → no-op | `IDENTITY_RESOLUTION_FAILED` |
| Arm already consumed | CAS 落空 → **回读**复用既有授权 → 继续管线；回读未命中 → Fail Closed | `IDENTITY_RESOLUTION_FAILED` |
| Scope mismatch | no-op | `IDENTITY_RESOLUTION_FAILED` |
| Identity bootstrap failure | 事务**回滚**（Arm 恢复 ARMED）→ 无 Identity 残留 | `IDENTITY_RESOLUTION_FAILED` |
| Grant bootstrap failure | 同上（Identity+Grant 同事务 → 无半成品） | `IDENTITY_RESOLUTION_FAILED` |
| Transaction conflict（Arm CAS 输） | 回读分支（§11.2） | 命中→继续；未命中→`IDENTITY_RESOLUTION_FAILED` |
| Identity/Grant UNIQUE 冲突 | find-or-create 回读复用（CP-4 M1/M2） | 无失败（正常继续） |
| Session conflict | 既有（CP-4 M3 修） | `SESSION_RESOLUTION_FAILED`（既有，非本轮引入） |
| Agent failure | Arm 已消费、授权已建立（§10.3） | `AGENT_DISPATCH_FAILED`（retryable=True，既有） |
| Delivery failure | 同上 | `CHANNEL_DELIVERY_FAILED`（既有） |

### 19.2 归类（用户要求）

- **回落 `IDENTITY_RESOLUTION_FAILED`**：Arm not found / expired / consumed / scope mismatch /
  Identity 或 Grant 建立失败（含事务回滚）。理由：这些情形的**可观测事实**就是
  "这条入站没有可用身份"——与今天完全一致，**不得**为此新增失败码。
- **属于 Authorization failure**：**无**。Authorization 阶段照常执行；
  若 Bootstrap 已建立 Grant 而 Routing 失败（Connection inactive）→ `ROUTING_FAILED`
  （与 Package 2-B §B6 一致，`routing.py:84-88`）。
- **属于既有基础设施错误**：Agent / Delivery / Session（同上表，全部既有映射）。
- **不得出现**：`UNEXPECTED_FAILURE` 作为 Bootstrap 的"默认归宿"
  —— 必须显式回落到上表的既有码（`interaction_recording.py:104-118` 未注册异常会退化成
  `UNEXPECTED_FAILURE`，那会**丢掉"这是身份/授权未建立"这一事实**）。

### 19.3 本轮不新增

```
❌ 新 STAGE_* 常量      ❌ 新 failure_code
❌ 新 ApplicationError   ❌ 新 Runtime 阶段
```

---

## 20. CP-4 Boundary（本轮只定义边界，CP-4 实现）

CP-4 需要实现与验证：

| # | 项 | 说明 |
|---|---|---|
| 1 | **M1** Identity find-or-create | `create` → `IntegrityError` → `find_by_channel_and_external_id` 回读复用 |
| 2 | **M2** Grant find-or-create | `create` → `IntegrityError` → `find_by_binding` 回读复用 |
| 3 | **M3** Session 冲突回读 | `SessionCreationExecutor` 唯一约束冲突 → `find_active` 回读复用（既有缺陷，CP-1 §6.3） |
| 4 | **Arm CAS** | 条件 UPDATE + `rowcount == 1`；赢家/输家路径（§11） |
| 5 | **N 条并发首条消息** | 恰好 1 条 Identity + 1 条 Grant；N 条 interaction 全部到达终态（不得出现 `SESSION_RESOLUTION_FAILED`） |
| 6 | **replay** | 同 `external_interaction_id` 重推 → 不二次 Bootstrap、不二次 Agent（既有 12.9.4.9 + CAS） |
| 7 | **worker reclaim** | 崩溃 → stale reclaim → 重放 → **不新增** Identity/Grant/Arm 消费（find-or-create 命中） |
| 8 | **multi-instance** | 至少以「两个 tenant-scoped UoW 并发」模拟；PG 下条件 UPDATE 串行化 |
| 9 | **TTL / 过期** | 过期 Arm → 零授权写入；消费 SQL 用 DB 时钟 |
| 10 | **Supersede** | 重复 Verify → 至多一条 ARMED；旧行 REVOKED 且可审计 |
| 11 | **Trust Gate 前提** | 复用 CP-2 的矩阵测试；**Arm 存在但未过闸门 → 零授权写入** |
| 12 | **Guard（G-1~G-5）** | §18.4 的机器化守卫 |

---

## 21. Open Decisions（需下一 Gate 裁决）

| # | 决策 | 为什么必须裁决 | 建议 |
|---|---|---|---|
| **O-1** | 部分唯一索引 `UNIQUE(tenant,channel,connection) WHERE status='armed'` vs 同事务 revoke-then-insert | 决定"至多一条 ARMED"是 **DB 保证**还是**应用层纪律**；涉及方言支持与迁移写法 | 首选部分唯一索引；若摩擦大，退化为事务式并显式登记为"应用层保证" |
| **O-2** | **新增窄表需 Cloud-0.1.5 显式豁免** | CP-1 §10.3 已登记："新增极窄持久列/表 属新状态列/实体，与 Cloud-0.1.5『不为 Gate 新增状态列』精神需显式豁免" | 需一个显式裁决文件（不改 Cloud-0.1.5 冻结本身，只登记豁免与理由） |
| **O-3** | **Arm 的操作入口面**（新增管理面端点？复用既有连接管理动作？） | CP-1 §12.4 N4 已把"是否新增 HTTP 端点"列为独立裁决；本轮不改 API | 建议：复用既有管理面注入纪律的最小动作面；**是否新增端点在实现前裁决** |
| **O-4** | **OSS / `AUTH_REQUIRED=False` 路径下的 initiator** | Arm 的合法性依赖"可归属"；无浏览器会话时 `initiator_user_id` 无来源 | 建议：Arm **要求**已认证操作者；OSS 单租户路径需显式裁决（拒绝 arm / 引入 bootstrap operator / 强制 AUTH_REQUIRED） |
| **O-5** | **Group conversation 是否可被 Arm 消费**（`chatid`） | S-1 的 residual risk 在群聊下放大（任意群内首个 @Bot 即可能获得授权） | 建议：允许（与 Grant 作用域一致），但**必须在 UI/文档显式提示该 scope 是"下一个未知会话"**；若要收紧需独立裁决 |

（S-2 加固见 §15，已定为**独立后续 Gate**，不计入本轮 O 列表。）

---

## 22. Final Gate

```
CONDITIONAL PASS
```

### 22.1 为什么不是 BLOCKED

本模型在当前 ACP 架构下**可以安全成立**，每一项关键机制都有现成落点：

| 需要的能力 | 现有落点 |
|---|---|
| 原子 one-shot | `claim_processing` 的条件 UPDATE + `rowcount`（`interaction_record.py:41-67`） |
| 跨实例一致的 TTL | DB 时钟 + 只传间隔（`interaction_record.py:139-151, 177-193`） |
| 租户隔离 + 防 scope 错配 | tenant 自有列 + RLS + 复合 FK（Grant 已验证，`models/connection_conversation.py:59-70`） |
| Identity 幂等 | UNIQUE(tenant, channel, external_id)（`models/identity.py:30-35`） |
| Grant 幂等 | 四元组 UNIQUE（`:59-63`） |
| 未绑定身份可用 | `user_id=None` 已验证（`tests/test_step9_2_identity_execution.py:91-96`） |
| 事务形状 | 短事务 COMMIT → 无事务执行（`worker.py:74-86`） |
| Trust Gate 前提 | CP-2 已实现并机器化 |
| initiator 事实 | `AuthenticatedUser`（`core/browser_auth.py:31-40`） |

### 22.2 为什么不是 PASS

存在 **5 项必须先裁决的架构选择**（§21 O-1 ~ O-5），其中：

- **O-2**（新增窄表需 Cloud-0.1.5 豁免）与 **O-3**（Arm 入口面）直接决定实现能否开工；
- **O-4**（无鉴权路径下的 initiator）触及 CP-1 冻结的 P4（可归属）；
- **O-1 / O-5** 影响一致性与风险边界。

它们都不是"尚未实现"，而是**需要显式架构裁决**——按本轮 Gate 定义，这正落在 CONDITIONAL PASS。

### 22.3 满足 PASS 的前置条件

```
A-1  O-1 ~ O-5 全部有书面裁决（可以是简短裁决记录，不必是本文件的修订）
A-2  CP-2 修复已部署（否则线上仍是 CP-2 前代码 —— 见 CP-2 报告 §9 D-6）
A-3  CP-4 的 M1/M2/M3 与 Arm CAS 一并实现（§20）
```

---

## 附：本轮未做的事（只读声明）

未创建 migration / 未创建 `bootstrap_arms` 表 / 未写任何 Bootstrap 代码；
未修改 Identity / Grant / Session / Runtime / ADR / Package 2-C / Gate 2.6；
未部署 Cloud / 未创建生产 Arm / 未创建测试数据；未 commit / tag；
未连线生产环境（本轮不涉及 V-3）。

本文件为**只读设计审计产出**，**不构成实现批准**；
实现批准以 §21 的 O-1 ~ O-5 裁决与 CP-4 验收结论为前置。
