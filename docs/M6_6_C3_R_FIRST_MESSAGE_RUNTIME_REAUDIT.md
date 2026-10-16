# M6.6-C3-R — First Real Message Runtime Semantics Re-Audit

> 性质：**READ-ONLY 审计**。未改代码、未改 DB、未改 migration、未改配置、未改测试、
> 未创建/删除任何数据、未 commit、未 tag、未执行任何写入型验证（不跑会写 SQLite /
> PostgreSQL / local_outbox 的检查）。
>
> 本轮唯一产出物 = 本文件（`docs/M6_6_C3_R_FIRST_MESSAGE_RUNTIME_REAUDIT.md`）；
> 所有结论均来自源码 / ORM / migration 语义 / 既有冻结文档的静态阅读。
>
> 审计问题：**能否安全地改变 Runtime 语义，使「首次真实 WeCom 消息在满足明确
> bootstrap 条件时自动完成 Identity + Grant Bootstrap，并继续处理当前 Interaction，
> 最终让 Agent 回复第一条消息」？**
>
> 结论先行：**CONDITIONAL PASS** —— 技术上可行、且不需要绕过 Authorization；
> 但存在 4 条**硬前置条件**（其中 1 条是当前架构下真实存在的未授权旁路，必须先关闭），
> 且「Bootstrap 的授权前提」不能是「这条消息本身」，必须是**一次显式、可归属、
> 有界、租户内、通道内**的运维验证动作，否则 C 退化为 A（必须 BLOCKED）。

---

## 1. Executive Summary

| # | 问题 | 结论 |
|---|---|---|
| 1 | 历史上有明确 Security risk？ | **有**，但**不在** Bootstrap 机制本身，而在**触发前提**：若「陌生身份的第一条消息」即可触发自动建 Identity + 建 Grant，则与已冻结红线正面冲突（`ADR-5`「❌ 首条消息自动创建 ACP User / Identity」、Package 2-C「❌ 不得「首次收到消息即写入授权」」） |
| 2 | Tenant isolation 风险？ | **无结构性风险**。Bootstrap 若运行在 per-tenant Runtime 的 tenant-scoped UoW 内，`identities.tenant_id` 是自有列 + RLS 谓词列；`connection_conversations` 的**复合 FK** 在 DB 层同时锁死跨租户与同租户跨 channel 错配 |
| 3 | Authorization bypass？ | **不需要**。Bootstrap 的语义是「在冻结管线**之前**建立授权事实」，随后 Authorization 阶段**照常执行**并读到非空许可集合 |
| 4 | Race condition？ | **存在 3 处必须处理的并发面**（Identity 插、Grant 插、**Session 插**）；其中 Session 插是**当前就存在的缺陷**（会被 Bootstrap 放大成用户可见失败） |
| 5 | Duplicate Identity / Grant？ | **不会**（tenant-leading UNIQUE 硬约束），但**必须捕获 IntegrityError → 回读复用**，否则并发首条消息会落 `UNEXPECTED_FAILURE` |
| 6 | Replay / duplicate delivery？ | 渠道重推**不会**重复执行（12.9.4.9 状态感知接纳 + CAS claim）；**但**「崩溃后 stale reclaim」存在 at-least-once 执行，用户可见重复投递由**渠道侧一次性回复句柄**兜住 |
| 7 | Transaction inconsistency？ | **可避免**。Bootstrap 必须是**独立短事务（Identity + Grant 同事务）→ COMMIT → 才继续管线**；外部 Agent 调用**不在**任何事务内（既有 dispatch 已在 UoW 退出后才调 Adapter） |
| 8 | Runtime boundary violation？ | **可通过「执行边界上的独立 Bootstrap 组件」避免**；**不应**把创建塞进 IdentityResolver / Authorization / 新增 `RUNTIME_EXECUTION_ORDER` 阶段 |
| 9 | WeCom 第一条消息能否被回复？ | **能**。ACK 与 Agent 回复早已结构性分离；回复句柄（`response_url` / `bot_reply.req_id`）经快照 → Worker 重建 → `OutboundResponse.metadata` 直达 Adapter；`aibot_respond_msg` 回复窗口 **24h** |
| 10 | C 是否技术可行？ | **可行**（CONDITIONAL），最大风险是**授权语义漂移**（default-deny → 首次接触可建立 allow）与**未授权旁路放大**（见 §9.4 / §14 R-1、R-2） |

**一句话**：Fail Closed **不是不可突破的安全边界，但它是「授权必须显式成立」这一原则的实现**。
只要 Bootstrap 的**授权前提**是「运维显式启动的有界验证窗口」而不是「陌生人发了一条消息」，
C 就是「Authorization establishment」而非「Authorization bypass」——这正是本审计要区分的那条线。

---

## 2. Current Runtime Semantics

### 2.1 执行顺序（冻结）

`RUNTIME_EXECUTION_ORDER`（`app/application/runtime.py:78-86`）：

```
identity_resolution → authorization → routing → session_resolution
   → [session_creation?（仅 CREATE_NEW）] → agent_dispatch → delivery
```

`RuntimeCoordinator.execute`（`app/application/coordinator.py:93-181`）逐段消费：

| 段 | 输入 | 失败即 STOP 的 typed error |
|---|---|---|
| 1 Identity | `(channel_id, external_identity_id)` | `IdentityResolutionFailedError`（Identity 未命中） |
| 2 Authorization | `(channel_id, external_conversation_id)` | `AccessAuthorizationDeniedError`（许可集合为空） |
| 3 Routing | `channel_id` + **已授权候选集合** | `RoutingFailedError`（eligible ≠ 1） |
| 4 Session Resolution | `(identity_id, connection_id, external_conversation_id)` | `SessionResolutionFailedError` |
| 5 Session Creation（条件） | 同上 + `agent_id` | `SessionResolutionFailedError` |
| 6 Dispatch | RoutingResult + CP session id + content | `AgentDispatchFailedError` / `AgentResponseInvalidError` |
| 7 Delivery | OutboundResponse | `ChannelDeliveryFailedError`（processing 仍 SUCCEEDED） |

### 2.2 入站 → 执行的两条触发路径

```
HTTP webhook / local message（app/api/runtime/channels.py:105-168）
   accept_message（验签→租户→状态感知去重→PENDING+COMMIT）
     → HTTP 200 ACK（ACK ≠ 执行，12.9.4.6）
       → BackgroundTasks → worker_for(Scope).process(id)          ← Trigger 1

WeCom Bot WS（main.py:419-425）
   帧 → accept_message（同上，同一入口）→ 同一租户 worker.process(id)  ← 同一 Trigger 1
   （在 exec 线程内**同步**执行，见 §9.1）

TenantFanoutDriver（main.py:354-357）每轮 find_pending → worker.process   ← Trigger 2
```

两条触发路径**汇合到同一个 `Worker.process`**，执行权由 `claim_processing` 的 CAS 唯一裁决
（`app/persistence/repositories/interaction_record.py:41-67`）。

### 2.3 Worker 的相位与事务边界（关键）

`Worker.process`（`app/application/worker.py:62-87`）：

```
读 interaction 快照（纯读）
  → claim_processing（CAS，独立短事务，COMMIT）     ← 取得执行权
  → 读回 processing_started_at（attempt fence 值）
  → runtime.execute(inbound)                        ← **不在任何事务内**
  → record_success / record_failure（独立短事务，带 fence 等式）
```

因此**「长事务裹住执行」在当前架构中已经不存在**：
`PersistingCoordinator` 的每个 phase 各自 `with uow`（`interaction_recording.py:168-175, 237-271, 275-284, 288-344, 346-397`），
`AgentDispatchExecutor` 也在 `with self._uow_factory()` **退出之后**才调用 Adapter
（`app/application/agent_dispatch.py:94-125`，注释明写「UoW 已退出，外部 I/O 不占事务」）。
→ §7 要的「Bootstrap COMMIT → Agent 执行 → Delivery 且外部调用不进事务」**在现有形状上天然成立**。

### 2.4 失败语义（唯一映射点）

`STAGE_FAILURE_MAP`（`interaction_recording.py:104-118`）当前**没有** bootstrap 阶段：

```
IDENTITY_RESOLUTION_FAILED (identity_resolution, retryable=False)
UNAUTHORIZED               (authorization,       retryable=False)
ROUTING_FAILED             (routing,             retryable=False)
SESSION_RESOLUTION_FAILED  (session_resolution,  retryable=False)
AGENT_DISPATCH_FAILED      (agent_dispatch,      retryable=True)
AGENT_RESPONSE_INVALID     (agent_dispatch,      retryable=False)
CHANNEL_DELIVERY_FAILED    (delivery,            retryable=True/False)
未注册异常 → (None, UNEXPECTED_FAILURE, False)
```

`STAGE_*` 常量集合在 `app/domain/activity_record.py:18-26` 冻结（7 个）。
→ **最小改动含义**：Bootstrap 失败**不应**新增阶段常量，而应**回落到既有 Fail Closed 路径**（见 §12）。

### 2.5 当前首条真实消息的真实结局

```
首条 WeCom 消息
  → 验签通过 → 租户解析 → PENDING + COMMIT → ACK 200
  → Worker: claim → Identity Resolution → **Identity 未命中 → STOP**
  → interaction 终态 processing=FAILED / delivery=PENDING
    failure_stage=identity_resolution / failure_code=IDENTITY_RESOLUTION_FAILED
  → **Agent 从未被调用，无任何投递**
```

**但第 1 条消息并非无用**：`request_payload` 里已经落库了
`external_identity_id`（= WeCom userid）与 `external_conversation_id`（= 单聊 userid / 群聊 chatid）
（`app/domain/interaction_snapshot.py:42-68`，`app/application/interaction_recording.py:258-266`），
这正是既有文档登记的 **bootstrap discovery** 路径
（`docs/architecture/implementation-package-2-step-7-real-wecom-e2e-runbook.md:137-157`）。

---

## 3. Why Fail Closed Exists

Fail Closed 在当前架构里**不是**一条孤立的安全策略，而是三条已冻结原则在 Identity/Authorization 两段的落地：

| 原则 | 冻结处 | 在代码中的落点 |
|---|---|---|
| **ADR-5** Authorization 不自动创建 ACP User | `docs/architecture/authorization-and-access-model.md:33` | `identity_resolution.py:28-30,66-67`：未命中 → raise，**绝不创建** |
| **ADR-5 红线**「❌ 首条消息自动创建 ACP User / Identity」 | `authorization-and-access-model.md:650` | 同上（运行时零创建，全仓唯一创建路径是管理面 `IdentityApplicationService.create_identity`） |
| **ADR-6** Discovery ≠ Authorization | `authorization-and-access-model.md:34,307-346` | `access_authorization.py:55-70`：`granted == ∅ ⇔ DENY`；Package 2-C 红线「❌ 不得「首次收到消息即写入授权」」（`implementation-package-2-c-…-freeze.md:72`） |
| **ADR-7 / ADR-8** 授权在 Session 之前、Routing 之前 | `authorization-and-access-model.md:35-36` | `runtime.py:78-86` 的顺序常量 |

**Gate 2.6 已经明确把「自动创建策略」列为未裁决**，并且给了两条红线
（`docs/architecture/implementation-gate-2-6-external-identity-semantic-decision.md:180-217`）：

```
D4 — Lifecycle
  Identity exists  ≠  Identity is automatically created
  ❌ 不得以「Identity 可独立存在」为由，在任何运行路径引入自动创建
  ❌ 不得在本 Gate 顺手定义「首次入站即建立 Identity」的策略
  明确未裁决（留给后续独立 Gate）：
    ? 谁有权创建「未绑定 Identity」？（管理面显式动作 / 未来 Discovery 动作 / 两者）
    ? Discovery 是否需要一个显式触发点？（若需要，它属于哪个边界）
    ? 未绑定 Identity 的创建是否需要审计要求？
```

**这是本审计最重要的一条上下文**：C 方案要求的正是 D4 遗留的那三个问题，
因此 **C 不是一个「实现细节」，而是一次需要显式冻结的语义裁决**。
反过来说：D4 **没有**禁止它，只是**没有裁决**它——这决定了最终 Gate 是
「CONDITIONAL PASS（需显式裁决 + 前置条件）」而不是「符合现有冻结」。

补充：Gate 2.5 §E.3（`implementation-gate-2-5-external-identity-lifecycle-audit.md:192-200`）记录了一条硬约束——
**`identities` 没有 RLS bootstrap 策略**，因此 Identity 的读写**必须**发生在 `app.tenant_id` 已设置的
tenant-scoped 上下文内。当前入站路径天然满足（验签 → 已认证 Channel → tenant → tenant-scoped UoW），
**Bootstrap 也必须如此**（见 §5）。

---

## 4. Bootstrap Security Model

### 4.1 三类语义必须区分（R1 的核心）

| 类别 | 语义 | 安全判定 |
|---|---|---|
| **A** 任意陌生外部身份自动 Bootstrap | 「谁发消息谁被建身份 + 建授权」 | **不可接受**。等于把 default-deny 反转成「首次接触即 allow」，直接违反 ADR-5 红线与 Package 2-C 红线；对生产通道而言，第一个陌生人正是你最不该自动授权的人 |
| **B** 处于「首次连接验证 / onboarding context」的 Channel 收到第一条真实消息后 Bootstrap | 触发条件 = **运维显式启动的验证上下文**（有界、可归属）；消息只是**触发器** | **可接受**（需显式裁决）。授权前提是**运维动作**，不是「陌生人的消息」 |
| **C** Tenant Admin 已创建 Channel + Agent + Connection，并主动进入 Verify Connection / First Success | 同 B，但**归属更明确**：具体到「哪条 Connection 正在被验证」 | **可接受**，且比 B 更精确——Bootstrap 的目标对象由运维指定，**不由 Router 推断** |

### 4.2 为什么「virgin channel」不能充当授权前提（必须记录的反例）

一个看似零成本、零新状态的门控是：

```
if channel 尚无任何 Identity 且尚无任何 Grant:
    自动 Bootstrap
```

**必须否决**，理由：

1. **不可区分**：每个生产通道在历史上都曾是「virgin」。这条判据无法区分
   「运维正在验证」与「生产通道被陌生人首次触达」。
2. **后果不可逆**：它会为那次首触**永久写入一条 allow 行**（Grant 不可变，只能删+建），
   使该 Conversation 从此获得该 Connection 的访问权。
3. **与 ADR-6 语义冲突**：「第一次看见」正是 ADR-6 明确禁止当作授权依据的那个事实。

结论：**授权前提必须是「一次显式的、可归属的、有界的运维动作」，不能是从既有数据推导出的隐含状态。**

### 4.3 可接受的授权前提应满足的 5 条属性

| # | 属性 | 含义 | 若缺失的后果 |
|---|---|---|---|
| P1 | **显式（Explicit）** | 由已认证操作者的主动动作创建（不是消息到达推断） | 退化为 A |
| P2 | **有界（Bounded）** | 有 TTL 和/或一次性消费；不允许永久敞开的「首次」 | 等于长期放开自动授权 |
| P3 | **精准作用域（Scoped）** | 绑定 `(tenant, channel, connection)`——运维指明「验证的是哪条通道路径」 | 可能为**别的** channel/connection 建身份/授权 |
| P4 | **可归属 / 可审计（Attributable）** | 留下「谁、何时、为哪条 Connection 启动」的事实 | 事后无法解释「这个 Grant 是谁允许的」 |
| P5 | **不改变机制（No mechanism change）** | 产出的仍是**普通** Identity 行 + **普通** Grant 行 | 引入第二套身份/授权对象 |

P5 是「不引入不必要的新实体」这条原则在 Bootstrap 上的落点：
Bootstrap **只写两种既有对象**，不新增 `PendingIdentity` / `BootstrapGrant` / `Verification` 实体。

### 4.4 Bootstrap 与既有 Grant 模型的一致性（R5 判定：**一致**）

| 既有冻结陈述 | Bootstrap 是否冲突 |
|---|---|
| Grant = Conversation → Connection（`app/domain/connection_conversation.py:3-8`） | **不冲突**：Bootstrap 写的就是一行 Conversation→Connection |
| default deny（缺行 = 不允许）（`connection_conversation.py:14-15`） | **不冲突**：deny 仍由缺行表达；Bootstrap **不写 deny 行**，只在被显式授权的验证窗口内写**一行 allow** |
| 行不可变，改授权 = 删 + 建 | **不冲突**：Bootstrap 只 create |
| Router = active ∩ granted，且 eligible 必须恰好 1（`app/application/routing.py:82-92`） | **不冲突**：Bootstrap 授予**运维指定的那一条** Connection，因此即使该 Channel 有多条 active Connection，`eligible` 仍恰为 1 |
| 授权作用域是 Conversation，不是 Identity/User（ADR-3） | **不冲突**：Bootstrap 建 Identity 与建 Grant 是两件事，Grant 仍只认 `(channel_id, external_conversation_id)` |
| Package 2-C「❌ 不得「首次收到消息即写入授权」」 | **张力点**：literal 冲突。必须由显式裁决改写为「不得由**消息到达**触发写入授权；允许由**运维显式启动的有界验证窗口**触发」 |

**R5 结论**：Bootstrap = **Authorization establishment**，不是 bypass。
判定依据：Bootstrap 完成后，冻结管线（Identity → Authorization → Routing → Session → Dispatch → Delivery）
**一行不改地照常执行**，Authorization 仍然要做「读许可集合 → 空集则 DENY」的判定——
只是这次它读到的集合非空。**不存在任何「跳过某个阶段」的路径。**

---

## 5. Tenant Isolation Analysis（R2）

### 5.1 逐项核对

| 对象 | 是否 tenant-scoped | 证据 |
|---|---|---|
| **Channel** | ✅ | `channels.tenant_id` NOT NULL FK（RLS 谓词列）；管理面读经 tenant-scoped UoW；入站 tenant 取自**已认证 Channel**（`channel_inbound.py:53-68,145-166`）。`find_tenant_id_by_id` 是**刻意的 bootstrap 例外**（`persistence/repositories/channel.py:35-41`），仅用于「定位读」 |
| **Connection** | ✅ | `connections.tenant_id` NOT NULL FK；`find_active_by_channel` 在 tenant-scoped UoW 内 |
| **Agent** | ✅ | `agents.tenant_id`；Dispatch 经 `connections.get_by_id`（tenant-scoped）读取代理 |
| **Identity** | ✅ | `identities.tenant_id` **自有列** NOT NULL FK + RLS 谓词；读路径全部带 `tenant_id` 过滤（`persistence/repositories/identity.py:23-33,61-71`）；创建时 `row.tenant_id = self._tenant_id`（同文件 `35-41`） |
| **Grant（ConnectionConversation）** | ✅ | `connection_conversations.tenant_id` **自有列**（`persistence/models/connection_conversation.py:74-77`，docstring 明写「**不**经 Connection 推导」）；四元组 UNIQUE 以 tenant 为前导列 |

### 5.2 结构性防线（这是本审计中最强的正面证据）

`connection_conversations` 除四元组 UNIQUE 外还有一条**复合 FK**
（`persistence/models/connection_conversation.py:19-27, 65-69`）：

```
FK (tenant_id, channel_id, connection_id) → connections(tenant_id, channel_id, id)
```

它由 DB 结构同时消灭两种错配：
- **跨租户**（grant.tenant_id ≠ connection.tenant_id）；
- **同租户跨 channel**（grant.channel_id ≠ connection.channel_id）——RLS 盖不住，单列 FK 也盖不住。

→ **结论：Bootstrap 不可能跨 Tenant 创建 Grant**：它写的 `(tenant, channel, connection)` 三元组
只要与运维指定的 Connection 不一致，DB 直接拒绝。这是**结构性保证**，不依赖应用层自觉。

### 5.3 `external_conversation_id` 跨租户碰撞

**无碰撞风险**：它是**只在 Channel Scope 内解释**的裸键（`connection_conversation.py:22-24`），
所有唯一约束都以 `tenant_id` 为前导列；同名字符串在两个租户中是两条互不相干的授权。

### 5.4 IDOR / guessed-ID

| 入口 | 现状 | 是否被 Bootstrap 放大 |
|---|---|---|
| 管理面 `POST /api/identities`、`POST /api/connection-conversations` | tenant-scoped，跨租户等价于不存在（404） | 否 |
| 入站 URL 的 `channel_id` | **只是路由定位**；验签通过后 tenant 取自**已认证 Channel**，绝不取自 URL（`channel_inbound.py:53-68`） | 否（Bootstrap 在租户已确定之后） |
| `GET /api/channels/{id}/known-conversations` | 纯读投影，不产生授权 | 否 |
| **`POST /api/channels/{id}/wecom/webhook` 对 `wecom_bot` 渠道** | **当前未授权旁路**（见 §9.4） | **是——被显著放大**（原本只是产生一条失败 interaction；Bootstrap 开启后可能创建 Identity + Grant） |

**必须记录**：`wecom_bot` 的信任模型是「用 `bot_secret` 建立的订阅连接本身」
（`app/adapters/channels/wecom_bot.py:36-43`），其 `parse_message` 把
`signature/timestamp/nonce` 当作**空占位**；而 HTTP webhook 入口同样会把它按渠道类型分派过去。
→ 一个知道自己 channel_id 的匿名调用者可以构造 `aibot_msg_callback` JSON POST 到该端点。
这条旁路**在启用 Bootstrap 之前必须先关闭**（§12 前置条件 CP-2 与 §14 R-2）。

---

## 6. Concurrency Analysis（R3）

### 6.1 场景

```
Interaction A ─┐
Interaction B ─┴─ 同时发现：Identity missing + Grant missing
```

（两条不同的 `external_interaction_id`，同一 `(channel, external_identity, external_conversation)`。）

### 6.2 三道并发面与现状

| # | 竞争点 | DB 硬约束 | 当前代码行为 | 判定 |
|---|---|---|---|---|
| 1 | **Identity 插入** | `UNIQUE(tenant_id, channel_id, external_id)`（`models/identity.py:30-35`） | `IdentityApplicationService.create_identity`：**先预检 → 再 create**，**不捕获 IntegrityError**（`application/identities.py:128-139`） | **并发时一侧会抛 IntegrityError** → 若是运行时路径，将落 `UNEXPECTED_FAILURE`（映射表未注册 IntegrityError） |
| 2 | **Grant 插入** | `UNIQUE(tenant_id, channel_id, external_conversation_id, connection_id)`（`models/connection_conversation.py:60-63`） | 同样是「预检 → create」，**不捕获**（`application/connection_conversations.py:74-99`） | 同上 |
| 3 | **Session 插入** | `UNIQUE(tenant_id, identity_id, connection_id, external_conversation_id)`（`models/session.py:29-34`） | `SessionCreationExecutor.execute` 用 **catch-all** 把任何异常包成 `SessionResolutionFailedError`（`application/session_creation.py:89-97`） | **并发 CREATE_NEW 时一侧会变成 `SESSION_RESOLUTION_FAILED`（用户可见失败）** |

> ⚠️ **文档漂移（本审计发现的既有问题）**：`session_creation.py:26-28` 的 docstring 写
> 「sessions 表当前无唯一约束（5.3 冻结），第一版依赖 `find_active → create` 的事务顺序」——
> 但 ORM 已有 tenant-leading UNIQUE（Cloud-0.1.1-B 加入）。因此「事务顺序」在今天**不再**是唯一防线，
> 而「并发重复 → 整条 interaction 失败」成了新的真实行为。

### 6.3 结论与最小修复边界

**能否保证「N 条并发首条消息 → 恰好一条 Identity + 恰好一条 Grant + 所有合法消息正常继续」？**

- **Identity / Grant：能**（DB 约束已在），**但需要** ORM/Repository 层的
  「**INSERT 冲突 → 回读这条既有行 → 复用**」语义（find-or-create）。
- **「所有合法消息正常继续」：当前不能**，因为第 3 道面会把并发的一侧打成
  `SESSION_RESOLUTION_FAILED`。

**最小修复边界（三处，均为「加兜底」而非「改语义」）**：

```
M1  Identity bootstrap：create → on IntegrityError → find_by_channel_and_external_id → 复用
M2  Grant  bootstrap  ：create → on IntegrityError → find_by_binding → 复用
M3  SessionCreationExecutor：捕获唯一约束冲突 → find_active 回读 → 命中则**复用**（等价 REUSE_ACTIVE），
    未命中才按失败处理
```

M3 是**独立于 Bootstrap 的既有缺陷**（今天也能触发，只是 Bootstrap 会把「两条消息同时走到 session」
的概率显著提高），**但它不是 Bootstrap 引入的**。

补充：`claim_processing` 的 CAS（`interaction_record.py:41-67`）保证**同一条** interaction
无论被 Trigger 1 与 Trigger 2 同时命中几次，**只有一个**执行者。
并发首条消息是**两条不同的** interaction，因此两者都应被正常执行——这正是 M1~M3 要保证的。

---

## 7. Transaction / Failure Analysis（R4）

### 7.1 目标形状（用户问题）

```
DB Bootstrap transaction → COMMIT → Agent execution → Delivery
（不把外部 Agent 调用放进 DB transaction）
```

### 7.2 判定：**现有架构已经支持，且是既有范式**

`Worker.process` 的形状就是「相位 → 独立短事务 → COMMIT → 执行（无事务）→ 相位」：

| 时刻 | 事务状态 | 证据 |
|---|---|---|
| claim | 独立短事务 + COMMIT | `worker.py:74`、`interaction_recording.py:275-284` |
| Runtime execute（含 Agent 调用） | **无事务** | `worker.py:79`；`agent_dispatch.py:94-125`（UoW 先退出） |
| record outcome | 独立短事务 + COMMIT | `interaction_recording.py:288-344/346-397` |

→ Bootstrap 只需**再插一个独立短事务**（位置见 §10），并遵守同一纪律：
**Identity + Grant 必须在同一个 UoW 内提交**（要么都成立，要么都不成立）。

### 7.3 两种失败组合的推演

| 情形 | 结果 | 是否有害 |
|---|---|---|
| **Identity 建成功，Grant 建失败**（同事务） | 事务回滚 → **两者都不存在** → 回落既有 Fail Closed（`IDENTITY_RESOLUTION_FAILED`）→ interaction 终态 FAILED | **无害**（无半成品） |
| **Bootstrap 成功（COMMIT），但后续处理失败**（Routing/Session/Dispatch/Delivery 任一） | Identity + Grant **保留**；interaction 落对应 typed failure（`ROUTING_FAILED` / `SESSION_RESOLUTION_FAILED` / `AGENT_DISPATCH_FAILED` / `CHANNEL_DELIVERY_FAILED`） | **可接受**：供给是「事实」，不是「需要回滚的副作用」。且下一次同会话消息会因 Grant 已存在而正常进入 Agent |
| **Bootstrap 成功但 ROUTING_FAILED**（例：被指定的 Connection 已 inactive） | Grant 是「对该 Conversation 使用该 Connection 的允许」，Connection inactive → `active ∩ granted = ∅` → `ROUTING_FAILED`（**不是** UNAUTHORIZED，符合 Package 2-B §B6 与既有冻结） | **可接受**，且排障方向正确 |

### 7.4 不应做的事

```
❌ 把 Bootstrap 放进 Agent 调用的同一个事务（会把外部 I/O 裹进长事务）
❌ 在 Bootstrap 失败时去「补偿删除」已提交的东西（Identity/Grant 是一次性事实，不应有 saga）
❌ 为 Bootstrap 新增 failure stage / failure code（见 §12：回落既有标识）
```

---

## 8. Replay / Idempotency Analysis（R6）

### 8.1 渠道重推（同 `external_interaction_id`）

`ChannelInboundService.accept_message` 的**状态感知重复接纳**（`channel_inbound.py:43-51, 219-238`）：

```
_load_channel → 验签 → tenant scope → _existing_row_id(channel_id, external_interaction_id, tenant_factory)
    命中 → 返回既有 id：**ACK + 不 create**
    未命中 → create_pending
```

→ 重推**不会**产生第二条 interaction，**不会**触发第二次 Bootstrap，**不会**第二次调用 Agent。

**一个必须说清的实现细节**：HTTP 端点对「命中既有行」的情况**仍会**调度
`worker.process(existing_id)`（`api/runtime/channels.py:128-134`），
但 `claim_processing` 的 CAS 只对 `processing_status='pending'` 的行生效 ——
已终态/已在执行的行 `rowcount=0` → `process` 返回 False → **Runtime 一次都不跑**（G1 安全阀）。
因此「ACK + STOP」是由 **CAS** 兑现的，而不是靠端点分支。

### 8.2 崩溃 + stale reclaim（at-least-once 的真实来源）

```
执行完成并已投递 → 进程在 record_success 之前崩溃
  → 行停在 processing（processing_started_at 已写）
  → Driver 每 N 轮 reclaim_stale_processing(threshold=300s) 把行退回 PENDING
  → Worker 重新 claim → **Agent 被第二次调用** → 第二次 Delivery 尝试
```

- **attempt fence**（`interaction_record.py:91-127`）只保证**旧 attempt** 的 outcome
  不能写穿新 attempt（`OutcomeCommit.REJECTED`），**不阻止重执行**。
- **用户可见重复投递**由**渠道侧一次性句柄**兜住：
  - `response_url`：官方「每个 URL 只能调用一次」，第二次 → `errcode=60140` → `CHANNEL_DELIVERY_FAILED(retryable=False)`（`wecom.py:595-644`）；
  - `aibot_respond_msg`：同一 `req_id` 被消费后重发 → `errcode != 0` → `retryable=False`（`wecom_bot.py:690-697`）。
- 结论：**重复的是「Agent 调用 + 一次注定失败的投递尝试」，不是「用户看到两条回复」**；
  但**成本与副作用**（Agent 侧外部调用）确实会重复。这是**既有**的 at-least-once 特性，
  Bootstrap **不引入也不改变**它，但 Bootstrap 会让「首条消息也进入这条路径」，
  因此**必须与 Bootstrap 一起登记为已知语义**。

### 8.3 相同文本 ≠ 重复

用户连发两条「你好」= 两个不同 `msgid` = 两条合法 interaction = 两次回复。
这不是重复，**不应用去重抹掉**。

### 8.4 幂等的正确实现位置

Bootstrap 的幂等必须**由持久化事实承担**（Identity 行 + Grant 行的存在性），
**不能**用进程内状态（重启即失效）或「只做一次」的布尔量：

```
ensure_bootstrap(inbound, armed):
    identity = find(channel, external_id)  or  (create → on conflict → find)
    grant    = find(binding)               or  (create → on conflict → find)
```

→ 崩溃后重放 = 无副作用（find 命中即复用），这正是 M1/M2 的语义。

---

## 9. WeCom Adapter Analysis（R8）

### 9.1 两个并列形态（`MultiChannelAdapter` 按 `channel.type` 分派）

| 形态 | type | 入站 | 出站 | 信任模型 |
|---|---|---|---|---|
| 自建应用 / 机器人 webhook | `wecom` | HTTP 回调：XML（自建应用）或 JSON（智能机器人），**每请求验签**（SHA1+AES） | `response_url`（机器人，优先）或 `access_token` + `message/send`（自建应用） | 每请求验签 |
| 智能机器人长连接 | `wecom_bot` | WS 帧 `aibot_msg_callback`（无逐帧签名） | `aibot_respond_msg`（**必须经本进程活连接**） | 订阅连接本身 |

### 9.2 逐条回答用户的问题（以当前代码为准）

| 问题 | 答案 | 证据 |
|---|---|---|
| 第一条真实消息到达后**是否已有** outbound response path | **有**（两条形态都有） | `wecom.py:545-593`；`wecom_bot.py:614-699` |
| **ACK 与 Agent response 是否已经分离** | **是，且是结构性分离**。HTTP 侧 ACK = `200`（webhook）或 `{"status":"accepted","interaction_id"}`（local）；Agent 回复**从不**进入该 HTTP 响应 | `channels.py:105-168`；`interaction_recording.py` 的 ACK 边界；`build_passive_reply` 明确**不是** deliver 路径（`wecom.py:271-289`） |
| 是否可能把 Agent 回复**误认为 ACK** | **不可能**（回复走独立通道：`response_url` POST / `respond_msg` 帧） | 同上 |
| 是否可在**同一次 inbound lifecycle** 继续进入 Agent | **可以**：`accept_message`（PENDING+COMMIT）之后即 `worker.process(id)`。webhook/local 经 `BackgroundTasks`（响应之后）；`wecom_bot` 在 exec 线程内**同步**执行 | `channels.py:132-134,165-167`；`main.py:419-425` |
| Delivery 是否可直接用**当前 inbound context** | **可以**：`response_url` / `bot_reply.req_id` 走 `metadata`，经 **快照 → Worker 重建 → Coordinator 合并 → OutboundResponse.metadata → Adapter** | `wecom.py:398-409`、`wecom_bot.py:402-415`；`interaction_snapshot.py:54-68`；`coordinator.py:157-171` |
| **response timing / timeout 限制** | 有，且**分形态**：`aibot_respond_msg` 回复窗口 **24h**；长连接帧携带的 `response_url` 有效期 **≤1h**（+30min 有效 / +60min `errcode=60140`）；`DEFAULT_ACK_WAIT_TIMEOUT_S=10s` **只覆盖「发帧→ACK」**，与 Agent 执行时长**互不叠加** | `wecom_bot.py:95-114, 208`；`wecom.py:119, 605-620` |
| 是否**必须先 ACK 才能异步回复** | **不存在这种限制**（不是「5s 被动回复」模型）：ACK 与回复是两条独立通道；异步执行时长只受上述回复窗口约束 | 同上一格 |
| 是否已有机制避免 response 被误认为 ACK | **有**（ACK 通道与回复通道物理分离） | 同上 |

### 9.3 对「首条消息直接得到 Agent 回复」的结论

✅ **渠道能力侧没有任何阻塞**：首条消息的**回复句柄已被入站解析保证存在**
（缺失即 Fail Closed，绝不产生「无法回复的交互」：`wecom.py:398-400`、`wecom_bot.py:402-406`），
且回复窗口足够长（24h / ≤1h）以容纳异步 Worker 执行。

⚠️ **两个必须记录的既有约束**：

```
W-1  wecom_bot 的 enqueue→执行 是**串行**的（每 binding 一条 exec 线程，`wecom_bot.py:811-829`）：
     一次 Agent 执行会阻塞该 binding 后续帧的消费。首条消息的 Bootstrap 只多一个短事务，影响可忽略；
     但「首条消息」若伴随长执行，会推迟同连接后续消息（既有行为，非 Bootstrap 引入）。

W-2  wecom_bot 的 deliver **必须**在持有活连接的进程内完成（单实例前提，`wecom_bot.py:73-81`）。
     Bootstrap 不改变该前提；多副本部署下首条消息的回复同样会失败（既有约束）。
```

### 9.4 ⚠️ 与 Bootstrap 强相关的**未授权旁路**（必须随 Bootstrap 一起关闭）

```
事实：HTTP 端点 POST /api/channels/{id}/wecom/webhook 只做「按 channel.type 分派」
      （MultiChannelAdapter），而 wecom_bot 的 parse_message 把 signature/timestamp/nonce
      当作空占位、不做任何验签（信任来自订阅连接）→ 一个知道自己 channel_id 的匿名调用者
      可以 POST 一段 aibot_msg_callback JSON，使该 wecom_bot 渠道产生一条真实 Interaction。

当前后果：该 Interaction 在 Identity Resolution 处 Fail Closed（无害，只留一条失败记录）。
Bootstrap 开启后的后果：**若该渠道正处于 arm 窗口内，匿名请求即可触发 Identity + Grant 创建**
       ——即「未认证的授权写入」。这是本审计发现的最高优先级风险（§14 R-2）。
```

**这一条直接决定 Final Gate 必须是 CONDITIONAL 而不是 PASS。**

---

## 10. Runtime Boundary Analysis（R7）

### 10.1 候选位置逐项判定

| 候选位置 | 判定 | 理由 |
|---|---|---|
| **IdentityResolver 内**（`IdentityResolutionExecutor`） | ❌ **不可** | 9.2 已冻结「**绝不创建** Identity/User」，且有 AST 守卫与测试锁定；把创建塞进去会同时破坏「Execution ≠ Resolver」与 ADR-5 |
| **Authorization 层**（`AccessAuthorizationExecutor`） | ❌ **不可** | Package 2 冻结「**不创建任何东西**」——授权执行器只读授权表（`access_authorization.py:20-24`） |
| **SessionResolver 之前**（新阶段插进 `RUNTIME_EXECUTION_ORDER`） | ❌ **不建议** | `RUNTIME_EXECUTION_ORDER` 是冻结常量（9.8.1 + Package 2 Step 4 顺序冻结），新增阶段是**有序变更**，会把「七段」变成「八段」，并污染所有依赖顺序常量的守卫；且 Bootstrap 与 Session 语义无关 |
| **intake（`ChannelInboundService.accept_message`）** | ❌ **不建议** | 它是 ACK 快速路径与「重复一律 ACK+STOP」的落点；在此写库会把供给绑到「接收」而非「执行」，且对「收到但永不执行的交互」也产生供给 |
| **Runtime orchestration 层（Coordinator 内部）** | ⚠️ **次优** | Coordinator 是「只编排、不创建」的纯编排器；在其中创建会让「Coordinator 零 persistence/capability 依赖」这条纪律出现裂缝 |
| ✅ **执行边界上的独立 Bootstrap 组件**（由 Worker/执行入口在 `runtime.execute(inbound)` **之前**调用） | ✅ **最合理** | ① 保持七段管线**逐字节不变**；② 复用**已有范式**——`PersistingCoordinator` 就是「在执行边界上、同一 Protocol 形状」的包装（`interaction_recording.py:152-217`）；③ 语义干净：**Bootstrap 是管线的前置条件，不是管线中的一段**；④ 失败可回落既有 Fail Closed（不新增阶段常量） |

### 10.2 推荐形状（不实现，仅判定）

```
Worker.process(id)
   ├─ load snapshot                       （既有）
   ├─ claim_processing（CAS，短事务）      （既有）
   ├─ **Bootstrap.ensure(inbound)**        ← 新增：独立短事务；不满足条件即 no-op
   │     不满足 → 什么都不做 → 管线照常 → Identity 未命中 → 既有 IDENTITY_RESOLUTION_FAILED
   │     满足   → Identity（find-or-create）+ Grant（find-or-create）→ COMMIT
   └─ runtime.execute(inbound)             （**七段完全不变**）
        identity（命中）→ authorization（读到非空）→ routing（eligible=1）
        → session → dispatch → delivery（用同一条 response_url / req_id）
```

**关键性质（逐条对应本审计的 R1~R7）**：

- **零 bypass**：Authorization 阶段仍然执行、仍然可能 DENY（例如 Grant 未随 Bootstrap 建立时）。
- **零管线改动**：`RUNTIME_EXECUTION_ORDER` 不变、七段 Executor 一行不改、`STAGE_*` 集合不变。
- **零新实体**：只写既有 `identities` / `connection_conversations` 行；一致性由既有 UNIQUE + 复合 FK 保证。
- **可回落**：不满足 Bootstrap 条件时行为与今天**完全一致**（这是可验证的等价性断言，见 §13 T-2）。
- **授权前提可归属**：arm 事实必须绑定 `(tenant, channel, connection)` + 操作者 + 时限（§4.3 P1~P5）。

### 10.3 前提事实的存放位置（仅判定，不含实现）

| 选项 | 特点 |
|---|---|
| **Channel.config 内的一个 bootstrap 子块** | **零 schema 变更**（`channels.config` 已是 JSON 列），且 `channels` 既有 tenant 列 + RLS + 既有 bootstrap 读策略；代价：`config` 目前承载 WeCom 凭据且读路径有脱敏逻辑，需确认脱敏/合并语义不会与之冲突 |
| **新增极窄持久列/表** | 语义最清晰、可索引可审计，但属「新状态列/实体」，与 Cloud-0.1.5 冻结的「不为 Gate 新增状态列」精神与「不引入不必要新实体」原则需要显式豁免 |
| **进程内 lease** | 零持久化，但重启即失效、多副本不安全（与 `wecom_bot` 的单实例前提叠加后可接受度更低），不满足 P4（可归属/可审计）的持久化期待 |

> 本审计**不裁决**具体形态（属实现步），只判定：**该前提必须存在、必须有界、必须可归属，
> 且不得从「virgin channel」这类隐含状态推导。**

---

## 11. A/B/C Technical Comparison

> 按要求：**不做产品价值排名**，只列技术维度。

| 维度 | **A** 保持 Fail Closed（第二条消息完成 First Success） | **B** 首条 → Bootstrap → 系统提示 → 第二条 → Agent | **C** 首条 → Bootstrap → 继续当前 Interaction → Agent → 首条即得回复 |
|---|---|---|---|
| **Security** | ✅ 无任何新增面；与全部冻结红线一致 | ⚠️ 需定义 arm 前提；若前提为「virgin channel」则等同 A 的反面（不可接受） | ⚠️ 同 B，且**放大 §9.4 旁路**（未认证请求可触发授权写入）→ 必须先关闭该旁路 |
| **Authorization** | ✅ 不变（default-deny 严格成立） | ⚠️ 需把红线改写为「不得由**消息到达**触发；允许由**运维有界窗口**触发」 | ⚠️ 同 B，且必须在**管线之前**完成（否则 Authorization 已在同一次执行中判过 DENY） |
| **Complexity** | ✅ 0（今天已是此行为） | 中：需 1 个 arm 事实 + 1 个 provisioning 路径 + 1 个提示状态（前端） | 中：与 B 相同 + 「同一次执行继续」→ 复用既有 ACK/执行分离，**不额外增加运行时复杂度**（关键：C 不比 B 更复杂） |
| **Runtime impact** | ✅ 0 | 小（Bootstrap 可放在执行边界；管线仍会先跑一次失败） | **小**（同一位置；因为「继续」本就是既有行为——ACK≠执行，执行本来就在这条消息的生命周期内） |
| **Data consistency** | ✅ 不变 | ✅ 依赖既有 UNIQUE + 复合 FK；仍需 M1~M2 的 find-or-create | ✅ 同 B（B/C 的数据面**完全相同**；差别只在「第二条消息」是否必需） |
| **UX consequence** | ❌ 用户需要发第二条消息；首次体验出现一次「无回复」 | ⚠️ 仍需第二条消息（只是被系统解释了） | ✅ **首条即得回复**（唯一满足目标的方案） |
| **Migration requirement** | ✅ 无 | 取决于 arm 事实的存放形态：若用 `Channel.config` → **0 migration**；若用新列 → 1 次 migration | 同 B |
| **Test requirement** | ✅ 既有测试已覆盖 | 需新增：arm 有界性、跨租户/跨 channel 负例、并发、回放 | 与 B 相同 **+** 「首条消息 E2E 回复」+「不 arm 时与 A 逐字节等价」+「旁路关闭」回归 |

### 11.1 C 是否在当前架构下技术可行？

**✅ 可行。** 逐条给出可行性依据：

| 需求 | 现有能力是否足够 |
|---|---|
| 首条消息能被回复 | ✅ 回复句柄由入站解析保证；24h/≤1h 窗口足够异步执行（§9） |
| 供给对象（Identity/Grant）可由既有对象表达 | ✅ Identity 允许 `user_id=NULL`（Package 1 已落地）；Grant 是既有 allow 行 |
| 供给不跨租户 | ✅ per-tenant Runtime UoW + RLS + 复合 FK |
| 供给后管线照常 | ✅ 七段无需改动；Bootstrap 只是前置事实 |
| 失败可回落 | ✅ 不满足条件即 no-op → 与今天完全等价 |
| 并发安全 | ⚠️ 需 M1~M2（find-or-create）；M3（session 竞争）是既有缺陷 |
| 事务边界 | ✅ 「短事务 COMMIT → 执行 → 投递」已是既有范式 |

### 11.2 如果可行，最大的风险是什么？

**最大风险 = 授权语义漂移（不是实现 bug）。**

具体形态：arm 窗口若无界/不可归属/不精准作用域，C 会静默退化为 A ——
「首次接触即建立 allow」成为事实上的默认策略，而这一点**没有任何自动化守卫能察觉**
（系统仍然「有 Grant 才 ALLOW」，一切测试都会绿）。

次大风险 = **§9.4 的未认证旁路被放大**：Bootstrap 之前它只产生一条失败记录；
Bootstrap 之后它可能产生一条**由匿名请求触发的授权行**。

### 11.3 如果不可行，阻断它的真正技术原因是什么？

**不是**「当前代码没实现」（那是 Not Implemented）。
真正会构成架构性阻断的只有一种情形：

> **如果产品拒绝承认「运维显式验证动作」是一个合法的授权来源**
> （即坚持「所有 Grant 只能由管理面 API 显式创建，且不接受任何运行时触发」），
> 那么 C **在语义上不成立**（此时它不是 Tech Unsafe，而是 **Architecturally Invalid**）。

除此之外，**没有任何结构约束阻止 C**：
Identity 可空（已落地）、Session 不依赖 User（Gate 2.5 C.2 已证）、
RLS/租户不依赖 User（Gate 2.5 D/E 已证）、回复通道已就绪（§9）。

---

## 12. Minimal Architecture Change

> **最小 Runtime 语义变化**（不是最小代码量）：只改「供给的触发前提与落点」，
> **不改授权机制、不改管线、不新增失败码、不新增实体**。

### 12.1 必须新增的 1 个语义（唯一实质性语义变化）

```
新增语义：
  「首次连接验证」是一次**显式的、有界的、可归属的**运维动作；
  在该窗口内到达的、且属于被指定 (tenant, channel, connection) 的第一条
  真实入站消息，构成**合法的 Bootstrap 触发**：
      → 建立一条未绑定（user_id=NULL）的 Channel-scoped External Identity
      → 建立一条 Grant（该 Conversation → 被指定的 Connection）
      → 随后**照常**进入冻结的七段管线。
```

这是 Gate 2.6 D4 遗留给「后续独立 Gate」的那三个问题的**共同答案**，
因此必须以**独立语义裁决**形式落地（不是顺手实现）。

### 12.2 可复用（**完全不需要改变**）

```
✅ Runtime 七段与 RUNTIME_EXECUTION_ORDER（runtime.py:78-86）—— 一行不改
✅ Authorization 语义与 default-deny（access_authorization.py:55-70）
✅ Router「active ∩ granted，eligible 必须为 1」（routing.py:82-92）
✅ Identity 可空（Package 1 已落地）+ Identity Resolution「命中即成功」
✅ Grant = Conversation → Connection 的既有表/唯一键/复合 FK
✅ ACK ≠ 执行 的异步边界（12.9.4.6）与两条触发路径汇合到 CAS claim
✅ 回复句柄通路（response_url / bot_reply.req_id → snapshot → OutboundResponse.metadata）
✅ 「短事务 COMMIT → 无事务执行 → 短事务落终态」的事务范式
✅ 越权防护：tenant-scoped UoW + RLS + 复合 FK + 入站 URL channel_id 仅作定位
```

### 12.3 需要新增（最小集）

```
N1  一个「Bootstrap 前置事实」（arm）：显式、有界（TTL/一次性）、
    绑定 (tenant, channel, connection) + 发起者 + 时间；可审计。
    —— 形态（Channel.config 子块 / 窄列 / 窄表）留待实现步裁决；§10.3 只列权衡。

N2  一个独立的 Bootstrap 组件（执行边界前置，不进入七段）：
    ensure_bootstrap(inbound) → 三态
      NOT_APPLICABLE（无窗口/不匹配）→ 什么都不做 → 管线照常 → 既有 Fail Closed
      APPLIED        → Identity + Grant（同一短事务）→ COMMIT
      ALREADY        → find-or-create 命中 → 直接继续

N3  find-or-create 语义（M1/M2）：Identity 与 Grant 的 INSERT 冲突 → 回读复用。

N4  一个触发/消费面，使运维能「启动验证并知道它被消费了」
    （这是 N1 的输入端；**是否新增 HTTP 端点需单独裁决**，
     因为 Cloud-0.1.5 的 forbidden 列表含「为 Gate 新增后端端点」）。
```

### 12.4 必须同时修复（既有缺陷，独立于 Bootstrap，但被它放大）

```
F1  未授权旁路：wecom_bot 渠道经 HTTP `POST /api/channels/{id}/wecom/webhook` 可被
    无凭据注入 aibot_msg_callback（§9.4）→ 必须 Fail Closed。
    这是**启用 Bootstrap 的硬前置**：否则 Bootstrap = 未认证授权写入面。

F2  SessionCreationExecutor 的唯一约束冲突处理（M3）：
    并发 CREATE_NEW 目前会把一侧打成 SESSION_RESOLUTION_FAILED。
    （顺带修正 session_creation.py:26-28 的「sessions 表当前无唯一约束」这处文档漂移。）

F3  Bootstrap 失败不新增 STAGE_*/failure_code：回落既有
    IDENTITY_RESOLUTION_FAILED（保持 STAGE_FAILURE_MAP 与 STAGE_* 集合不变）。
```

### 12.5 明确不做

```
❌ 不改 RUNTIME_EXECUTION_ORDER / 不加第八段
❌ 不在 IdentityResolver / Authorization / Routing / Session Executor 内创建任何东西
❌ 不新增身份/授权实体（PendingIdentity / BootstrapGrant / Verification …）
❌ 不让 Router 参与「该给哪条 Connection 授权」（目标 Connection 必须由 arm 指定）
❌ 不用「virgin channel」等隐含状态作为授权前提
❌ 不修改 Grant 的作用域语义（仍是 Conversation → Connection）
❌ 不把 Agent 调用放进任何数据库事务
❌ 不新增 failure stage / failure code
❌ 不把「配置成功 / arm 成功」当作 First Success（Cloud-0.1.5 G5 冻结）
```

---

## 13. Required Tests

> 全部为**语义/边界**测试意图（不含实现）。每条都标注它守的是哪条不变量。

**T-1 首条消息 E2E（目标达成）**
arm(channel, connection) → 一条真实入站 → Identity（`user_id=NULL`）+ Grant 各 1 行 →
`processing=succeeded × delivery=delivered` → 投递经同一 inbound 句柄 → 用户首条即得回复。

**T-2 未 arm 时逐字节等价（最重要的一条）**
无 arm 时，首条消息的结果与今天完全相同：
`failure_stage=identity_resolution` / `failure_code=IDENTITY_RESOLUTION_FAILED` /
`retryable=False` / `response_payload` 为 NULL / **Identity 与 Grant 行数均为 0**。

**T-3 arm 的有界性与一次性**
- arm 过期后到达 → 不 Bootstrap（回落 T-2 行为）；
- arm 已被消费后第二条**新**消息（不同 msgid、同会话）→ 不重复 Bootstrap（幂等：不新增行）；
- arm 被撤销 → 不 Bootstrap。

**T-4 作用域负例（P3）**
arm 在 channel A / tenant 1 →
- tenant 2 的同名 conversation → 不 Bootstrap；
- channel B 的消息 → 不 Bootstrap；
- 被 arm 的 Connection 之外不存在任何新 Grant 行（`connection_id` 必须等于被指定值）。

**T-5 归属性（P4）**
Bootstrap 产生的行可回答「谁在何时为哪条 Connection 启动的验证」；
且 arm 记录本身必须 tenant-scoped（跨租户读不到）。

**T-6 并发（N 条首条消息同时到达）**
N 条不同 msgid 的并发首条消息 → **恰好 1 条 Identity + 恰好 1 条 Grant**；
N 条 interaction 全部到达终态（**不允许**任何一条因 session 唯一约束冲突而落 `SESSION_RESOLUTION_FAILED`
——即 T-6 同时是 F2/M3 的验收）。

**T-7 失败原子性**
Grant 建失败（例：被引用的 Connection 不存在/跨 channel）→ 事务回滚 →
**Identity 不得残留**（0 行）→ interaction 落 `IDENTITY_RESOLUTION_FAILED`。

**T-8 重推（Replay）**
Bootstrap 成功后，渠道以**同一 msgid** 重推 →
interaction 行数不变、`process` 调用不产生第二次执行（CAS 返回 False）、
**无第二次 Agent 调用、无第二次投递**。

**T-9 崩溃重放**
Bootstrap 已 COMMIT、执行前进程崩溃 → 行被 reclaim → 重新执行 →
**不新增第二条 Identity / 第二条 Grant**（find-or-create 命中）。

**T-10 边界守卫（机器化）**
- `IdentityResolutionExecutor` / `AccessAuthorizationExecutor` / `RoutingExecutor` /
  `SessionResolutionExecutor` / `SessionCreationExecutor` 源码中**零** Identity/Grant 创建调用；
- `RUNTIME_EXECUTION_ORDER` 长度与元素集合不变；
- `STAGE_*` 常量集合不变、`STAGE_FAILURE_MAP` 键集合不变；
- 全仓仍无「全渠道/通配」授权写入；
- 运行时创建路径**唯一**（Bootstrap 组件），且它在执行边界而非管线内。

**T-11 旁路闭环（F1）**
对 `wecom_bot` 类型渠道，`POST /api/channels/{id}/wecom/webhook` 必须**拒绝**
（不可注入）；且**即使该渠道正在 arm 窗口内**，该端点也不得产生任何 Identity/Grant 行。

**T-12 回复通路不变**
Bootstrap 触发的那次执行，其 `OutboundResponse.metadata` 仍携带原入站句柄
（`response_url` / `bot_reply.req_id`），且投递仍走同一条 Adapter 路径
（不因 Bootstrap 而回退到 `message/send`）。

**T-13 与 Cloud-0.1.5 的一致性**
First Success 仍由 `processing=succeeded × delivery=delivered` 推导；
不得出现 arm/verification 的持久「first_success」状态列被 UI 直接当作 First Success。

---

## 14. Risks

| ID | 风险 | 严重度 | 说明与最小缓解 |
|---|---|---|---|
| **R-1** | **授权语义漂移**：arm 无界/不可归属/作用域过宽 → C 静默退化为 A | **高** | 强制 P1~P5（§4.3）；arm 必须有 TTL/一次性 + `(tenant, channel, connection)` 精准绑定 + 审计留痕；机器化守卫「不存在无界的 arm 查询」 |
| **R-2** | **未认证旁路放大**：`wecom_bot` 渠道经 HTTP webhook 端点可被无凭据注入帧，arm 窗口内将成为**未认证授权写入** | **高** | **启用 Bootstrap 的硬前置**：该端点在 `wecom_bot` 类型上 Fail Closed（T-11）；建议同时复核 HTTP 入口对 `local` 之外的形态是否都应拒绝 |
| **R-3** | 并发首条消息落 `SESSION_RESOLUTION_FAILED` | 中 | M3（F2）：唯一约束冲突 → 回读复用；T-6 验收 |
| **R-4** | IntegrityError 未捕获 → `UNEXPECTED_FAILURE`（丢「这是身份冲突」这一事实） | 中 | M1/M2 find-or-create；不允许让 IntegrityError 冒泡成 `UNEXPECTED_FAILURE` |
| **R-5** | at-least-once：崩溃 + reclaim → 重复 Agent 调用（用户可见重复投递由一次性句柄兜住） | 中 | 既有特性；登记为已知语义（不阻断 C）。若需消除，属独立的「投递幂等 / claim 租约」议题，**不在 Bootstrap 范围** |
| **R-6** | arm 事实的存放形态引入「不必要的新实体/状态列」 | 中 | 优先评估零 schema 方案（`Channel.config` 子块）；若必须新增列，需显式豁免并说明「为何必要」 |
| **R-7** | `response_url` ≤1h 窗口 / `wecom_bot` 单实例前提被误认为「Bootstrap 引入的限制」 | 低 | 属既有渠道约束（§9.3 W-1/W-2），在 C3 设计文档中显式登记，避免误归因 |
| **R-8** | 与既有 bootstrap discovery 运维流程并存造成两套供给路径 | 低 | 明确关系：**运行手册路径 = 事后供给（第二条消息）**；**C = 事前 arm + 首条供给**。二者可共存，但必须只有一个写入实现（N2） |
| **R-9** | Cloud-0.1.5 冻结「不为 Gate 新增后端端点」与 N4 的张力 | 低 | N4 的消费面是否新增端点属独立裁决；若可复用既有管理面动作承载 arm，则无冲突 |

---

## 15. Final Gate

```
CONDITIONAL PASS
```

**判定依据（区分 Not Implemented 与 Unsafe/Invalid）**：
C 目前是 **Not Implemented**（不是 Unsafe、也不是 Invalid）。
现有架构的每一块拼图都已就位：Identity 可空（Package 1 落地）、
Session 不依赖 User（Gate 2.5 C.2 已证）、租户/RLS 不依赖 User（Gate 2.5 D/E 已证）、
ACK 与回复结构性分离、回复窗口足够异步（§9）、
「短事务 → 无事务执行 → 短事务」的事务范式已存在（§7）、
Identity/Grant 的幂等有 DB 硬约束背书（§6）。
**唯一缺的是一个显式、有界、可归属的授权前提，以及 3 处并发/旁路加固。**

### 必须全部满足的前置条件（缺一不可）

```
CP-1  显式语义裁决：把 Package 2-C 的「❌ 不得「首次收到消息即写入授权」」
      改写为「不得由**消息到达**触发写入授权；允许由**运维显式启动的有界验证窗口**触发」，
      并同时回答 Gate 2.6 D4 遗留的三个问题（谁有权创建未绑定 Identity /
      Discovery 的显式触发点属于哪个边界 / 是否需要审计要求）。

CP-2  【硬前置】关闭 wecom_bot 经 HTTP webhook 端点的未认证注入路径（F1 / R-2）。
      未关闭之前，Bootstrap 不得上线。

CP-3  arm 前提满足 P1~P5：显式、有界（TTL 且/或一次性）、
      精准作用域 (tenant, channel, connection)、可归属可审计、只写既有对象。

CP-4  并发与原子性加固：M1/M2（Identity/Grant find-or-create）、
      M3（Session 唯一约束冲突 → 回读复用）、Bootstrap 的 Identity+Grant 同事务提交；
      外部 Agent 调用**不在**任何事务内（既有形状已满足，需机器化守卫）。

CP-5  边界守卫：Bootstrap 位于**执行边界**而非七段管线内；
      RUNTIME_EXECUTION_ORDER / STAGE_* / STAGE_FAILURE_MAP 不变；
      运行时创建点唯一且可审计；不新增失败码（回落 IDENTITY_RESOLUTION_FAILED）。
```

### 如果 CP-1 被拒绝（即拒绝承认「运维验证动作」是合法授权来源）

则本方案**必须判 BLOCKED**，且理由必须写成：
**Architecturally Invalid（授权来源不合法）**，而**不是**「当前代码没实现」。

---

## 附：最终明确回答

> **「SteeringFlow 是否可以让真实 WeCom 第一条消息完成 Bootstrap，继续进入正常 Agent
> Runtime，并让 Agent 直接回复第一条消息？」**

**可以——在 CP-1 ~ CP-5 全部满足的前提下（CONDITIONAL PASS）。**

- **渠道侧没有阻塞**：首条消息必然携带可回复句柄（缺失即入站 Fail Closed），
  `aibot_respond_msg` 回复窗口 24h、`response_url` ≤1h，均足够异步执行；
  ACK 与 Agent 回复是两条独立通道，不存在「必须先 ACK 才能异步回复」的限制，
  也不存在「回复被误认为 ACK」的路径。
- **Runtime 侧没有阻塞**：Bootstrap 只需在**冻结七段之前**建立两条既有事实
  （Identity + Grant），随后 Authorization / Routing / Session / Dispatch / Delivery
  **照常执行**——Bootstrap 是 **Authorization establishment**，不是 **Authorization bypass**。
- **唯一真正的约束是语义性的**：授权前提必须是**运维显式启动的有界验证窗口**
  （否则 C 退化为「陌生人首条消息自动获得授权」，必须 BLOCKED），
  并且必须**先关闭 `wecom_bot` 的未认证 HTTP 注入旁路**——
  否则 Bootstrap 会把一条既有的「无害失败路径」升级为「未认证的授权写入路径」。

**本审计未做的事（只读声明）**：未修改任何生产代码 / DB / migration / 配置 / 测试；
未创建或删除任何数据；未执行任何会写入 SQLite / PostgreSQL / local_outbox 的检查；
未 commit / 未 tag；未设计任何新实体、字段命名、端点或错误码字面量。
本文件为只读审计产出，**不构成实现批准**。
