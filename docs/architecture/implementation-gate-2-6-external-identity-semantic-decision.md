# Implementation Gate 3 — External Identity Semantic Decision（语义裁决）

> 状态：**FROZEN（语义层）** / Implementation **NOT STARTED**
> 性质：**只做语义裁决**。不实现、不改代码、不改测试、不建 migration、不设计字段命名。
> 「怎么实现」不在本 Gate 范围——本 Gate 只回答「语义上是不是这样定义」。
>
> 输入：
> `docs/architecture/authorization-and-access-model.md`（ADR-1 ~ ADR-10）
> `docs/architecture/implementation-gate-1-authorization-impact-audit.md`
> `docs/architecture/implementation-gate-2-minimal-authorization-design.md`
> `docs/architecture/implementation-gate-2-5-external-identity-lifecycle-audit.md`
>
> 前置已证明（Gate 2.5）：技术上可以，且 **隔离（Tenant/RLS）与 Session 语义都不会被破坏**
> ——因为它们本来就不依赖 User。

```
阶段状态
  Authorization Architecture     G — PASS / FREEZE
  Gate 1  Impact Audit           DONE
  Gate 2  Minimal Authorization  DONE
  Gate 2.5 Identity Lifecycle    DONE（技术可行性已证明）
  Gate 3  Semantic Decision      ← 本文件（只做裁决）
  Implementation                 NOT STARTED
```

---

## 0. 裁决总表（D1 – D7）

| # | 裁决 | 冻结措辞 | 性质 |
|---|---|---|---|
| **D1** | Identity 定义 | **Identity = Channel-scoped External Identity；ACP User association OPTIONAL** | 定义（语义扩张） |
| **D2** | User association 语义 | **`user_id = NULL` = 未与 ACP Account 建立管理绑定**；不是 Anonymous / Guest / System User | 定义（反滥用约束） |
| **D3** | Resolution 成功条件 | **Identity exists + Channel matches + External ID matches → RESOLVED**；User 是否存在**不再**决定成败 | 语义变更（对现有 8.2/9.2 契约的**定向修订**） |
| **D4** | Lifecycle | **`Identity exists` ≠ `Identity is automatically created`**；本 Gate **不裁决**自动创建策略 | 边界隔离 |
| **D5** | Session | `Session.identity_id` 仍是**唯一**的 Identity 依赖；Session **不新增** `user_id`；隔离键不变 | **no-change freeze** |
| **D6** | Tenant / RLS | `Identity.tenant_id` 仍是**直接**租户归属来源；**不得**变成 `Identity → User → Tenant` | **no-change freeze** |
| **D7** | Management | **「创建/发现 External Identity」与「绑定到 ACP User」是两个不同语义**；未来若都需要，必须分开，不得继续合并在一个 `CreateIdentity(user_id, ...)` 里 | 语义拆分（实现形态未决） |

**一句话**：SteeringFlow 正式把「未绑定 ACP User 的 External Identity」定义为
**合法的一等运行时身份**（Channel-scoped），把 **ACP User 关联降级为可选的管理绑定**，
而不是身份存在性的前提。

---

## D1 — Identity 定义

### 裁决（冻结）

```
Identity = Channel-scoped External Identity

    Channel  1 ────────── N  Identity
                                 │
                                 └── ACP User association: OPTIONAL（可选）

唯一键（不变）：(tenant_id, channel_id, external_id)
```

- Identity 是**运行时一等身份**：它自身足以参与 Identity Resolution、承载 Session 归属、
  参与审计事实，**不需要先有 ACP User**。
- `external_id` **不是全局身份键**：它只在 Channel Scope 内解释
  （`WeCom/zhangsan` 与 `Feishu/zhangsan` 是两个 Identity）。

### 理由

- ADR-1 已冻结「External Identity 不要求拥有 ACP User」；本决策把它从**原则**落到
  **模型定义**层。
- Gate 2.5 已证明：`identities.tenant_id` 是自有列、RLS 直接按该列判定、
  Session 只依赖 `identity_id`、运行时从不消费 `resolved.user`
  → **现有结构本来就支持这个定义**，缺的只是 `user_id` 的非空约束与其契约措辞。

### 推论（实现约束）

- 现有 `IdentityRepository` 端口的措辞（「按 Identity 的**外部身份语义**命名……
  而不是按 `external_id` 全局查找——后者会隐含『跨渠道同一人』的错误假设」）
  **与新定义一致**，无需反向修订；
- `display_name` / `status` / `metadata` 仍属 Identity 自身的档案字段，与绑定无关。

### 红线

```
❌ 不把 external_id 变成全局身份键（不做跨渠道合并）
❌ 不引入「跨渠道同一人」的隐式假设
❌ 不为了定义而新增实体（不新增 Participant / Principal / Subject 等第二套身份对象）
```

---

## D2 — User association 语义

### 裁决（冻结）

```
user_id = NULL
    ⇔  External Identity 未与 ACP Account 建立管理绑定（unbound）

不是：
    ✗ Anonymous Identity
    ✗ Guest Identity
    ✗ System User
    ✗ 「公共 / 任何人可用」的身份
```

**NULL 只表达「无绑定」，不表达任何主体类型。**

### 理由

`user_id = NULL` 若被解释为 Anonymous/Guest/System，会立刻引入一个**隐式的共享主体**：
所有未绑定身份塌缩成同一个「人」，从而伪造出「同一个人在多渠道」的结论——
这正是 6.6 明确禁止的自动 Identity Linking 的反面版本（不合并名字，但合并主体）。

### 推论（实现约束）

- **禁止 sentinel**：不得用 `user_id = 0` / `-1` / 内置保留 id 代替 NULL；
- 不得新增「默认 User」「系统 User」「Guest User」作为兜底绑定；
- 未绑定身份的「是谁」只有一个事实来源：`(channel_id, external_id)`。

### 红线

```
❌ AnonymousIdentity / GuestIdentity / SystemUser / ServiceAccount 实体
❌ 以 sentinel id 或兜底 User 代替 NULL
❌ 把「未绑定」解释为「任何人可用」（那是授权问题，不是身份问题）
❌ 自动 Linking / fuzzy / email / phone 匹配（6.6 既有禁令，继续有效）
```

---

## D3 — Resolution

### 裁决（冻结）

```
Identity Resolution 成功条件：

    Identity exists
  + Channel matches
  + External ID matches
        ↓
    RESOLVED

而：User exists / does not exist  →  不再决定 Resolution 成败
```

（Identity 未命中 → 仍然 Fail Closed，`IDENTITY_RESOLUTION_FAILED`。）

### 理由

归属判定需要的是「这条入站属于哪个 Identity」，不是「这个 Identity 有没有管理账号」。
把 User 存在性塞进解析成功条件，会让「未绑定」等价于「解析失败」——
**身份存在却被判为不存在**，与 D1（Identity 是一等身份）直接矛盾。

### 与现有冻结的关系（必须显式修订，不得静默）

本裁决**改变了已冻结的 8.2/9.2 语义**，受影响面（Gate 2.5 已定位的 F2）：

| 受影响物 | 现有措辞 / 行为 | 修订方向 |
|---|---|---|
| `IdentityResolutionFailedError`（`app/application/errors.py`） | docstring：「未找到 … **或找到 Identity 但其 user 缺失**时抛出」 | 失败条件收窄为「Identity 未命中」 |
| `application/identity_resolution.py::execute` | `user = uow.users.get_by_id(...)`；`user is None` → raise | 不再以 User 缺失为失败 |
| `domain/identity_resolution.py` | `IdentityResolution.user: User`（非可选） | 关联变为可选（或拆出 identity-only 解析） |
| 测试 | `test_step9_2::test_b_identity_without_user_fails_closed` 等 | 按新语义改写 |

> **⚠️ 已知偏差（必须记账，不得静默）**：**当前代码与 D3 不一致**——它仍会在
> User 缺失时 Fail Closed。本 Gate 只冻结**目标语义**；实现前，现状行为是
> **已登记的偏离**（F2，NOT STARTED）。因此**现在不要写代码守卫来断言 D3**
> （必然失败），也**不得**把「当前行为」误述为「符合裁决」。

### 红线

```
❌ 为让解析成功而创建 User / Identity（ADR-5 继续有效）
❌ fuzzy / 猜测 / 兜底 User
❌ 静默修订 8.2/9.2 措辞（必须显式、可追溯）
```

---

## D4 — Lifecycle

### 裁决（冻结）

```
Identity exists                    ≠   Identity is automatically created

「允许无 User 的 Identity 存在」      「系统在某个路径上自动创建 Identity」
```

本 Gate **只裁决「存在性合法性」**，**不裁决自动创建策略**。

### 理由

这两件事极易被偷渡混合：一旦「Identity 可以独立存在」被写下，很容易顺手推出
「所以收到消息时创建一条 Identity 就行了」——那会直接推翻 ADR-5 与 9.2 的
「绝不创建」纪律。

### 现状（不变）

- 运行时：`IdentityResolutionExecutor` **绝不创建** Identity / User（9.2 冻结）；
- 全仓**唯一**创建路径是管理面 `IdentityApplicationService.create_identity`（6.6）。

### **明确未裁决**（留给后续独立 Gate）

```
?  谁有权创建「未绑定 Identity」？（管理面显式动作 / 未来 Discovery 动作 / 两者）
?  Discovery 是否需要一个显式触发点？（若需要，它属于哪个边界）
?  未绑定 Identity 的创建是否需要审计要求？
```

### 红线

```
❌ 不得以「Identity 可独立存在」为由，在任何运行路径引入自动创建
❌ 不得在本 Gate 顺手定义「首次入站即建立 Identity」的策略
```

---

## D5 — Session（no-change freeze）

### 裁决（冻结）

```
Session.identity_id                         ← 仍然是唯一的 Identity 依赖
Session                                     ← 不新增 user_id
隔离键 (identity_id, connection_id, external_conversation_id)  ← 语义不变
```

### 理由

Gate 2.5 已证明：`sessions.identity_id` FK 到 `identities.id`，唯一键含 `identity_id`，
创建与解析全链路不涉及 User；`RuntimeCoordinator` 只消费 `resolved.identity.id`。
因此「Identity 独立于 User」**对 Session 零影响**——**不需要任何变更**。

### 红线

```
❌ Session 新增 user_id
❌ 以 User.id 作为 Session actor（8.3 冻结：actor 是 Identity.id）
❌ 修改三维隔离键或去掉 external_conversation_id
```

---

## D6 — Tenant / RLS（no-change freeze）

### 裁决（冻结）

```
Identity.tenant_id        ← 仍然是 Identity 的直接租户归属来源（自有列）
Identity → User → Tenant  ← 不得出现
```

### 现状证据（Gate 2.5）

| 事实 | 位置 |
|---|---|
| `identities.tenant_id` NOT NULL + FK + index（自有列） | `persistence/models/identity.py`；`alembic/versions/0002_tenant_isolation.py`（`identities ∈ CORE_TABLES`） |
| RLS 谓词直接是 `tenant_id = current_setting('app.tenant_id')::integer` | 0002（+ 0005 同表达式 `ALTER POLICY`） |
| Repository 全部读方法按 `IdentityModel.tenant_id` 过滤 | `persistence/repositories/identity.py` |
| 创建时的 tenant 来自**操作者 TenantScope**（不是被绑定 User） | `main.py`：`_identities_service(scope=Depends(tenant_scope_provider))` |

### 推论

- 未绑定 Identity 的租户归属 = **创建动作所在的 TenantScope**；
- 该归属**不依赖** `user_id`，也不得回退到任何 User 推导。

### 红线

```
❌ 任何形式的 Identity → User → Tenant 推导
❌ 让 user_id 参与租户判定
❌ 关闭/绕过 RLS 或改变 tenant_isolation 谓词列
```

---

## D7 — Management

### 裁决（冻结）

**两个动作是两个不同语义，必须分开：**

```
A. Create / discover External Identity        「这条外部身份存在」
B. Bind External Identity to ACP User         「这条外部身份属于哪个 ACP Account」

现状：CreateIdentity(user_id, ...) 把 A 与 B 合并为一个动作
裁决：未来若两者都需要支持，必须是两个不同语义，不得继续合并
```

### 现状证据（Gate 2.5）

`IdentityApplicationService.create_identity`：
必填 `user_id` → `users.get_by_id` 存在性校验 → `UserNotFoundError` →
`Identity(user_id=…)` + `(channel_id, external_id)` 唯一键。
即：**「身份本体」与「管理绑定」当前是一个动作**；
HTTP 契约 `IdentityCreate.user_id: int` 必填；`identity.user_id` 在 update 中不可变。

### 本 Gate 只冻结「必须分开」，**不裁决实现形态**

```
?  是否需要两个端点 / 两个 Application 动作 / 一个动作 + 可选参数   → NOT DECIDED
?  Bind 是否允许改绑 / 解绑（Linking / Unlinking）                 → NOT DECIDED
?  已存在的合并合同（POST /api/identities）如何演进/兼容             → NOT DECIDED
```

### 红线

```
❌ 把「绑定」塞进发现路径（发现即绑定）
❌ 把「创建身份」塞进绑定路径（绑定即隐式创建）
❌ 继续用一个 CreateIdentity(user_id, ...) 同时表达 A 与 B
❌ 自动 Linking / 自动绑定
```

---

## 9. 现状语义 vs 冻结语义（对照表）

| 维度 | 现状（代码事实） | 本 Gate 冻结语义 | 差异性质 |
|---|---|---|---|
| Identity 定义 | 「外部身份 → User 的绑定」（`user_id` 非可选） | Channel-scoped External Identity，User 关联可选 | **语义扩张**（D1） |
| `user_id = NULL` | 不存在（NOT NULL） | = 未绑定；非 Anonymous/Guest/System | **新增语义**（D1/D2） |
| Resolution 成功条件 | Identity 命中 **且** User 存在 | Identity 命中（channel + external_id）即成功 | **语义变更**（D3） |
| 自动创建 | 运行时零创建；管理面唯一创建 | 不变；且**明确不裁决**策略 | 不变 + 边界隔离（D4） |
| Session 依赖 | `identity_id` | 不变 | **无变更**（D5） |
| Tenant / RLS | `identities.tenant_id` 直接归属 | 不变 | **无变更**（D6） |
| Management | 创建 + 绑定合一 | 必须分开（形态未决） | **语义拆分**（D7） |

---

## 10. 影响面映射（语义 → 实现 delta，全部 NOT STARTED）

| Delta | 由哪条裁决推出 | 内容边界（Gate 2.5 F1/F2/F3 对应） | 状态 |
|---|---|---|---|
| **F1** | D1 / D2 | Schema：`identities.user_id` → 可空（**migration**）；连带领域类型 / Mapper / 响应契约；FK `ondelete` 裁决 | **NOT STARTED** |
| **F2** | D3 | 运行时：`IdentityResolution.user` 可选化 + 不再以 User 缺失为失败 + `IdentityResolutionFailedError` 措辞收窄 + 测试改写 | **NOT STARTED** |
| **F3** | D7 | 管理面：Create/Discover 与 Bind 的语义拆分（形态未决） | **NOT STARTED** |
| — | D5 / D6 | **无 delta**（no-change freeze） | — |
| — | D4 | **无 delta**（自动创建策略明确未裁决） | — |

> **顺序要求**：F1 与 F2 必须**同一变更内**落地，否则中间态会出现
> 「Schema 允许 NULL 但运行时仍 Fail Closed」（无功能意义）或
> 「运行时不再要求 User 但 Schema 仍 NOT NULL」（直接报错）。
> F3 可独立于 F1/F2 推进（但若 Bind 语义变化，须与 F1 的字段可空性一致）。

---

## 11. 与既有裁决的一致性检查

| 既有裁决 | 是否冲突 | 说明 |
|---|---|---|
| ADR-1（External Identity 不要求拥有 ACP User） | **不冲突，被落实** | D1/D2 把它落到模型定义 |
| ADR-3（授权作用域 = Conversation） | **无关** | 授权不消费 Identity（Gate 2 D3） |
| ADR-5（不自动创建 ACP User） | **不冲突，继续有效** | D4 明确隔离「存在性」与「自动创建」 |
| 8.2 / 9.2（Identity Resolution Fail Closed） | **定向修订**（D3） | 必须显式、可追溯；登记为 F2 偏差 |
| 8.3（Session actor = Identity.id） | **不冲突，被强化** | D5 no-change freeze |
| 6.6（Identity Management ≠ Identity Resolution） | **不冲突，被强化** | D7 在管理侧继续拆分（A/B 两动作） |
| Cloud-0.1.1-B（tenant 两层防线） | **不冲突** | D6 冻结两层防线均基于 `tenant_id` 列 |
| ADR-9（V0 不做复杂权限系统） | **不冲突** | 本 Gate 不引入任何权限/策略概念 |

---

## 12. 机器化护栏（**本 Gate 不写**，实现步再写）

**为什么现在不写**：D1/D2/D3 是**目标语义**，与当前代码**故意不一致**——
此时写代码守卫必然失败（例如「断言 `identity.user_id` 可为空」会失败）。
纪律要求：**不得**为了让守卫通过而提前改代码，也**不得**写一个必然失败的守卫。

**实现步应落地的守卫范围**（供后续 Gate 使用，本文件只登记）：

```
(a) 无 Anonymous / Guest / SystemUser / ServiceAccount 类
(b) 无 sentinel user_id 用法（0 / -1 / 保留 id 兜底）
(c) 无「Identity → User → Tenant」推导（Identity 读路径只以 tenant_id 过滤）
(d) sessions 表不新增 user_id；三维隔离键约束名与列面不变
(e) 运行时链路仍零创建 Identity / User（ADR-5 不回归）
(f) D3 生效后：Identity 命中即解析成功（User 缺失不再 Fail Closed）
(g) D7 拆分后：创建/发现与绑定不在同一动作内
```

---

## 13. 本次未做（范围声明）

```
❌ 未改生产代码 / 未改测试 / 未改冻结契约 / 未建 migration
❌ 未把 user_id 改成可空（只裁决「语义上应如此」）
❌ 未裁决自动创建策略（D4 明确排除）
❌ 未裁决 Management 的实现形态（D7 只冻结「必须分开」）
❌ 未裁决 F1/F2/F3 的落地顺序与排期（仅标注顺序要求）
❌ 未设计任何新对象、字段命名、端点或错误码字面量
❌ 未触碰 Authorization（Gate 2 范围）与 Runtime order
❌ 未写任何机器化护栏
```

**本文件为语义裁决冻结，不构成实现批准。** 下一步是
**Implementation Gate 4（External Identity Lifecycle 实现设计）**：
在 D1–D7 之下设计 F1/F2/F3 的具体变更边界，并同步 Targeted Tests。
