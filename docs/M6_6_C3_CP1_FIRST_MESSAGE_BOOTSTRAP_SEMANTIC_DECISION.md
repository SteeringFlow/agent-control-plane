# M6.6-C3-CP1 — First Message Bootstrap Semantic Decision

> 性质：**READ-ONLY / ARCHITECTURE DECISION ONLY**。
> 未改代码、未改 DB、未改 migration、未改生产配置、未改测试、未创建/删除数据、
> 未 commit、未 tag；**未修改既有 ADR / 未修改 Package 2-C / 未修改 Gate 2.6**
> （本轮只提出文档变更建议，见 §11）。
>
> 本阶段是 M6.6-C3 First Message Runtime Semantics 的 **ARCHITECTURAL DECISION GATE**。
> 本文件**不重新审计 Runtime**（C3-R 已完成），**不重新证明 C 方案技术可行**
> （C3-R 已判 CONDITIONAL PASS），只裁决一件事：
>
> **Tenant Admin 显式发起的、短时、有界、可审计的「Verify Connection / First Success」
> 动作，是否可以成为首次真实 Channel Message 的合法 Bootstrap 授权来源。**
>
> 裁决主文：**CONDITIONAL PASS** — 答案是 **YES**（该来源合法），
> 但生效前必须先完成 **ADR-12 签发 + 3 项语义 ratify**（§13）。
> 与 ADR-5 / Package 2-C / ADR-6 / Gate 2.6 之间**不存在不可调和的语义冲突** → **不是 BLOCKED**。

---

## 1. Executive Summary

| # | 问题 | 结论 |
|---|---|---|
| 1 | 是否承认 Tenant Admin 显式发起的 Verify Connection 是合法 Bootstrap 授权来源？ | **YES**（§7）——它与 ADR-3「授权作用域 = Conversation」同构：授权的**来源**始终是「一次显式管理动作」，arm 只是把该动作的**适用对象**从「已知会话键」扩展为「该 channel 上第一条未知会话」 |
| 2 | 与 ADR-5 是否冲突？ | **本体不冲突，字面推论冲突**（§3）。ADR-5 本体是「不自动创建 ACP User」——**绝对保持**；冲突项是 ADR-5 §推论（`:296-297`「运行时链路不得出现任何创建 Identity 的调用」）与 ADR §六（`:650`「❌ 首条消息自动创建 ACP User / Identity」），二者必须**显式重新表述**而非删除 |
| 3 | 与 Package 2-C 是否冲突？ | **不冲突**（§4）。Package 2-C `:72` 的「❌ 不得「首次收到消息即写入授权」」在 armed 语义下**仍然成立**：授权来源是 arm（消息之前已存在），消息只是 trigger。但需要一条**精确化补充**，避免未来读者把「arm 兑现」误读为「首次收到消息即授权」 |
| 4 | 与 ADR-6 是否冲突？ | **不冲突**（§5）。Bootstrap **不是** Discovery → Authorization，而是 `Operator Verification → Authorization`；Discovery（会话被观察到）依然不产生授权状态 |
| 5 | Gate 2.6 D4 三问能否回答？ | **能，但答案本身是新裁决**（§6）。D4 明确把三问「留给后续独立 Gate」——CP-1 正是那个 Gate；但落地仍需 ADR 文本 |
| 6 | 授权来源 / 触发 / 效果是什么？ | **来源**=已认证 Tenant Admin 的显式有界 Verify 动作；**触发**=被 arm 的 channel 上第一条通过信任闸门的入站消息；**效果**=创建或复用 1 条 External Identity（`user_id` 允许 NULL）+ 1 条 Grant（§7） |
| 7 | Bootstrap 能否创建 ACP User？ | **不能**（§8） |
| 8 | 是否冻结「message ≠ authorization source, message = trigger」？ | **建议冻结**（§9，措辞见该节） |
| 9 | 最大未决风险是什么？ | **arm 无法预先命名会话键**，因此 arm 语义上等价于「该 channel 上下一条未知会话获得一次有界 allow」→ 存在「第一条消息来自非操作者的陌生人」的**有界竞态**（§8.4 / §12 / §14） |
| 10 | 是否存在机器化守卫冲突？ | **存在一处结构性风险**（§11.4）：`test_adr5_runtime_never_creates_acp_user_or_identity` 只扫描**固定 4 个文件**；若把 Bootstrap 放在该清单之外，就会**通过守卫而实质违反 ADR-5**。本轮明确禁止这种规避，要求**扩展**守卫扫描面 |

**一句话**：本 Gate 承认该授权来源合法（YES），并要求把它写成 **ADR-12（新增编号 + 显式取代关系）**——
因为 ADR 文件自身规定（`:702-703`）**任何修改须以「新增 ADR 编号 + 显式取代关系」的方式提交，
不得原地改写既有 ADR 的语义**。在 ADR-12 签发之前，该来源是 **admissible but not yet lawful**，
故判定 **CONDITIONAL PASS**。

---

## 2. Current Frozen Authorization Principles

> 全部引自真实文件（含行号），不做转述性改写。

| 来源 | 位置 | 冻结内容 |
|---|---|---|
| **ADR 总纲** | `docs/architecture/authorization-and-access-model.md:18-21` | 授权问题只有一个是「这个 Conversation 能不能访问这个 Connection」；**收到消息（Discovery）与允许处理（Authorization）是两件事** |
| **ADR-3** | `:149-207` | Authorization 的 MVP 作用域 = **Conversation**；判定输入 = `(channel_id, external_conversation_id)`；输出 = **许可集合**；`Conversation` **不是实体**，只是 channel-side 会话键 |
| **ADR-5** | `:257-303` | Authorization **不自动创建 ACP User**；「First Message → Resolve → Access Check → ALLOW/DENY」，绝不 `→ Create ACP User` |
| **ADR-5 推论** | `:296-297` | **运行时链路（Identity Resolution / Routing / Session / Dispatch）不得出现任何创建 `Identity` / `User` 的调用** |
| **ADR-5 红线** | `:302-303` | ❌ 无「首条消息自动建 User / Identity」；❌ 无 `IdentityLinker` / `IdentityMatcher` / 自动 linking |
| **ADR-6** | `:307-349` | **Discovery ≠ Authorization**；`:343-344` Discovery 可以发生但**不产生授权状态**；授权默认 = **deny**（缺授权记录 = 不允许），允许必须**显式成立** |
| **ADR-6 红线** | `:348-349` | ❌ **不得存在「首次收到消息即写入授权」的代码路径**；❌ 不得把「Routing 选中」当授权结论 |
| **ADR-7 / ADR-8** | `:353-489` | Authorization 在 Session Resolution 之前、且先于最终 Routing；Router 只消费**已授权 eligible connections**；未授权请求**不创建 Session / 不调用 Agent / 不产生 Delivery** |
| **ADR §六 forbidden** | `:644-658` | ❌ 首条消息自动创建 ACP User / Identity（`:650`）；❌ 把「收到消息」当作授权结论（`:652`） |
| **ADR 修改纪律** | `:702-703` | 本文件为 **FROZEN**：**任何修改须以「新增 ADR 编号 + 显式取代关系」的方式提交，不得原地改写既有 ADR 的语义** |
| **Package 2-C** | `implementation-package-2-c-…-freeze.md:69-73` | 逐条确认 ADR-6 语义继续有效，其中 `:72`「❌ 不得「首次收到消息即写入授权」」✅ **继续有效** |
| **Package 2-C 窄豁免** | `:90-98` | `connection_conversations` = **V0 唯一的授权状态存储**；**不是 ACL / 权限表**（无 effect / 优先级 / principal；作用域 = conversation；deny = 缺行） |
| **Gate 2.6 D4** | `implementation-gate-2-6-…-decision.md:180-217` | `Identity exists ≠ Identity is automatically created`；**本 Gate 不裁决自动创建策略**；三问明确未裁决 |
| **Gate 2.6 D7** | `:279-316` | 「创建/发现 External Identity」与「绑定到 ACP User」是两个不同语义，**必须分开**；实现形态 NOT DECIDED |
| **Package 1（落地现状）** | `app/schemas/identities.py:34-38`、`app/application/identities.py:50-58,115,129-130` | **V0 不暴露「创建未绑定 Identity」的独立 API**；`CreateIdentity.user_id` **保持必填**，且服务层强制 `users.get_by_id(user_id)` 存在性校验 |

### 2.1 关键实现事实（决定本 Gate 的可选空间）

| 事实 | 证据 |
|---|---|
| 运行时**零创建**且 Identity 未命中即 Fail Closed | `app/application/identity_resolution.py`（raise `IdentityResolutionFailedError`） |
| **授权阶段已能容忍 `user_id=NULL` 的 Identity** | `backend/tests/test_package_2_step_2_runtime_authorization.py:371` `test_e1_unbound_external_identity_with_grant_is_allowed`、`:394` `test_e2_…_without_grant_is_unauthorized` |
| 授权判定**只读**授权表、只看 granted 集合 | `:245` `test_b7_executor_reads_only_grants`（禁 `identity_id` / `user_id` / `external_identity_id` 出现在执行器标识符里） |
| **当前全仓没有任何路径能创建 `user_id=NULL` 的 Identity** | 运行时禁止（ADR-5）+ 管理面强制 `user_id` 必填（`identities.py:115,129-130`）→ **Gate 2.6 D4-Q1 的实际答案是「Nobody」** |
| `RUNTIME_EXECUTION_ORDER` 被**精确元组**断言锁定（≥5 处） | `test_cloud_0_1_1_b_tenant_isolation_dod.py:148`、`test_cloud_0_1_1_b_tenant_fanout.py:87`、`test_cloud_0_1_1_a_…:133`、`test_step10_8_4…:386`、`test_step10_1_4…:286` |
| ADR-5 守卫的**扫描面是固定 4 个文件** | `backend/tests/test_adr_authorization_and_access_model.py:284-298` `_RUNTIME_CHAIN_FILES = (identity_resolution.py, routing.py, coordinator.py, runtime.py)` |
| ADR-6 守卫**只断言类不存在**，且其自述预留演进 | `:327-340`（`AuthorizationGrant` / `AutoGrant` / `AuthorizationDecision`；docstring 写「Package 2 / Step 4 修订说明」） |
| 守卫清单本身被 meta-guard 锁定 | `test_package_2_step_4_authorization_contract_closure.py:405-422`（逐条列出必须存在的测试名 → 变更须**增补**不得删改） |
| ADR-11 **只有内容冻结，文件从未创建** | `implementation-package-2-c-…:508`「ADR-11 待签发」、`:515`「✗ ADR-11 文件本身（本 Gate 只冻结其内容）」；`docs/architecture/` 下**无 ADR-11 文件** |

---

## 3. ADR-5 Analysis

### 3.1 逐字对照

ADR-5 本体（`:257-285`）：

```
First Message
    ↓
Unknown External Identity
    ↓
Create ACP User          ❌
    ↓
Authorize
```

**「External Identity 可以一直没有 ACP User。」**

ADR-5 推论（`:296-297`）：

> 运行时链路（Identity Resolution / Routing / Session / Dispatch）**不得**出现任何
> 创建 `Identity` / `User` 的调用。

ADR-5 红线（`:302-303`）：

> ❌ 无「首条消息自动建 User / Identity」。

### 3.2 判定

| 分项 | 是否被 C 方案违反 | 说明 |
|---|---|---|
| **ADR-5 本体**（不自动创建 ACP User） | **❌ 不违反，且必须永久保持** | Bootstrap 只创建 **External Identity**（`user_id=NULL`），**永不创建 ACP User**。ADR-5 本体的措辞恰恰是「External Identity 可以一直没有 ACP User」——Bootstrap 正是这句话的**正向落地**，而非例外 |
| **ADR-5 红线**（无「首条消息自动建 Identity」） | **⚠️ 字面冲突，语义可调和** | 「自动」的判别标准是「**是否有预先存在的显式授权上下文**」（见 3.3）。armed 情形下消息之前已存在 Tenant Admin 建立的 arm；未 armed 情形下**保持原样禁止** |
| **ADR-5 推论**（运行时链路零创建 Identity） | **⚠️ 字面冲突，需重新表述** | 该推论是在「**不存在任何显式授权来源**」前提下写下的实现约束；如今出现了一个显式来源，因此推论必须**加上限定**，而不是被删除 |

### 3.3 「自动创建」与「在已存在的显式 Bootstrap Context 中创建」的语义区别

这是本 Gate 最关键的语义辨析。判别标准必须是**可机器化判别**的，否则它就只是一句修辞。

| 维度 | 「自动创建」（ADR-5 红线所禁） | 「在已存在的显式 Bootstrap Context 中创建」（CP-1 所需） |
|---|---|---|
| **授权来源** | **消息本身**（收到消息 ⇒ 授予） | **运维的显式 arm 动作**（消息到达之前已存在） |
| **发起者** | 无（任何能触达 Bot 的外部参与者，其身份未知） | **已认证 Tenant Admin**（浏览器会话 / 管理面 API） |
| **有界性** | 无界（永久、任意次数、任意会话） | **有界**：短 TTL + 一次性兑现（§8） |
| **作用域** | 无限（该 channel 上所有会话、无限身份） | **精准**：绑定 `(tenant, channel, connection)`；且**至多兑现 1 个 conversation / 1 个 identity** |
| **可归属 / 可审计** | 无（事后无法回答「谁允许的」） | **有**：arm 记录 initiator + 时间 + 目标；兑现记录 arm→identity→grant |
| **未满足条件时的行为** | —— | **回到现状 Fail Closed**（`IDENTITY_RESOLUTION_FAILED`），与今天逐字节等价 |
| **判别问题（可机器化）** | 是否存在「无 arm 的创建路径」 | **消息到达时是否已存在一条有效的、未兑现的、匹配本三元组的 arm** |

### 3.4 结论

- **message-only bootstrap 仍然绝对禁止**（没有 arm ⇒ 行为与今天完全一致）。
- ADR-5 需要的是**一次显式重新表述 + 一条 superseding ADR**，而**不是**削弱 ADR-5 本体。
- ADR-5 的机器化守卫（`test_adr5_runtime_never_creates_acp_user_or_identity`、
  `test_adr5_identity_resolution_still_fails_closed`）**必须被扩展**而非被绕过（§11.4）。

---

## 4. Package 2-C Analysis

### 4.1 逐字对照

Package 2-C `:69-73`（ADR-6 条款关系表）：

| ADR-6 条款 | 与本设计的关系 |
|---|---|
| 「授权默认 = deny（缺授权记录 = 不允许）」 | ✅ 正是本设计所实现的（`granted = ∅ → DENY`，deny 由缺行表达） |
| 「允许必须显式成立」 | ✅ 正是 grant 表要提供的东西 |
| 「Discovery 可以发生…但不产生授权状态」 | ✅ known-conversations 严格遵循（纯读投影，不写授权） |
| **❌ 不得「首次收到消息即写入授权」** | ✅ **继续有效**（回填是**一次性迁移**，不是运行时路径） |
| ❌ 不得把「Routing 选中」当授权结论 | ✅ 继续有效 |

Package 2-C `:90-98`（窄豁免）：

```
APPROVED（窄范围且唯一）：
    connection_conversations
它**不是** ACL / 权限表（无 effect / 优先级 / principal；作用域 = conversation；deny = 缺行）。
它是 V0 **唯一**的授权状态存储。
```

### 4.2 判定：C 方案是否仍然满足「不得「首次收到消息即写入授权」」？

**满足——前提是把该红线的判读对象拆成两件事。** Package 2-C 的这句红线（继承自 ADR-6 `:348`）
所禁止的是「**收到消息这一事实本身构成授权依据**」。它有两种可能的读法，必须显式区分：

| 读法 | 含义 | armed Bootstrap 是否满足 |
|---|---|---|
| **R-1（意图读法，正确）** | **message 不得作为 authorization source** | ✅ **满足**：source = arm（消息之前已存在）；message 只是 trigger |
| **R-2（字面读法，过宽）** | 任何「第一条消息导致授权行出现」的代码路径都禁止 | ⚠️ **不满足**（第一消息确实导致 insert） |

**Package 2-C 与 ADR-6 的上下文支持 R-1**：ADR-6 通篇的论证是「**能被投递 ≠ 被授权**」
（`:337-339`），即禁止的是**把接收能力当作许可依据**；它并未讨论「运维预先授权的兑现时机」
这一情形（该情形在 V0 时期根本不存在，因为当时无任何运行时授权行写入者）。
Package 2-C `:71-72` 的对照表把「Discovery 不产生授权状态」与「不得首次收到消息即写入授权」
并列，同样指向「**观测不得成为许可**」而非「**兑现不得发生在第一条消息上**」。

**因此 C 方案满足 Package 2-C，但 Package 2-C 需要一条「精确化补充」**：
明确「**arm 的兑现**」与「**首次收到消息即写入授权**」是两件不同的事——
前者的授权依据是 arm（预先存在、有界、可归属），后者的授权依据是消息本身。

### 4.3 窄豁免是否被扩张？

**不扩张。** 本方案**不新增任何授权状态存储**：
- Bootstrap 写入的仍是 `connection_conversations` 中**一行普通 Grant**（`create_connection_conversation` 的同一表、同一唯一键、同一语义）；
- arm 本身**不是授权状态行**（它不参与 `find_granted_connection_ids` 判定，授权阶段读到的集合仍只来自 `connection_conversations`）——arm 是「授权建立的授权」（一只一次性许可证），**不是**「授权」；
- 因此 Package 2-C `:97`「V0 **唯一**的授权状态存储」**保持不变**，其守卫（「授权表唯一性」）**无需豁免**。

> 这一点必须在 §11 的文档变更里写清楚：**arm 的表（若落库）不得被任何授权查询读到**，
> 否则会实质扩张窄豁免范围。

---

## 5. ADR-6 Analysis

### 5.1 Bootstrap 是否意味着 Discovery → Authorization？

**不是。** 必须明确两条不同的箭头：

```
❌ 被 ADR-6 禁止的箭头：
   Unknown Conversation
        ↓ Discover（收到消息）
   Authorized                     ← 授权依据 = 被观测到的事实

✅ armed Bootstrap 的箭头：
   Tenant Admin 显式 Verify Connection
        ↓ Arm（授权依据在此刻已成立）
   Bounded / one-shot / scoped Bootstrap Context
        ↓ 第一条可信入站（仅作为 trigger 兑现）
   Identity（复用或新建，user_id 可 NULL）+ Grant（一行普通 allow）
```

**判别式（可机器化）**：

```
Bootstrap 是否可发生
    == f(是否存在匹配 (tenant, channel, connection) 的有效未兑现 arm)
    ≠ f(这条消息来自谁 / 会话是否曾被观测到)
```

等式右边**完全不出现**「消息内容」「身份是否已知」「会话是否曾被 discovered」——
因此 Discovery 没有获得任何授权效力。

### 5.2 ADR-6 的三条推论逐项核对

| ADR-6 推论 / 红线 | armed Bootstrap 是否保持 |
|---|---|
| `:343` Discovery 可以发生，但**不产生**授权状态 | ✅ 保持（known-conversations 仍是纯读投影，Bootstrap 不由它触发） |
| `:344` 授权默认 **deny**（缺授权记录 = 不允许） | ✅ 保持（未 armed ⇒ 仍 DENY，与今天一致） |
| `:348` ❌ 不得存在「首次收到消息即写入授权」的代码路径 | ⚠️ **需按 §4.2 的 R-1 读法精确化**（同时新增一条守卫表达 arm 前置条件） |
| `:349` ❌ 不得把「Routing 选中」当授权结论 | ✅ 保持（Bootstrap 与 Routing 无任何耦合；Bootstrap 在 Routing 之前） |

### 5.3 结论

ADR-6 **本体不需要任何取代**（与 Package 2-C `:75` 对 ADR-6 的处置纪律一致：
「ADR-6 反而是本设计的依据，而非障碍」）。需要的是**一条补充条款**，把
「授权来源」与「授权兑现时机」显式分开。

---

## 6. Gate 2.6 D4 Answers

### 6.1 D4 原文（逐字引用，不重写）

`docs/architecture/implementation-gate-2-6-external-identity-semantic-decision.md:180-217`：

```
## D4 — Lifecycle

### 裁决（冻结）

Identity exists                    ≠   Identity is automatically created

「允许无 User 的 Identity 存在」      「系统在某个路径上自动创建 Identity」

本 Gate **只裁决「存在性合法性」**，**不裁决自动创建策略**。

### 理由

这两件事极易被偷渡混合：一旦「Identity 可以独立存在」被写下，很容易顺手推出:
「所以收到消息时创建一条 Identity 就行了」——那会直接推翻 ADR-5 与 9.2 的
「绝不创建」纪律。

### 现状（不变）

- 运行时：`IdentityResolutionExecutor` **绝不创建** Identity / User（9.2 冻结）；
- 全仓**唯一**创建路径是管理面 `IdentityApplicationService.create_identity`（6.6）。

### **明确未裁决**（留给后续独立 Gate）

?  谁有权创建「未绑定 Identity」？（管理面显式动作 / 未来 Discovery 动作 / 两者）
?  Discovery 是否需要一个显式触发点？（若需要，它属于哪个边界）
?  未绑定 Identity 的创建是否需要审计要求？

### 红线

❌ 不得以「Identity 可独立存在」为由，在任何运行路径引入自动创建
❌ 不得在本 Gate 顺手定义「首次入站即建立 Identity」的策略
```

> 注：D4 的两条红线**同样需要按「是否有预先存在的显式 Bootstrap Context」重新表述**，
> 而不是被废止。其中第二条（「不得**顺手**定义」）的原始约束对象是**当时的 Gate 2.6**——
> 本 Gate（CP-1，一个**独立 Gate**）恰恰是 D4 所指定的那个「后续独立 Gate」，
> 因此有资格正式回答三问；但**回答必须落在 ADR 文本上**，不能只留在本报告里。

### 6.2 三问逐项回答

#### D4-Q1「谁有权创建「未绑定 Identity」？」

| 项 | 内容 |
|---|---|
| **Current answer（真实代码事实）** | **Nobody。** 运行时被 ADR-5 + 9.2 禁止；管理面 `CreateIdentity.user_id` 必填且服务层强制 `users.get_by_id()` 存在（`identities.py:115,129-130`）；schema docstring 明写「V0 **不暴露**『创建未绑定 Identity』的独立 API」（`schemas/identities.py:36-38`） |
| **Proposed answer** | **唯一权力主体 = 已认证的 Tenant Admin，通过一次显式的、有界的 Verify Connection 动作（Bootstrap Arm）间的接行使**——且**仅在 arm 生效期内**、**仅对该 arm 指定的 `(channel, connection)`**、**至多产生 1 条未绑定 Identity**。运行时**不得**在无 arm 情形下创建任何 Identity。 |
| **Reason** | ① 与 Grant Management 的既有权力的**同一主体**（都是租户内已认证管理动作），不新增权力种类；② D4 的三个候选（管理面显式 / Discovery / 两者）中，**「管理面显式动作」是被选中的一个**，Discovery 被明确排除；③ 权力必须**可归属**（D4-Q3）才有资格作为授权来源 |

#### D4-Q2「Discovery 是否需要一个显式触发点？若需要，它属于哪个边界？」

| 项 | 内容 |
|---|---|
| **Current answer** | 无显式触发点；`known-conversations` 是纯读投影，**不产生**任何状态（ADR-6 + Package 2-C `:71`） |
| **Proposed answer** | **Discovery 永久不需要触发点，也不得获得任何授权效力。** 需要显式触发点的是 **Bootstrap**，其边界为：**执行边界上的独立 Bootstrap 组件**（由 Worker/执行入口在 `pipeline.execute(inbound)` **之前**调用），**不是** `RUNTIME_EXECUTION_ORDER` 中的新阶段，**不是** IdentityResolver 内部，**不是** Authorization 层（§9.3） |
| **Reason** | ① 若 Discovery 获得触发点，就等于 `Discovery → Authorization`（ADR-6 直接违反）；② Bootstrap 必须在 Identity Resolution **之前**发生（否则 Identity 必然 miss 并已 STOP）；③ 但**不得**进入 `RUNTIME_EXECUTION_ORDER`——该常量被 ≥5 处精确元组断言锁定（§2.1），新增阶段是「有序变更」且会把七段变八段；④ Authorization 层必须保持**只读**（`test_b7_executor_reads_only_grants` 已锁定），因此 Bootstrap **不得**落在其中 |

#### D4-Q3「未绑定 Identity 的创建是否需要审计要求？」

| 项 | 内容 |
|---|---|
| **Current answer** | 无（因不存在创建路径，故无审计面）。相邻先例：Grant 的创建记录 `created_at` + `updated_at`，但**无 actor 列**（`connection_conversations` 表无 `created_by`） |
| **Proposed answer** | **需要，且是硬要求。** 最小审计事实：**initiator（谁 arm）**、**armed_at**、**scope `(tenant, channel, connection)`**、**expires_at**、**consumed_at**、**consumed_by（哪条 interaction）**、**产生的 identity_id / grant_id**。其中「initiator」是**本方案合法性的组成部分**——没有它，Bootstrap 就退化为 ADR-5 红线所禁的「自动创建」 |
| **Reason** | ① arm 是**授权来源本身**，其可归属性是它区别于「自动创建」的唯一凭据（§3.3）；② C3-R §4.3 的 P4（可归属/可审计）与 T-5 验收项已把它列为前置；③ 事后必须能回答「这个未绑定 Identity 与这条 Grant 是谁允许建立的」 |

---

## 7. Authorization Source / Trigger / Effect

> 本节是本 Gate 的核心产出：把「第一条消息授予权限」这一**错误表述**，
> 分解为三个各自独立、各自可审计的概念。

### 7.1 三分定义

```
A. Authorization Source（谁有权要求系统建立授权？）
   = 已认证 Tenant Admin 在管理面显式发起的一次有界 Verify Connection 动作
     （= Bootstrap Arm）。它是**唯一的**合法性来源。
     绝不是：Message / Unknown Identity / Channel 未配置 Identity / Discovery。

B. Bootstrap Trigger（什么事件触发已经被允许的 Bootstrap？）
   = 被 arm 的 Channel 上第一条**通过信任闸门**的入站消息
     （WeCom：签名验签通过；wecom_bot：经已订阅的活连接到达且字段合法）。
     Trigger 只负责「兑现」，不负责「授权」。

C. Bootstrap Effect（Bootstrap 实际创建什么？）
   = 至多两行，且只能是这两行：
       ① 1 条 External Identity（(channel_id, external_id)，user_id 允许 NULL）
       ② 1 条 Grant（(channel_id, external_conversation_id, connection_id)）
     不创建：ACP User / Agent / Channel / Connection / Session 之外的任何对象。
```

### 7.2 后续链路（与今天**逐字节相同**）

```
Bootstrap Effect（两行已 COMMIT）
   ↓
normal Identity Resolution   ← 现在命中（identity 已存在）
   ↓
normal Authorization         ← 现在读到非空许可集合（grant 已存在）
   ↓
normal Routing               ← active ∩ granted，仍要求 eligible == 1
   ↓
normal Session Resolution / Creation
   ↓
normal Agent Dispatch
   ↓
normal Delivery              ← 仍走同一条 response_url / bot_reply.req_id
```

**Bootstrap 不在管线内、不改管线、不改任何阶段语义。** 它是管线的**前置事实供给**。

### 7.3 关键边界（一句话）

> **第一条消息是 Bootstrap 的 Trigger，不是 Authorization 的 Source。**
> Trigger 之所以能触发，是因为**在它到达之前**，授权来源（arm）已经显式成立。

---

## 8. Bootstrap Context Semantic Definition

> 只做**语义定义**，不设计 API（符合 CP-1 范围与 §13 禁令）。

### 8.1 八个语义属性

| # | 属性 | 定义 |
|---|---|---|
| 1 | **Initiator** | 只能是**该 tenant 内已认证的 Admin**（浏览器会话 / 管理面凭据）。不得由 Channel 侧、Agent 侧、消息内容、定时任务、或任何自动化路径触发。**arm 动作本身必须是一次显式的人类/管理面动作** |
| 2 | **Scope** | 必填三元组 `(tenant_id, channel_id, connection_id)`。**不绑定 agent**（agent 由 connection 派生，绑定 agent 会引入第二套派生链，违反「agent_id 派生自 Connection」）。**不绑定 conversation**（技术不可行，见 8.4）。**不绑定 user/identity** |
| 3 | **TTL** | **必须有过期时间**，且必须由 **DB 时钟**判定（沿用 12.9.4.11 §4 铁律：app 只传阈值/间隔，不生成绝对时刻）。建议语义为「分钟级」（具体值属 CP-3） |
| 4 | **Cardinality** | **一次性（one-shot）**：至多兑现 **1** 次、至多产生 **1** 个 conversation 的 Grant、至多 **1 条** Identity。兑现即失效（`consumed`），不得复用、不得延长 |
| 5 | **Consumption** | 在**第一次**满足「该 arm 匹配 + 该入站通过信任闸门」的时刻被**原子消耗**（与 Identity/Grant 的写入同一事务）。**消耗必须与写入同事务提交**，否则崩溃会产生「已消耗但未写入」的黑洞或「写入了但未消耗」的重复兑现窗口 |
| 6 | **Attribution** | **必须记录 initiator**（谁 arm 的）。这是「显式」与「自动」的判别凭据（§3.3），不可省略 |
| 7 | **Audit** | 见 D4-Q3（§6.2）：initiator / armed_at / scope / expires_at / consumed_at / consumed_by（interaction_id）/ 产生的 identity_id 与 grant_id |
| 8 | **Existing Objects Only** | arm **只能指向已经存在**的 `Channel` 与 `Connection`（二者都在该 tenant 作用域内）。arm **不得**创建、修改、或隐式修复 Channel / Connection / Agent；若 channel 或 connection 不存在 / 不 active / 跨 tenant ⇒ **arm 动作本身失败**（Fail Closed，不落 arm） |

### 8.2 明确回答：Bootstrap 是否可以创建新的 ACP User？

**不能。** 并以显式措辞冻结：

```
Bootstrap 只能：
  ✅ 创建 / 复用 External Identity（user_id 允许为 NULL，表示「未与 ACP Account 建立管理绑定」）
  ✅ 创建 / 复用 Connection Conversation Grant（一行普通 allow）

Bootstrap 不能：
  ❌ 创建 ACP User
  ❌ 创建 Agent
  ❌ 创建 Channel
  ❌ 创建 Connection
  ❌ 修改上述任一对象（含 status / config / credential）
```

> 语义依据：ADR-5 本体（`:285`「External Identity 可以一直没有 ACP User」）+ Gate 2.6 D2
> （`user_id=NULL` = 未绑定，**不是** Anonymous / Guest / System User，也**不表示「任何人可用」**）。
> 「创建 / 复用」中的**复用**分支不是可选项而是**必须**：Identity 已存在（admin 事先手工创建过）
> 时，Bootstrap 必须**复用既有行**，绝不产生第二行（(tenant, channel, external_id) 唯一键的语义）。

### 8.3 「兼容已有手工路径」的性质（重要）

armed Bootstrap 的 Effect 是**已存在的两条管理面动作的等价物**：

```
management:  POST /api/identities                      （但 V0 强制 user_id，故只能建「已绑定」）
management:  POST /api/connection-conversations        （CreateConnectionConversation）
armed Bootstrap:  ── 同一张表、同一唯一键、同一语义，区别只有两点 ──
                 ① identity 的 user_id 允许为 NULL（Package 1 已支持该列可空）
                 ② 两个动作在**同一次执行**里、由**第一条可信入站**触发、且被 arm 有界约束
```

因此本方案**没有引入新的授权种类**，只引入了一种**新的、有界的、可归属的授权来源**。
这正面回答了 §十四 的核心问题。

### 8.4 ⚠️ arm 的固有残余语义（必须显式承认，不得掩盖）

**arm 无法预先命名 conversation 键**——这是技术事实，不是设计缺陷：
单聊 conversation 键 = 发送者 `userid`，群聊 = `chatid`，**企业微信界面都不显示**
（这正是既有 runbook 的 `bootstrap discovery` 路径存在的原因：
`docs/architecture/implementation-package-2-step-7-real-wecom-e2e-runbook.md:137-157`
「企业微信界面不显示 chatid / userid，因此本测试**自动获取**」）。

因此 arm 的精确语义是：

> **「在本窗口内，该 channel 上第一条通过信任闸门的会话，获得对指定 connection 的一次有界许可。」

推论（必须写入 ADR-12 与产品文案，不得静默）：

```
如果 arm 窗口内第一条消息来自**非操作者的陌生人**：
  → 被 Bootstrap 的是**那位陌生人的** Identity 与 Grant（不是操作者的）
  → 操作者随后仍可在管理面看到该 Grant 并**撤销**（Delete 已存在）
  → 窗口结束后，arm 失效，后续陌生人回到 Fail Closed
```

**缓解强度排序（供 CP-3 决策，本 Gate 只登记）：**

| 方案 | 是否消除陌生人竞态 | 代价 |
|---|---|---|
| **S-1** one-shot + 短 TTL + operator 在场 + 事后可撤销 | **不消除，只收敛为一次** | 与目标 UX（发送「你好」）完全一致 |
| **S-2** 首条消息必须携带 UI 展示的验证口令（challenge） | **完全消除**（陌生人不持有口令，无法成为兑现对象） | 首条消息内容不是任意文本；需在入站内容上做匹配 → 触及「内容参与判别」的语义面（必须小心不使其变成授权依据） |
| **S-3** arm 只能在「操作者本人已知其渠道身份」的场景使用 | 不适用 | WeCom 界面不显示 userid ⇒ V0 不可行 |

**本 Gate 的倾向**：S-1 作为 V0 语义（与目标 UX 一致、实现最小），
S-2 登记为「严格更强的可选强化」，由 CP-3 在实现时决定是否提供；
但**无论选哪个，arm 的 one-shot + 短 TTL + 可撤销 + 可归属都不可省略**。

---

## 9. Runtime Semantic Definition

> 用户 §10 要求的语义图，逐条确认；并回答「Bootstrap 不做什么」。

### 9.1 目标语义（与用户给定图逐条对照）

```
Identity exists
    ↓
normal Runtime                       ✅ 与今天完全一致（零改动）

Identity missing
    ↓
Bootstrap Context exists?            ← 新增判定点（在执行边界，不在管线内）
    ├── NO
    │    ↓
    │  existing FAIL CLOSED          ✅ 与今天逐字节等价：
    │                                   identity_resolution →
    │                                   IDENTITY_RESOLUTION_FAILED
    │                                   (processing=failed, delivery=pending, retryable=false)
    │
    └── YES
         ↓
       Bootstrap                     ← 独立短事务：Identity(+Grant) → COMMIT
         ↓
       normal Authorization          ✅ 仍执行（读到的集合非空才继续）
         ↓
       normal Router                 ✅ 仍执行（active ∩ granted，eligible 必须 == 1）
         ↓
       normal Agent                  ✅
         ↓
       normal Delivery               ✅
```

### 9.2 Bootstrap **不**做什么（逐条确认用户 §10 的清单）

```
✅ Bootstrap 不 bypass Authorization     —— Authorization 阶段仍执行、仍可能 DENY
                                            （例：arm 指定的 connection 已 inactive ⇒
                                             授权通过但 ROUTING_FAILED；
                                             若 grant 因任何原因未写入 ⇒ 仍 UNAUTHORIZED）
✅ Bootstrap 不 bypass Router            —— Router 仍要求 eligible == 1；Bootstrap 不参与选路
✅ Bootstrap 不 bypass Connection        —— 目标 connection 由 arm 显式指定，不由 Router 推断
✅ Bootstrap 不 bypass Grant             —— 它**建立** Grant，随后 Authorization **消费**它
✅ Bootstrap 不 create hidden super-user —— 不创建任何 User（§8.2）
✅ Bootstrap 不 create ACP User          —— 同上
✅ Bootstrap 不 introduce a new Runtime stage
                                        —— **不进入 `RUNTIME_EXECUTION_ORDER`**；
                                           该常量被 ≥5 处精确元组断言锁定（§2.1）；
                                           落点为执行边界的前置组件（9.3）
```

### 9.3 Runtime 落点判定（回答用户 §R7 式问题，只判定不实现）

| 候选落点 | 判定 | 理由 |
|---|---|---|
| **IdentityResolver 内** | ❌ **不可** | ① 9.2 冻结「绝不创建」；② `test_adr5_identity_resolution_still_fails_closed` 断言该文件零 `{create, add, save, link, upsert}` 调用；③ 9.2「Fail Closed」本身就是它的契约语义 |
| **Authorization 层** | ❌ **不可** | `test_b7_executor_reads_only_grants` 已锁定授权执行器**只读**授权表，且不得消费 identity_id / user_id；在其中写库会同时破坏「只读」与「不得借用他人失败码」两条纪律 |
| **SessionResolver 之前（新增阶段）** | ❌ **不可** | `RUNTIME_EXECUTION_ORDER` 被 ≥5 处精确元组断言锁定；且 Bootstrap 必须早于 Identity Resolution，放在 Session 之前仍太晚 |
| **Runtime orchestration 层（Coordinator 内）** | ⚠️ **不建议** | Coordinator 是纯编排器；在其中创建会让「零 persistence / 零 capability 依赖」出现裂缝，且会落进 ADR-5 守卫的 `_RUNTIME_CHAIN_FILES` 清单（`coordinator.py`）→ 直接触发守卫失败 |
| ✅ **执行边界上的独立 Bootstrap 组件**（Worker/执行入口在 `pipeline.execute(inbound)` 之前调用） | ✅ **最合理** | ① 七段管线与顺序常量**逐字节不变**；② 复用**已有范式**（`PersistingCoordinator` 就是执行边界上的同 Protocol 包装）；③ 语义干净：Bootstrap 是管线的**前置条件**，不是一段；④ 不满足 arm 时 **no-op**，与今天等价 |

### 9.4 ⚠️ 与 ADR-5 守卫的**结构性冲突**（本 Gate 必须点明的隐患）

`test_adr5_runtime_never_creates_acp_user_or_identity` 的禁止标识符集合包含
`create_identity` / `ensure_identity` / `upsert_identity` / `provision_identity` / `auto_provision` / `IdentityApplicationService` 等，
但**只扫描固定 4 个文件**（`_RUNTIME_CHAIN_FILES`）。

```
若把 Bootstrap 实现为 application/bootstrap.py（不被该清单扫描）：
    → 守卫会"通过"
    → 但 ADR-5 的实质（运行时零创建）已被违反
    → 这是**守卫规避**，不是**架构合规**
```

**Package 2-B `:118-120` 已对此类规避立过先例纪律**：

> 本设计**不采用"挑一个不在黑名单里的名字"这种规避**，而是要求 **ADR-11 显式记录一条窄豁免**。

**因此本 Gate 明确要求**：ADR-12 签发时，**必须同步扩展 ADR-5 守卫的扫描面**
（把新的 Bootstrap 组件文件加入 `_RUNTIME_CHAIN_FILES` 等价集合），
并以**显式允许清单**（恰好一个创建入口）表达例外，
使「这一处创建」是**被机器化看见并批准的**，而不是**看不见的**。

---

## 10. Compatibility With Existing Architecture

| 既有冻结物 | 是否兼容 | 说明 |
|---|---|---|
| **ADR-1**（ACP User ≠ External Identity） | ✅ **兼容并被强化** | Bootstrap 正是「External Identity 不需要 ACP User」的正向落地 |
| **ADR-3**（作用域 = Conversation；判定输入 `(channel_id, external_conversation_id)`） | ✅ 兼容 | Bootstrap 建立的 Grant 键完全同构；授权判定输入不变 |
| **ADR-5 本体** | ✅ 兼容 | 仍不创建 ACP User（永久） |
| **ADR-5 推论 / 红线** | ⚠️ 需**显式重新表述**（§3.2 / §11.3） | 「运行时零创建」是「无显式来源」前提下的实现约束 |
| **ADR-6** | ✅ 兼容（**需要一条补充条款**而非取代） | Bootstrap ≠ Discovery→Authorization；default-deny 完整保留 |
| **ADR-7 / ADR-8** | ✅ 兼容 | Authorization 仍在 Session 之前、Routing 之前；Router 仍只消费 eligible |
| **ADR-9** | ✅ 兼容 | 不引入 effect / 优先级 / principal / 策略语言；仍是二值许可集合 |
| **ADR-10**（UX 约束） | ⚠️ 需注意措辞 | 「Verify Connection」是**产品语言**（不是 Policy/Principal/Scope 术语）⇒ 合规；但 arm 的**残余语义（S-1 陌生人竞态）应向操作者明示**，不得用模糊文案掩盖 |
| **Package 2-C 窄豁免** | ✅ **不扩张** | 仍只写 `connection_conversations` 一行普通 Grant；arm 不参与授权判定（否则会实质扩张豁免范围） |
| **Package 2-C `:72`** | ✅ 兼容（需精确化） | §4.2 的 R-1 读法 |
| **Gate 2.6 D4** | ⚠️ **三问必须由 ADR-12 正式回答** | 本 Gate 已给出 Proposed answer（§6.2） |
| **Gate 2.6 D7**（创建/发现 与 绑定必须分开） | ✅ 兼容 | Bootstrap 只做「发现/建立身份」+「引用已存在 connection」；**不产生绑定**（`user_id` 保持 NULL 即「未绑定」），绑定仍是管理面显式动作 |
| **`RUNTIME_EXECUTION_ORDER`** | ✅ 不变 | 七段、顺序常量、`STAGE_*` 集合、`STAGE_FAILURE_MAP` 键集合均不变 |
| **`connection_conversations` 唯一键 + 复合 FK** | ✅ 复用 | Bootstrap 的写入受同一约束保护（含跨租户/跨 channel 结构性防护） |
| **`identities.user_id` 可空（Package 1 / 0006）** | ✅ 已就位 | 无需新 migration 即可容纳 `user_id=NULL` |
| **机器化守卫** | ⚠️ 2 处需变更（**增补型**） | ① ADR-5 守卫**扩展扫描面**（§9.4）；② 新增 arm 前置条件守卫（增补，不删改既有条目——meta-guard 已锁定清单） |

---

## 11. Required Documentation Changes

> 本轮**不修改任何文档**，只提出建议。必须避免「为了新功能而破坏旧架构决策」。

### 11.1 A — 保持原文不动

```
✅ docs/architecture/authorization-and-access-model.md 的 ADR-1 / ADR-2 / ADR-3 / ADR-4 /
   ADR-7 / ADR-8 / ADR-9 / ADR-10            —— 逐字不动
✅ ADR-5 决策主体与「External Identity 可以一直没有 ACP User」 —— 逐字不动（且被强化）
✅ ADR-6 全篇（含两条红线）                   —— 逐字不动
✅ Package 2-C §1.4 对 ADR-6 的处置（不下取代）—— 逐字不动
✅ Package 2-C §1.5 窄豁免的**范围**（connection_conversations 为唯一授权状态存储）—— 不动
✅ Gate 2.6 D1 / D2 / D3 / D5 / D6 / D7      —— 逐字不动
✅ Gate 2.6 D4 的**措辞**（含两条红线）        —— 不动；由 ADR-12 回答其三问并说明取代关系
```

### 11.2 B — 增加例外条款（补充，不改语义）

```
B-1  ADR-6 §推论（:341-344）：追加一条
     「授权的兑现时机可以晚于授权的成立时刻；若一条授权在一个显式、有界、
       可归属的 Bootstrap Context 成立之后，由该 Channel 上第一条可信入站兑现，
       该兑现**不构成** Discovery → Authorization。」
B-2  Package 2-C §1.4 的 ADR-6 对照表（:69-73）：追加一行
     「❌ 不得「首次收到消息即写入授权」| ✅ 继续有效；arm 兑现见 ADR-12」
B-3  Gate 2.6 D4-Q1/Q2/Q3：追加「已由 ADR-12 回答」的指引（不改 D4 原文措辞）
```

### 11.3 C — 重新表述原则（必须显式、可追溯；不改本体）

```
C-1  ADR-5 §推论（:296-297）「运行时链路（Identity Resolution / Routing / Session /
     Dispatch）不得出现任何创建 Identity / User 的调用」
        → 重新表述为：
        「除**已显式成立的 Bootstrap Context 的兑现**外，运行时链路不得创建 Identity / User；
          该唯一例外必须由 ADR-12 定义，并被机器化守卫显式看见（不得放在守卫扫描面之外）。」
        （**ACP User 的创建仍然在任何情形下被绝对禁止**）

C-2  ADR §六 forbidden（:650）「❌ 首条消息自动创建 ACP User / Identity」
        → 精确化为：
        「❌ 无预先存在的显式 Bootstrap Context 的首条消息，自动创建 ACP User / Identity」
        （红线**不消失**，只是被限定到它所真正针对的情形）

C-3  Gate 2.6 D4 两条红线（:213-216）同样按 C-2 的限定精确化
```

> 措辞纪律：C-1 / C-2 / C-3 **必须以上述「新增编号 + 显式取代关系」的方式提交**
> （ADR 文件 `:702-703` 的硬规定），**不得原地改写**。

### 11.4 D — 新增一个 superseding ADR

```
D-1  新增 ADR-12：Bootstrap Authorization Source（首次连接验证）
     —— 内容至少包含：
        ① Authorization Source / Trigger / Effect 三分定义（本报告 §7）
        ② Bootstrap Context 的 8 项语义（本报告 §8）
        ③ 显式取代关系：ADR-5 推论（C-1）、ADR §六 :650（C-2）、Gate 2.6 D4 红线（C-3）
        ④ 回答 Gate 2.6 D4 三问（本报告 §6.2）
        ⑤ 明确的「不创建 ACP User / Agent / Channel / Connection」清单
        ⑥ 机器化守卫要求：ADR-5 守卫扫描面**扩展** + 新增 arm 前置条件守卫
        ⑦ 登记 arm 的残余语义（§8.4）与所选缓解（S-1 / S-2）

D-2  ⚠️ **ADR 编号不可占用 ADR-11**：
     ADR-11 已被 Package 2-C §1 冻结为「authorization persistence」专用槽位
     （其内容已冻结但**文件从未创建**，见 package-2-c :508 / :515）。
     因此新 ADR 编号为 **ADR-12**。

D-3  ⚠️ **顺带修复一处文档链缺口（登记，不阻塞）**：
     ADR-11「只有内容冻结、没有文件」意味着「新增编号 + 显式取代关系」这条
     取代台账上存在一个**空洞**（S1~S4 的取代事实无处可查）。
     建议在 ADR-12 中一并**原文登记**该缺口，或先补 ADR-11 文件，
     使取代链完整、可审计。
```

### 11.5 E — 机器化护栏变更（**增补型**，不得删改）

```
E-1  扩展 `test_adr5_runtime_never_creates_acp_user_or_identity` 的扫描面：
     把 Bootstrap 组件文件纳入 `_RUNTIME_CHAIN_FILES` 等价集合，
     并以**显式允许清单**（恰好一个 identity 创建入口 + 一个 grant 创建入口）表达例外。
     —— 目的：使唯一例外被机器**看见**，杜绝「放在守卫盲区」的规避（§9.4）。

E-2  新增 ADR-12 语义守卫（增补）：
     · 无 arm ⇒ 无创建（未 arm 时行为与今天逐字节等价）
     · arm 必须 one-shot（同一 arm 不得兑现两次）
     · arm 必须 scoped（不得跨 tenant / channel / connection）
     · Bootstrap 路径不得创建 ACP User / Agent / Channel / Connection
     · Bootstrap 不得绕过 authorization（authorization 仍执行且仍可 DENY）

E-3  `test_adr6_no_implicit_grant_on_first_message`：
     **不删不改其断言**（其 docstring 已预留演进说明）；
     把「arm 前置条件」的表达放在新增的 E-2 守卫里。

E-4  `test_package_2_step_4_authorization_contract_closure.py` 的契约条目表：
     **只增补**，不得删除或重命名既有条目（该表本身就是 meta-guard）。

E-5  `RUNTIME_EXECUTION_ORDER` 的 ≥5 处精确元组断言：
     **一处都不改**（这正是「不引入新 Runtime stage」的机器化保证）。
```

---

## 12. Frozen Security Red Lines

> 建议 CP-1 通过后，以下清单与 ADR-12 一同冻结。

### 12.1 允许（唯一合法路径）

```
Tenant Admin
   → Verify Connection（显式管理面动作）
   → Bootstrap Arm（有界 / 一次性 / 精准作用域 / 可归属 / 可审计）
   → First Message（通过信任闸门，作为 trigger）
   → Identity / Grant Bootstrap（至多 1 条 Identity + 1 条 Grant）
   → normal Authorization（仍执行、仍可 DENY）
   → normal Router / Agent / Delivery
```

### 12.2 禁止（逐条）

```
❌ Unknown Message            → Identity Bootstrap
❌ Unknown Message            → Grant Bootstrap
❌ Channel 未 arm             → Bootstrap
❌ Identity missing           → 自动授权
❌ Discovery                  → 自动 Authorization
❌ Bootstrap                  → bypass Authorization
❌ Bootstrap                  → 创建 ACP User
❌ Bootstrap                  → 创建 Agent / Channel / Connection
❌ Bootstrap                  → 修改既有 Channel / Connection / Agent 的任何字段
❌ arm 跨 tenant / 跨 channel / 跨 connection 兑现
❌ 一个 arm 兑现多于一次，或多于一个 conversation / identity
❌ arm 在过期后仍可兑现
❌ 过期 arm / 无 arm 存在时产生任何写入
❌ 把 arm 记录当作授权状态（arm 不得参与 granted 集合判定）
❌ 新增授权状态存储（窄豁免范围不得扩张）
❌ 把 Bootstrap 加入 RUNTIME_EXECUTION_ORDER 或新增 Runtime 阶段
❌ 在 IdentityResolver / Authorization / Coordinator 内创建 Identity 或 Grant
❌ 以「把创建代码放在 ADR-5 守卫扫描面之外」的方式通过守卫（§9.4）
❌ Bootstrap 失败被静默吞掉（必须回落既有 Fail Closed，且可观测）
```

### 12.3 建议冻结的原则（英文措辞，供 ADR-12 直接引用）

```
PRINCIPLE (proposed for freeze):

    A message may trigger Bootstrap **only when** a valid Bootstrap Context
    was explicitly created **before** the message arrived.

    In other words:
        message ≠ authorization source
        message = bootstrap trigger
        (precondition: the bootstrap authorization context already exists)
```

> **本 Gate 认同并将其作为建议冻结原则**（§9.1 的判定式即其机器化形式）。

---

## 13. CP-1 Decision

```
CONDITIONAL PASS
```

### 13.1 核心裁决（对 §十四 最终问题的直接回答）

> **是否承认：「Tenant Admin 显式发起的、有界、一次性、精准作用域、可审计的
> Verify Connection Bootstrap Context，可以成为首次真实 Channel Message
> 建立 Identity / Grant 的合法授权来源？」**
>
> ## **YES —— 承认。**
>
> 并且同时冻结：
>
> ## **第一条消息只是 Bootstrap Trigger，不是 Authorization Source。**

### 13.2 为什么不是 PASS

用户给定的 PASS 标准是「明确承认……**且与 ADR-5 / Package 2-C / ADR-6 / Gate 2.6
不存在不可调和的语义冲突**」。本 Gate 的判定：
- **不存在不可调和的冲突**（ADR-5 本体、ADR-6、Package 2-C 的 default-deny 与窄豁免
  全部可保持；冲突项均为**字面推论**，且都有可追溯的重新表述路径）；
- **但**仍有**明确的架构决策未完成**，因此按用户给定的 CONDITIONAL PASS 标准判定：

| # | 未完成的架构决策 | 为什么 CP-1 无法自行关闭 |
|---|---|---|
| **U-1** | **ADR-12 尚未签发**：ADR-5 推论 / ADR §六 `:650` / Gate 2.6 D4 红线的**重新表述**必须以上述 ADR 形式提交（`:702-703` 硬规定），而本轮**禁止修改既有 ADR** | 本 Gate 只能**提出**变更（§11），不能签发 |
| **U-2** | **「谁有权创建未绑定 Identity」的答案会反转一条已写下的 V0 事实**：`schemas/identities.py:34-38` 明写「V0 **不暴露**创建未绑定 Identity」，Package 1 选择的是「语义拆分 ≠ API 数量增加」。承认 armed Bootstrap 意味着**新增第二个创建入口**（虽是内部路径、且 `user_id=NULL`）——这需要 ADR-12 明确授权 | 属**架构授权**，非本 Gate 权限 |
| **U-3** | **arm 的残余语义（§8.4）尚未 ratify**：arm 只能以「该 channel 上下一条未知会话」为对象，因而存在**有界的陌生人竞态**。选择 S-1（接受 + 收敛为一次）还是 S-2（challenge 消除）是**语义决策**，需要产品/架构显式确认 | 涉及产品语义与 UX 一致性，需独立确认 |
| **U-4** | **arm 事实的存放类别未决**（`Channel.config` 子块 / 窄列或窄表 / 进程内 lease）——C3-R §10.3 保留为开放决策；影响多进程与重启安全性、RLS 覆盖、以及「arm 不得参与授权判定」的可证明性 | 属 CP-3 |

### 13.3 为什么不是 BLOCKED

用户给定的 BLOCKED 门槛是「证明这种 Bootstrap Authorization Source 在当前 ACP
Authorization Model 中**本质上属于非法授权来源**」。本 Gate 证明**恰恰相反**：

```
① 它不引入新的授权种类
     —— Bootstrap 写的仍是 connection_conversations 一行普通 allow（ADR-3 / ADR-6 / ADR-9 全兼容）
② 它与既有唯一授权来源（管理面 Grant Management API）同主体、同边界、同语义
     —— 只是把「已知会话键」放宽为「该 channel 的第一条未知会话」，并加上有界约束
③ 它的合法性凭据（显式 / 有界 / 精准作用域 / 可归属 / 可审计）全部可机器化判别
     —— §3.3 的判别式不依赖任何主观解释
④ 未 arm 情形与今天逐字节等价，且不需要任何 schema 变更之外的授权模型变更
⑤ ADR 文件自身提供了合法的修改出口（新增编号 + 显式取代关系），且已被使用过两次
     —— Package 1（0006 放宽 user_id 可空）与 ADR-11（授权持久化）都是先例
```

并且**明确区分**：

```
Not Implemented                     → 与判定无关（CP-1 不因「当前代码没实现」而降级）
Technically Unsafe                  → 已排除（§3.3 的判别式 + one-shot + TTL + 可撤销）
Architecturally Invalid             → 已排除（§10 全表兼容；无任何「授权来源不合法」的论证成立）
未完成的架构决策（U-1 ~ U-4）        → 这才是 CONDITIONAL 的真正来源
```

---

## 14. What CP-2 Must Solve

> 用户 §13 明确：CP-1 只做语义裁决，**不修复 wecom_bot 未认证 HTTP**。以下为移交项。

**CP-2 = 信任闸门闭环（硬前置，必须先于 CP-3 生效）**

| # | 待解问题 | 依据 |
|---|---|---|
| **CP-2-1** | `wecom_bot` 渠道经 `POST /api/channels/{id}/wecom/webhook` 可被**无凭据注入** `aibot_msg_callback`（`wecom_bot.parse_message` 把 `signature/timestamp/nonce` 当空占位，信任来自订阅连接）⇒ 必须 Fail Closed | C3-R §9.4（最高优先级风险 R-2） |
| **CP-2-2** | 该旁路在 bootstrap 之前只是「一条无害的失败 Interaction」；bootstrap 之后是「**未认证的授权写入**」⇒ 它是「Trigger 必须可信」这一语义的**结构性前提** | 本报告 §7.1-B（Trigger = **通过信任闸门**的入站） |
| **CP-2-3** | 逐渠道定义「信任闸门」的**唯一事实来源**：`wecom`（每请求验签）/ `wecom_bot`（订阅连接 + 帧字段合法）/ `local`（开发渠道，需显式声明其信任模型） | 同上 |
| **CP-2-4** | 复核**入站 URL 的 `channel_id` 仅作定位**这一纪律在两条路径上都成立（tenant 必须取自已认证 Channel） | ADR-6 / Cloud-0.1.3.1 |

**CP-2 不得做的事**：不得为了让 bootstrap "能用" 而放宽 `wecom_bot` 的入站信任（那会把
「无 arm 的陌生人」升级为「可注入的陌生人 + 可兑现的 arm」）。

---

## 15. What CP-3 Must Solve

**CP-3 = Bootstrap Arm 的落地设计（语义 → 具体）**

| # | 待解问题 |
|---|---|
| **CP-3-1** | **arm 事实的存放类别**（U-4）：`Channel.config` 子块 / 窄列 / 窄表 / 进程内 lease。判据：多进程与重启安全、RLS/tenant 覆盖、可审计性、以及「arm **不得**被授权查询读到」的可证明性 |
| **CP-3-2** | **时间权威与 TTL 语义**：过期判定必须使用 **DB 时钟**（12.9.4.11 §4 铁律：app 只传阈值，不生成绝对时刻）；TTL 建议值（分钟级） |
| **CP-3-3** | **原子一次性兑现**：`arm 消耗 + Identity 写入 + Grant 写入`必须在**同一事务**内提交；崩溃恢复语义必须明确（禁止「已消耗未写入」黑盒与「已写入未消耗」重复兑现窗口） |
| **CP-3-4** | **find-or-create 与 IntegrityError 处理**：Identity 与 Grant 的并发插入冲突必须「回读复用」，不得让 IntegrityError 冒泡成 `UNEXPECTED_FAILURE`（C3-R §6.2 的 M1/M2） |
| **CP-3-5** | **组件落点**：执行边界上的独立组件（§9.3 推荐）；**不得**进入 `RUNTIME_EXECUTION_ORDER`；**不得**落在 IdentityResolver / Authorization / Coordinator；并与 **CP-5 的守卫扩展**同时交付（否则构成 §9.4 的规避） |
| **CP-3-6** | **管理面动作与文案**：Verify Connection 的触发入口（是否新增端点属独立裁决——注意 Cloud-0.1.5 冻结「不为 Gate 新增后端端点」的精神）；且文案必须**如实说明** arm 的残余语义（§8.4） |
| **CP-3-7** | **撤销与再 arm**：operator 撤销已兑现 Grant 的路径（已存在 `Delete`）；arm 过期/已消耗后的再 arm 语义（允许新建 arm，但不得延长或复活旧 arm） |
| **CP-3-8** | **S-1 vs S-2 选定**（§8.4）：是否提供 challenge 强化 |
| **CP-3-9** | **Bootstrap 失败的回落与可观测**：失败必须回落既有 Fail Closed（`IDENTITY_RESOLUTION_FAILED`），**不新增** `STAGE_*` / `failure_code`（沿用 C3-R §12.5 F3 的最小化纪律） |

---

## 16. What CP-4 / CP-5 Must Solve

### 16.1 CP-4 = 并发 / 原子性 / 回归加固

| # | 待解问题 | 来源 |
|---|---|---|
| **CP-4-1** | **Session 唯一约束竞态**：并发首条消息使 `SessionCreationExecutor` 的一侧被 catch-all 包成 `SESSION_RESOLUTION_FAILED`（sessions 已有 tenant-leading UNIQUE）。最小修复 = 唯一约束冲突 → 回读复用 | C3-R §6.2 的 **M3**（既有缺陷，被 bootstrap 放大） |
| **CP-4-2** | **N 并发首条消息验收**：恰好 1 条 Identity + 1 条 Grant，且**所有合法消息都以终态结束**（不允许任一条因 Session 竞态而失败） | C3-R §13 T-6 |
| **CP-4-3** | **重推（同 `external_interaction_id`）**：不重复 bootstrap、不重复 Agent 调用、不重复 Delivery（12.9.4.9 去重 + CAS claim 已具备） | C3-R §8.1 |
| **CP-4-4** | **崩溃重放**：bootstrap 已 COMMIT、执行前崩溃 → reclaim 后重执行**不得**新增第二条 Identity / Grant | C3-R §8.2 / T-9 |
| **CP-4-5** | **at-least-once 执行**（既有特性）与「用户可见重复投递由一次性回复句柄兜住」的登记 | C3-R §8.2 / R-5 |
| **CP-4-6** | 文档漂移修正：`session_creation.py` docstring 仍写「sessions 表当前无唯一约束（5.3 冻结）」 | C3-R §6.2 |

### 16.2 CP-5 = 守卫与契约的修订机制（治理闭环）

| # | 待解问题 |
|---|---|
| **CP-5-1** | **ADR-12 签发**（含 §11.3 的 C-1 / C-2 / C-3 显式取代关系与 §11.2 的 B-1~B-3 补充条款） |
| **CP-5-2** | **ADR-11 文件缺口修复**（§11.4 D-3）：使取代台账完整可审计 |
| **CP-5-3** | **ADR-5 守卫扫描面扩展**（§11.5 E-1）：以显式允许清单表达唯一例外，**杜绝守卫盲区规避** |
| **CP-5-4** | **新增 arm 前置条件守卫**（§11.5 E-2）：无 arm 无创建 / one-shot / scoped / 不得创建 User-Agent-Channel-Connection / 不得绕过 authorization |
| **CP-5-5** | **契约条目表增补**（§11.5 E-4，只增不删）+ `RUNTIME_EXECUTION_ORDER` 精确元组断言**一处不改**（E-5） |
| **CP-5-6** | **回归基线重跑与不变量登记**：`test_adr_authorization_and_access_model`、`test_package_2_step_4_…`、`test_cloud_0_1_1_*` 等全绿，并把「未 arm ⇒ 与今天逐字节等价」落为可执行断言 |
| **CP-5-7** | **§12.3 的建议冻结原则**正式写入 ADR-12 |

---

## 附：最终回答（对用户 §十五 的逐条回应）

> **是否承认：「Tenant Admin 显式发起的、有界、一次性、精准作用域、可审计的
> Verify Connection Bootstrap Context，可以成为首次真实 Channel Message
> 建立 Identity / Grant 的合法授权来源？」**

**YES。**（CP-1 Decision = **CONDITIONAL PASS**）

并且同时明确：

> **第一条消息只是 Bootstrap Trigger，不是 Authorization Source。**
> 它的触发效力来自「在它到达之前，arm 已经显式成立」。

**两条路径必须同时成立（用户 §十六 的目标）：**

```
【路径一 · armed】
  用户配置 WeCom + Agent
    → 点击「验证连接」
    → SteeringFlow 创建受控 Bootstrap Context（有界 / 一次性 / 精准作用域 / 可归属 / 可审计）
    → 用户在 WeCom 发送「你好」
    → Bootstrap Identity / Grant（至多两行，同一事务 COMMIT）
    → normal Authorization → Router → Agent → Delivery
    → 用户第一条消息直接得到 Agent Response          ✅ 目标达成

【路径二 · 未 arm（必须同时保持）】
  普通陌生消息
    → 无 Bootstrap Context
    → 保持原有 Fail Closed（identity_resolution → IDENTITY_RESOLUTION_FAILED）
    → Agent 不被调用、无任何投递                     ✅ 与今天逐字节等价
```

**本审计未做的事（只读声明）**：未修改任何生产代码 / DB / migration / 测试 / 配置；
未创建或删除任何数据；未修改既有 ADR / Package 2-C / Gate 2.6（仅提出变更建议）；
未 commit / 未 tag；未设计 API 字段、未实现 Arm、未修改 Identity / Grant / Session / Runtime；
未执行任何会写入 SQLite / PostgreSQL / local_outbox 的检查。

本文件为架构决策产出，**不构成实现批准**；进入实现需先完成 §13.2 的 U-1 ~ U-4。
