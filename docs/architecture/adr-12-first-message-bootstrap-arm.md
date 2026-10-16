# ADR-12 — First Message Bootstrap Arm（Bootstrap Authorization Source）

> 状态：**FROZEN（本文件即签发动作）**
> 性质：**新增编号 + 显式取代关系**（依 `authorization-and-access-model.md:702-703` 的硬规定：
> 「任何修改须以『新增 ADR 编号 + 显式取代关系』的方式提交，**不得原地改写既有 ADR 的语义**」）
>
> 上游：ADR-1 ~ ADR-10（FROZEN）、**ADR-11**（授权状态持久化补签）、Package 2-C、Gate 2.6
> 　　　`docs/M6_6_C3_CP1_…`（语义裁决）、`docs/M6_6_C3_CP2_…`（Trust Gate）、
> 　　　`docs/M6_6_C3_CP3_BOOTSTRAP_ARM_DESIGN_AUDIT.md`（设计）、
> 　　　`docs/M6_6_C3_CP3_D_BOOTSTRAP_ARM_ARCHITECTURE_DECISION.md`（O-1 ~ O-5 裁决）
>
> **本文件关闭 CP-1 §13.2 的 U-1**，并（依据 CP-3 / CP-3-D 的裁决结果）一并关闭 U-2 / U-3 / U-4。

---

## 0. 一句话决策

```
Tenant Admin 通过 **authenticated session** 显式执行 Verify Connection，
可以创建一个 **bounded / one-shot / scoped / attributable / auditable** 的
Bootstrap Arm；该 Arm 是首次真实 Channel Message 建立 Identity + Grant 的
**唯一合法授权来源**。

第一条消息是 Bootstrap 的 **Trigger**，**不是** 授权来源。
```

---

## 1. 冻结语义链（写死）

```
Tenant Admin（已认证会话）
        ↓  显式 Verify Connection
Bootstrap Arm（bounded / one-shot / scoped / attributable / auditable）
        ↓
   = Authorization Source        ← 授权在此刻已成立（消息到达之前）
        ↓
first trusted inbound message    ← 通过 Trust Gate（CP-2）才可能到达此处
        ↓  Trigger（仅兑现，不授权）
Bootstrap establishment          ← 同一短事务：CAS 消费 Arm + Identity + Grant
        ↓
normal Authorization             ← 仍执行、仍可 DENY（不被 bypass）
        ↓
Router                           ← 仍要求 active ∩ granted 且 eligible == 1
        ↓
Agent
        ↓
Delivery
```

**未 Arm 时的另一条路径必须同时成立**：

```
第一条陌生消息（无 Arm / Arm 已过期 / 已消费 / scope 不符）
        ↓
既有 Fail Closed（IDENTITY_RESOLUTION_FAILED；无 Identity / 无 Grant / 不调用 Agent）
```

---

## 2. 三分定义（CP-1 §7）

```
A. Authorization Source
   = 已认证 Tenant Admin 显式发起的一次有界 Verify Connection 动作（= Bootstrap Arm）。
     绝不是：Message / Unknown Identity / Discovery / Channel 配置 / 任何自动化路径。

B. Bootstrap Trigger
   = 被 Arm 的 scope 上、第一条**通过信任闸门**的入站消息。
     Trigger 只负责兑现，不负责授权。

C. Bootstrap Effect
   = 至多两行，且只能是这两行：
       ① 1 条 External Identity（(channel_id, external_id)，user_id = NULL）
       ② 1 条 Grant（普通 ConnectionConversation 行）
```

---

## 3. Bootstrap Arm 语义（八属性 + CP-3/CP-3-D 冻结的实现约束）

| # | 属性 | 冻结内容 |
|---|---|---|
| 1 | **Initiator** | 必须是**该 tenant 内已认证的操作者**（`AuthenticatedUser`）。**永不**回退到 anonymous / guest / system / sentinel / 请求体携带的 user_id（O-4）。列 `initiator_user_id` **NOT NULL** |
| 2 | **Scope** | `(tenant_id, channel_id, connection_id)`。**不含 agent_id**（由 Connection 不可变绑定派生）；**不含 conversation 键**（技术不可行） |
| 3 | **TTL** | 必须有 `expires_at`；由 **DB 时钟**判定（`now()`），app 只传间隔、绝不生成绝对时刻；判定与消费在同一条 SQL 内（无 TOCTOU） |
| 4 | **Cardinality** | one-shot：至多兑现 1 次、至多 1 个 conversation 的 Grant、至多 1 条 Identity |
| 5 | **Consumption** | 与 Identity + Grant 的写入**同一短事务**提交（CP-3 §10）。事务失败 ⇒ 回滚 ⇒ Arm 回到 ARMED（可 retry） |
| 6 | **Attribution** | initiator / armed_at / scope / expires_at / consumed_at / consumed_by_interaction / 两个外部裸键 |
| 7 | **Audit** | Arm 行即审计记录；**不写 activities**（那是 Runtime Evidence） |
| 8 | **Existing Objects Only** | 只作用于已存在的 Tenant / Channel / Agent / Connection；**不创建**其中任何一个 |

**实现约束（CP-3-D 裁决，属本 ADR 授权范围）**：

```
O-1  同一 scope 至多一条 status='armed' —— 权威层 = **DB 部分唯一索引**
     （应用层 revoke-then-insert 只负责 supersede 语义，不承载不变量）
O-2  Arm 持久化 = 窄表 bootstrap_arms（须经 Cloud V0 冻结的例外程序，见 §12）
O-3  操作入口 = POST /api/connections/{connection_id}/bootstrap-arm（Connection 子资源）
     重复调用 = supersede；返回 201 + DB 生成的 expires_at
O-4  initiator 必须来自有效会话；AUTH_REQUIRED=False 且无 cookie ⇒ Arm 不可用（401）
O-5  V0 **禁止群聊会话消费 Arm**；群聊其余能力（Grant / Session / Agent / Delivery）不变
```

---

## 4. 显式取代关系

### 4.1 取代（仅限**推论性 / 字面**限制）

| # | 被取代物 | 原文 | 取代后 |
|---|---|---|---|
| **C-1** | ADR-5 §推论（`:296-297`） | 「运行时链路（Identity Resolution / Routing / Session / Dispatch）**不得出现任何**创建 `Identity` / `User` 的调用」 | 「**除已显式成立的 Bootstrap Context 的兑现外**，运行时链路不得创建 Identity / User；**ACP User 的创建在任何情形下仍被绝对禁止**；该唯一例外必须被机器化守卫**显式看见**」 |
| **C-2** | ADR §六 forbidden（`:650`） | 「❌ 首条消息自动创建 ACP User / Identity」 | 「❌ **无预先存在的显式 Bootstrap Context 的**首条消息，自动创建 ACP User / Identity」（红线不消失，只被限定到它真正针对的情形） |
| **C-3** | Gate 2.6 D4 两条红线（`:277-279`） | 「不得以『Identity 可独立存在』为由，在任何运行路径引入自动创建」 | 同 C-2 的限定；并注明**已由 ADR-12 正式回答 D4 三问**（§5） |

### 4.2 **明确不取代**（核心原则，永久保持）

```
❌ 不取代 ADR-5 本体：
     「Authorization 不自动创建 ACP User」
     「External Identity 可以一直没有 ACP User」

Bootstrap 不得自动创建 ACP User —— 这一条在本 ADR 下**进一步加强**，见 §6。
```

### 4.3 对 ADR-6 / Package 2-C：补充，不取代

```
ADR-6 本体与两条红线**逐字不动**。
补充条款（B-1）：授权的**兑现时机**可以晚于授权的**成立时刻**；
   若授权成立于一个显式、有界、可归属的 Bootstrap Context 之后，
   并由该 scope 上第一条可信入站兑现 —— 该兑现**不构成** Discovery → Authorization。
补充条款（B-2）：Package 2-C 对照表「❌ 不得『首次收到消息即写入授权』」
   ✅ 继续有效；arm 兑现见 ADR-12。
```

### 4.4 对 ADR-11 窄豁免：不扩张

```
bootstrap_arms **不是**授权状态表：不参与 find_granted_connection_ids；
授权阶段读到的集合仍**只**来自 connection_conversations。
ADR-11 §4「V0 唯一的授权状态存储」保持不变。
```

---

## 5. Gate 2.6 D4 三问（正式回答）

| 问 | 回答 |
|---|---|
| **D4-Q1 谁有权创建「未绑定 Identity」？** | **唯一权力主体 = 已认证 Tenant Admin**，通过一次显式、有界的 Verify Connection（Arm）**间接行使**；仅在 Arm 有效期内、仅对 Arm 指定的 `(channel, connection)`、至多产生 1 条未绑定 Identity。运行时在无 Arm 情形下**不得**创建任何 Identity。Discovery 被明确排除 |
| **D4-Q2 Discovery 是否需要显式触发点？属于哪个边界？** | **Discovery 永久不需要触发点，也不得获得任何授权效力。** 需要触发点的是 **Bootstrap**，其边界 = **执行边界上的独立 Bootstrap 组件**（Worker 在 `runtime.execute(inbound)` 之前调用）；**不是** Runtime 新阶段、**不是** IdentityResolver 内、**不是** Authorization 层 |
| **D4-Q3 未绑定 Identity 的创建是否需要审计？** | **需要，且是硬要求。** initiator / armed_at / scope / expires_at / consumed_at / consumed_by_interaction_id / 两个外部裸键。initiator 是**本方案合法性的组成部分**——没有它，Bootstrap 就退化成 ADR-5 红线所禁的「自动创建」 |

> 附带授权（关闭 CP-1 U-2）：**新增第二个 Identity 创建入口**（`bootstrap.py`，内部路径、
> `user_id=NULL`）由本 ADR 明确授权。管理面 `CreateIdentity` 的 `user_id` 必填语义**不变**。

---

## 6. Identity 语义（写死）

```
Bootstrap 创建的 Identity：
    user_id = NULL      ← 「未与 ACP Account 建立管理绑定」
                          （不是 Anonymous / Guest / System User，也不表示「任何人可用」）

Bootstrap **不得**：
    ❌ 创建 ACP User
    ❌ 把外部消息发送者提升为 ACP User
    ❌ 把该 Identity 绑定到发起 Arm 的 Tenant Admin
    ❌ 任何 sentinel user_id（0 / -1 / 保留 id 兜底）

理由：
    ACP User 与 External Identity 是两个主体（ADR-1）；
    绑定后 IdentityResolution 会把该外部参与者当作 Admin 返回（identity_resolution.py:70-73），
    即把渠道侧动作归属给管理面账号 —— 语义与审计双重错误。
    绑定 = Identity Linking，属未来 Slice 的显式管理动作，Bootstrap 绝不顺带完成。
```

---

## 7. Grant 语义（写死）

```
Bootstrap 只能创建**普通** Grant（一行 ConnectionConversation）：
    同一张表、同一四元组唯一键、同一语义、无新列。

❌ 不得创建 temporary grant / bootstrap grant / 特殊授权类型
❌ 不得给 Grant 加 expires_at（ADR-9 红线；Grant 表禁止该列）
❌ 不得新增授权状态存储

⇒ Bootstrap 的**临时性只来自 Arm Context**，不来自 Grant。
   授权一旦建立，就是普通、可撤销（既有 DELETE）、无有效期的授权。
```

---

## 8. Arm 的固有残余语义与缓解（U-3 关闭）

**残余语义（必须如实登记，不得掩盖）**：Arm 无法预先命名会话键 ⇒ 其真实语义是
「本窗口内该 scope 上第一条通过信任闸门的**单聊**会话，获得一次有界许可」。
若窗口内第一条消息来自**非操作者的陌生人**，被 Bootstrap 的是**那位陌生人的** Identity 与 Grant。

**裁决（关闭 U-3）**：

```
Baseline = S-1（one-shot + 短 TTL + operator 在场 + 事后可撤销）
Optional hardening = S-2（challenge），留独立 Gate；Arm 行预留 challenge 字段位
```

**收敛措施（全部不可省略）**：短 TTL / one-shot / 精准 scope / supersede / 完整审计 /
Grant 普通且可撤销 / **O-5：群会话不消费 Arm**。

**UI 文案义务**：必须如实说明「窗口内第一条消息将被验证」，不得用模糊文案掩盖残余语义
（ADR-10）；**不得**出现 Arm / Bootstrap / 授权词汇（Cloud-0.1.5 G3）。

---

## 9. 组件落点与 Runtime 不变性

```
落点   app/application/bootstrap.py —— 执行边界上的独立组件
       由 Worker 在 claim 之后、runtime.execute(inbound) 之前调用
       不满足 Arm 条件 ⇒ no-op（与今天逐字节等价）

不变   RUNTIME_EXECUTION_ORDER 仍 7 段（一字不改）
       无新 STAGE_* / 无新 failure_code / STAGE_FAILURE_MAP 键集合不变
       失败一律回落既有码（IDENTITY_RESOLUTION_FAILED），不得落 UNEXPECTED_FAILURE

禁止   ❌ IdentityResolver / Authorization / Routing / Session / Coordinator 内创建
       ❌ 新增第八个 Runtime Stage
```

---

## 10. 事务语义（关闭 U-4 的实现形态，CP-3-D §10）

```
BEGIN（tenant-scoped UoW）
  校验 Arm（scope / status='armed' / expires_at > now() / chat_type == 'single'）
  CAS 消费（条件 UPDATE → rowcount == 1）
  find-or-create Identity（user_id = NULL；冲突 → 回读复用）
  find-or-create Grant（普通行；冲突 → 回读复用）
COMMIT
（事务外）Authorization → Routing → Session → Agent → Delivery
```

**Agent / Delivery 失败不回滚授权事实**：授权建立是事实，不是副作用；
下一条消息直接成功，无需重新 Arm。Arm 保证「授权已建立」，**不**保证「首条必然成功」
（First Success = `processing=succeeded × delivery=delivered`，Cloud-0.1.5）。

---

## 11. 机器化守卫要求（本 ADR 的强制项）

| # | 要求 | 说明 |
|---|---|---|
| **E-1** | **ADR-5 守卫语义升级** | 现状 `test_adr_authorization_and_access_model.py:284-289` 的 `_RUNTIME_CHAIN_FILES` 是**固定 4 文件**。这**不足以**证明「整个 runtime 只有被允许的位置能创建 Identity」——把创建放进清单外的 `bootstrap.py` 会**通过守卫而实质违反 ADR-5**（CP-1 §9.4 已点名的守卫规避）。<br>**要求**：改为**全仓扫描 + 显式允许清单**（形态复用既有先例 `test_package_2_step_4…:324-329`：全仓扫 `connection_conversations.create` → `creators == [...]`）。允许清单**恰好**：`identity 创建 ∈ {identities.py（管理面）, bootstrap.py}`、`grant 创建 ∈ {connection_conversations.py（管理面）, bootstrap.py}`。清单外出现即失败 |
| **E-2** | 新增 Arm 前置条件守卫 | 无 Arm ⇒ 无创建 / one-shot / scoped（不跨 tenant / channel / connection）/ 不得创建 User·Agent·Channel·Connection / 不得绕过 Authorization / 群会话不消费 Arm |
| **E-3** | ADR-6 既有断言**不删不改** | `test_adr6_no_implicit_grant_on_first_message` 保留；Arm 前置条件的表达放在 E-2 |
| **E-4** | 契约条目表**只增补** | `test_package_2_step_4_…` 的条目表是 meta-guard，不得删改既有条目 |
| **E-5** | `RUNTIME_EXECUTION_ORDER` 精确元组断言**一处不改** | 这是「不引入新 Runtime Stage」的机器化保证 |

---

## 12. 与 Cloud V0 冻结的关系

```
本 ADR **不修改** docs/cloud-v0-release-freeze.md。
bootstrap_arms（新表）+ Arm 端点（新端点）在冻结文档下默认属 §3.2 的 post-release backlog。
入 V0 必须另有一条显式 **Release Blocker 记录**
（见 docs/cloud-v0-release-blocker-bootstrap-authorization-source.md）。
本 ADR 的**语义效力不依赖**该 blocker 记录；是否进入 V0 只影响**排期**。
```

---

## 13. 冻结原则（英文措辞，供实现与测试引用）

```
PRINCIPLE (frozen):

    A message may trigger Bootstrap **only when** a valid Bootstrap Arm
    was explicitly created **before** the message arrived.

    In other words:
        message  ≠  authorization source
        message  =  bootstrap trigger
        (precondition: the bootstrap authorization context already exists)

    And:
        Bootstrap creates an External Identity (user_id = NULL).
        Bootstrap NEVER creates, promotes, or implies an ACP User.
```

---

## 14. 本 ADR 未做的事

```
❌ 未实现任何代码 / 迁移 / 端点 / 测试（实现属 CP3-F / CP-4）
❌ 未修改 ADR-1 ~ ADR-10 原文、未修改 Package 2-C、未修改 Gate 2.6
❌ 未修改 Cloud V0 冻结文档
❌ 未设计 API 请求/响应字段细节（属实现步）
```

> 本文件为 **FROZEN**。任何修改须以「新增 ADR 编号 + 显式取代关系」方式提交，
> 不得原地改写（`authorization-and-access-model.md:702-703`）。
