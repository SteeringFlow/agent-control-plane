# Implementation Gate 2.5 — External Identity Lifecycle Audit（只读审计）

> 性质：**只读审计**。未改代码、未建 migration、未改契约、未改测试。
> 本文件**只回答一个问题**：
>
> **「我们能不能在不破坏现有 ACP Core 隔离与 Session 语义的情况下，
> 让 External Identity 独立于 ACP User 存在？」**
>
> 不讨论 Authorization（那是 Gate 2 的范围），不设计实现。

```
阶段状态
  Authorization Architecture      G — PASS / FREEZE
  Read-only Impact Audit          DONE（Gate 1）
  Minimal Authorization Design    DONE（Gate 2）
  External Identity Lifecycle     ← 本文件（Gate 2.5，只读审计）
  External Identity 决策          NOT STARTED
```

---

## 0. 结论先行

| 问题 | 答案 |
|---|---|
| **隔离（Tenant / RLS）会不会被破坏？** | **不会。** `identities` 自带 `tenant_id` 列（NOT NULL + FK + index），RLS 策略**直接以该列为谓词**——**不存在** `Identity → User → Tenant` 的间接隔离链 |
| **Session 语义会不会被破坏？** | **不会。** Session 只依赖 `identity_id`，**不依赖 User**；且 `resolved.user` 在整个运行时**从未被消费**（纯校验，非数据流） |
| **那 blocker 到底是什么？** | **只有 `identities.user_id` 的 NOT NULL（及围绕它的 3 处契约）**。它不是隔离问题、不是 Session 问题，是**一个 Schema 约束 + 两处契约语义**问题 |
| **能不能做？** | **技术上可以，且不破坏隔离与 Session。** 代价 = **1 次 migration + 3 处冻结面契约 + 1 套新管理语义** |
| **该不该现在做？** | **取决于产品是否要求「未纳管的渠道参与者也能与 Agent 交互」。** 若接受 Gate 2 的 D1/R-A（未绑定者不进入执行），则**现在不需要**——Authorization 本身也不依赖它（Gate 2 D3） |

### 对 Gate 2 的一处**精化**（诚实记账）

Gate 1/Gate 2 曾表述「真正的 blocker 是 `sessions.identity_id NOT NULL`」。Gate 2.5 的精确结论是：

```
Session 依赖的是「存在一条 Identity」，而不是「该 Identity 有 User」。

  Identity（无 User） ──→ Session 完全成立（identity_id 有值、唯一键齐全、创建/解析不需要 User）
  没有 Identity       ──→ Session 无法建立（这才是 Gate 2 R-A 下的真实阻断点）
```

因此：**只要允许「无 User 的 Identity」存在，Session 就没有任何 blocker；
反过来，若坚持 R-A（不建 Identity），Session 才成为阻断点。**
Blocking 与否取决于**是否创建 Identity**，而不是取决于 **User**。

---

## A. Identity 当前真实语义（从真实代码，不看文档措辞）

### A.1 字段面（三处一致定义）

| 层 | 位置 | 字段 |
|---|---|---|
| Domain | `app/domain/identity.py` | `user_id: int`（**非可选**）、`channel_id: int`、`external_id: str`、`id`、`display_name`、`status="active"`、`metadata` |
| ORM | `app/persistence/models/identity.py` | `user_id` **NOT NULL** FK `users.id`、`channel_id` NOT NULL FK、`external_id` NOT NULL、`display_name`、`status`、`metadata`(列名 `metadata`)、`created_at/updated_at`、**`tenant_id` NOT NULL FK `tenants.id`（自有列）** |
| 唯一键 | 同上 `__table_args__` | `UNIQUE(tenant_id, channel_id, external_id)` |

### A.2 Identity 到底是「什么」

**结论：Identity 在代码语义上是「已纳管绑定（binding record）」，不是「外部参与者实体」。**

三条证据（都是代码事实，不是文档说法）：

1. **`user_id` 是骨架字段且非可选**：Domain 类型是 `int` 而非 `int | None`；
   Mapper 的 `apply_to_orm` 明确注释「id / user_id / channel_id / external_id 保持不变——
   绑定关系不属于 update 可改的字段」；
2. **唯一键约束的是「绑定」的唯一性**：`(tenant_id, channel_id, external_id)`
   —— 表达的是「某渠道内的某外部身份只能有一条绑定」，而不是「外部身份本身」的唯一性；
3. **没有一条创建路径能产出「无 User 的 Identity」**：全仓只有
   `IdentityApplicationService.create_identity` 一处创建（见 F），它**必填 user_id**。

> 语义含义（**事实推论，不是设计建议**）：当前模型把「外部参与者」与
> 「参与者 ↔ User 的绑定」**合并成了同一个对象**。因此「让 External Identity 独立于
> User 存在」在语义上等价于「把这两个概念拆开」——这是本审计发现的最重要结构性事实。

---

## B. `identity.user_id` / `identity.user` 被假定为 NOT NULL 的**全部**位置（穷举）

| # | 位置 | 假定形式 | 类型 |
|---|---|---|---|
| 1 | `domain/identity.py` | `user_id: int`（类型不可空） | 领域类型 |
| 2 | `persistence/models/identity.py` | `user_id ... ForeignKey("users.id"), nullable=False, index=True` | Schema |
| 3 | `persistence/mappers/identity.py::to_domain` | `user_id=row.user_id` | 映射 |
| 4 | `persistence/mappers/identity.py::to_orm` | `user_id=entity.user_id` | 映射 |
| 5 | `application/identity_resolution.py::execute`（运行时） | `user = uow.users.get_by_id(identity.user_id)`；`user is None` → **`IdentityResolutionFailedError`** | **运行时策略** |
| 6 | `domain/identity_resolution.py` | `IdentityResolution.user: User`（**非可选**） | 领域契约（8.2 冻结） |
| 7 | `application/identities.py::create_identity` | `_require_positive_id(input.user_id)` + `users.get_by_id(...) is None` → `UserNotFoundError` | 管理契约（6.6 冻结） |
| 8 | `schemas/identities.py` | `IdentityCreate.user_id: int`（必填，`extra="forbid"`）；`IdentityResponse.user_id: int` | HTTP 契约（12.4 冻结） |
| 9 | `api/management/identities.py` | `POST /api/identities` 直接 `CreateIdentity(**payload.model_dump())` | 管理 API |
| 10 | 测试（冻结断言） | `test_step6_6::test_create_identity_rejects_missing_user`、`test_create_identity_rejects_invalid_input`（`user_id=0/-1`）、`test_step9_2::test_b_identity_without_user_fails_closed` | 机器化锁定 |

**穷举结论**：`user_id` 的 NOT NULL 假定分布在 **领域类型 / Schema / 映射 / 运行时策略 /
管理契约 / HTTP 契约 / 测试** 七层，**没有一处**与 Tenant 或 Session 的隔离逻辑相关。

**另一个关键事实（数据流层面）**：

```
IdentityResolutionExecutor 取回 user  →  放进 IdentityResolution(identity, user)
RuntimeCoordinator.execute 只使用：resolved.identity.id
                                       ↓
                        全运行时链路没有任何地方消费 resolved.user
```

即：**User 的查询当前是「纯校验（gate）」，不是「数据流」**。
（`coordinator.py` 中仅出现 `resolved.identity.id`；`app/application/` 全目录 grep
无 `resolved.user` 消费点。）

---

## C. Session Dependency — Identity 是否可以脱离 User？

### C.1 Session 侧的全部依赖（真实代码）

| 位置 | 事实 | 是否涉及 User |
|---|---|---|
| `persistence/models/session.py` | `identity_id` **NOT NULL** FK `identities.id`；唯一键 `UNIQUE(tenant_id, identity_id, connection_id, external_conversation_id)` | **否**（只 FK 到 identities） |
| `application/session_creation.py::SessionCreationExecutor.execute` | Fail Closed：`identity_id / channel_id / connection_id / agent_id` 必须 `> 0`；写入 `Session(identity_id=...)` | **否** |
| `application/session_resolution.py::SessionResolutionExecutor.execute` | 以 `(identity_id, connection_id, external_conversation_id)` 查 `find_active` | **否** |
| `application/coordinator.py` | `identity_id = resolved.identity.id` | **否** |
| `domain/session.py` | 隔离键 = `Identity.id × Connection.id × external_conversation_id`（actor 是 Identity，**不是 User**） | **否**（8.3 明确裁决） |

### C.2 结论

```
Session 需要：一条 Identity 行（有 id）
Session 不需要：该 Identity 上有 User
```

**因此「Identity 无 User」对 Session 语义的影响 = 0**：唯一键、创建、解析、Dispatch
所需 `control_plane_session_id` 全部照常成立。

**注：** 运行时的阻断点在于 `IdentityResolutionExecutor`（B#5）：它在 User 缺失时
**主动** Fail Closed。这是**策略选择**，不是 Session 的结构依赖。

---

## D. Tenant Ownership — 若 Identity 没有 User，它属于谁？

| 事实 | 位置 | 含义 |
|---|---|---|
| `identities.tenant_id` 是**自有列**（NOT NULL + FK `tenants.id` + index） | `persistence/models/identity.py`；`alembic/versions/0002_tenant_isolation.py`（`identities` ∈ `CORE_TABLES`） | Identity **直接**拥有租户归属 |
| 运行时/管理面读 Identity 时按 `IdentityModel.tenant_id` 过滤 | `persistence/repositories/identity.py`（`_tenant_id` 参数，全部 4 个读方法均应用） | 隔离**不经过** User |
| 管理面创建 Identity 时的 tenant 来源 | `main.py`：`_identities_service(scope = Depends(tenant_scope_provider))` → `_tenant_scoped_factory(scope)` | tenant 来自**操作者的 TenantScope**，**不是**从 `identity.user_id` 推导 |
| 全仓无任何「由 user_id 推导 Identity 的 tenant」的代码 | grep 无命中 | 二者之间**本来就没有派生关系** |

**结论：`user_id = NULL` 不会导致 Identity 失去 Tenant ownership。**
租户归属是 `identities.tenant_id` 的列级事实，与 User 无关。

> 唯一与 User 相关的「租户解析」在 **Browser Auth** 域（登录：User → tenant_id），
> 那是**操作者身份**链路，与 `identities` 表的数据归属无关。

---

## E. RLS — 当前 Identity 的隔离是否通过 User 间接实现？

### E.1 真实 RLS 定义

```sql
-- alembic/versions/0002_tenant_isolation.py（仅 PostgreSQL）
CORE_TABLES = ("users", "identities", "channels", "agents",
               "connections", "sessions", "interactions", "activities")

for t in CORE_TABLES:
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY
    ALTER TABLE {t} FORCE ROW LEVEL SECURITY
    CREATE POLICY tenant_isolation ON {t}
        USING      (tenant_id = current_setting('app.tenant_id')::integer)
        WITH CHECK (tenant_id = current_setting('app.tenant_id')::integer)
```

（`0005_tenant_isolation_rls_robustness.py` 以同一表达式 `ALTER POLICY`，语义不变。）

### E.2 结论

**不存在 `Identity → User → Tenant` 的 RLS 链。** 策略谓词直接是
`identities.tenant_id = current_setting('app.tenant_id')`：

```
Identity 的隔离路径（真实）
   identities.tenant_id  ←──── RLS policy 直接判定
                                （第二层防线）
   IdentityRepository._tenant_id 过滤（第一层防线）

不是：
   identities → users → tenants   ❌（代码中不存在这条链）
```

**因此 `user_id = NULL` 对 RLS 的影响 = 0。**

### E.3 一个必须记录的既有约束（与 user_id 无关，但会影响未来设计）

- bootstrap 策略只有两张表有（`0002` 的 `tenants_bootstrap`、`0004` 的 `channels_bootstrap`）；
  **`identities` 没有 bootstrap 策略** → Identity 的读取**必须在已设 `app.tenant_id` 的
  上下文内**。
- 现状自洽：入站路径先「定位 Channel（bootstrap）→ 验签 → 解析 tenant →
  tenant-scoped UoW」，Identity 解析发生在 tenant 已确定之后；Worker 也是 per-tenant。
- 未来含义：任何「在 tenant 未知时解析 Identity」的设计都会撞上这条 RLS 边界——
  这是既有硬约束，不是 user_id 引入的。

---

## F. Management Semantics — `create_identity` 到底在做什么？

### F.1 真实行为（`IdentityApplicationService.create_identity`）

```
1. _require_positive_id(user_id)      → user_id 必须为正整数（否则 InvalidIdentityInputError）
2. _require_positive_id(channel_id)
3. _require_non_blank(external_id)
4. users.get_by_id(user_id) is None   → UserNotFoundError（零写入）
5. channels.get_by_id(channel_id) is None → ChannelNotFoundError（零写入）
6. identities.find_by_channel_and_external_id 命中 → DuplicateIdentityError
7. uow.identities.create(Identity(user_id=…, channel_id=…, external_id=…)) + commit
```

配套约束（同层冻结）：
- HTTP 契约 `IdentityCreate.user_id: int` **必填**（`extra="forbid"`）；
- `UpdateIdentity` **不含** `user_id/channel_id/external_id`（绑定不可变，改绑定 = Identity Linking，属未来 Slice）；
- 测试锁定：无 User → `UserNotFoundError` 且零写入（`test_step6_6`）。

### F.2 结论：它在创建**「已纳管的 External Identity」**

| 语义候选 | 是否成立 | 依据 |
|---|---|---|
| **创建 External Identity（参与者本身）** | **不成立** | 若如此，`user_id` 不应必填、User 缺失不应报错 |
| **创建「已纳管 External Identity」（绑定）** | **成立** | 必填 user_id + User 存在性校验 + 绑定骨架不可变 + 唯一键是绑定唯一性 + docstring「外部身份桥梁 / external identity → User 的管理绑定」 |

**因此：「未纳管的外部身份」在当前系统中无法被表示**——它既不能经管理 API 创建，
也不能经运行时被创建（ADR-5：运行时禁止创建 Identity/User；9.2 明确「绝不创建」）。

---

## G. 综合依赖矩阵（Identity ↔ X）

| 关系 | 是否依赖 User | 证据 | user_id 置 NULL 的影响 |
|---|---|---|---|
| **Identity ↔ Tenant** | **否** | `identities.tenant_id` 自有列（NOT NULL + FK）；管理面 tenant 来自操作者 scope | **无**（归属保持） |
| **Identity ↔ RLS** | **否** | 策略谓词直接是 `tenant_id` 列 | **无**（隔离保持） |
| **Identity ↔ Session** | **否** | `sessions.identity_id` FK → identities；唯一键含 identity_id；创建/解析不用 User | **无**（Session 照常成立） |
| **Identity ↔ Runtime 策略** | **是**（策略层） | `IdentityResolutionExecutor`：User 缺失 → Fail Closed；`IdentityResolution.user` 非可选 | **有**（需改策略 + 契约） |
| **Identity ↔ Domain 类型** | **是** | `Identity.user_id: int`；`IdentityResolution.user: User` | **有**（类型需可空/拆解） |
| **Identity ↔ Management API** | **是** | `IdentityCreate.user_id` 必填 + UserNotFoundError + 测试 | **有**（契约 + 校验变更） |
| **Identity ↔ Schema** | **是** | `user_id` NOT NULL FK | **有**（migration） |
| **Identity ↔ FK 生命周期** | 尚未被行使 | 全 `app/api/**` **无任何 `@router.delete`**（0 命中）→ 当前不存在删除 User 的路径 | FK `ondelete` 语义尚未使用；一旦 user_id 可空需显式裁决 |
| **Identity ↔ Interaction/Activity** | **否** | 两张表都不引用 identity_id/user_id（interactions 有 channel_id/session_id；activities 有 interaction_id/session_id） | **无** |

---

## H. 最终回答（唯一问题）

> **我们能不能在不破坏现有 ACP Core 隔离与 Session 语义的情况下，
> 让 External Identity 独立于 ACP User 存在？**

**能——隔离与 Session 都不会被破坏**，因为它们**本来就不依赖 User**：

```
Tenant ownership  ：identities.tenant_id 自有列           → 与 User 无关  ✅
RLS               ：policy 直接判定 tenant_id 列          → 与 User 无关  ✅
Session           ：只 FK 到 identities 的 id             → 与 User 无关  ✅
Runtime 数据流     ：resolved.user 从未被消费（纯校验）      → 与 User 无关  ✅
```

**但代价是三处冻结面 + 一次 migration**（因为 NOT NULL 假定横跨
领域类型 / Schema / 运行时策略 / 管理契约 / HTTP 契约 / 测试 七层）：

| # | 面 | 需要变更的内容（边界描述，不是实现方案） | 冻结依据 |
|---|---|---|---|
| **F1** | Schema | `identities.user_id` NOT NULL → 可空（**migration**）；连带：领域类型 `int → int \| None`、Mapper 两处、`IdentityResponse.user_id`、FK `ondelete` 语义裁决 | 4.2 / 0002 | 
| **F2** | 运行时契约 | `IdentityResolution.user` 变可选（或拆出 identity-only 解析）；`IdentityResolutionExecutor` **不再**因 User 缺失而 Fail Closed → 改变 `IDENTITY_RESOLUTION_FAILED` 的语义边界 | **8.2 / 9.2** + `test_step8_2` / `test_step9_2` |
| **F3** | 管理契约 | `create_identity` 支持「无 User 纳管」：`CreateIdentity.user_id` 可选、去掉 UserNotFoundError 分支 | **6.6 / 12.4** + `test_step6_6` |

**并且：这不是 Gate 2 的 minimal 变更**（Gate 2 的结论是 0 表 / 0 列 / NO migration，
且 Authorization 不消费 Identity）。它属于**独立的产品语义决策**：

- 若产品**接受** Gate 2 的 D1/R-A（未绑定参与者不进入执行）
  → **现在不需要**做 F1/F2/F3；
- 若产品**要求**「未纳管的渠道参与者也能与 Agent 对话」
  → 必须做 F1/F2/F3（三者缺一不可：只改 Schema 会被运行时策略挡住，只改策略会有 Schema 报错）；
  并且要同时明确：ADR-5（不自动创建）**仍然成立**——「允许无 User 的 Identity 存在」
  ≠「系统自动创建 Identity」，创建仍需显式动作。

---

## I. 与既有裁决的一致性检查

| 既有裁决 | F1/F2/F3 是否冲突 |
|---|---|
| ADR-1（External Identity 不要求拥有 ACP User） | **不冲突**；F1/F2/F3 正是把它落到 Schema/契约层 |
| ADR-5（Authorization 不自动创建 ACP User） | **不冲突**；未纳管 Identity 的创建仍需显式动作（运行时禁止创建不变） |
| ADR-3（授权作用域 = Conversation） | **不冲突且无关**；授权不消费 Identity（Gate 2 D3） |
| 8.3（Session actor 是 Identity.id，不是 User.id） | **不冲突**；F1 只放开 User，不动 Session 键 |
| Cloud-0.1.1-B（tenant 隔离两层防线） | **不冲突**；两层都基于 `tenant_id` 列，与 user_id 无关 |
| 6.6 / 8.2「Identity Management ≠ Identity Resolution」 | **不冲突**；但 F2/F3 分别落在两侧，**必须分开改**，不得合并成一个 Service |

---

## J. 本次未做（只读声明）

```
❌ 未改生产代码 / 未改测试 / 未改冻结契约 / 未建 migration
❌ 未把 user_id 改成可空（仅判定「能不能」与「代价是什么」）
❌ 未设计 External Participant 与 Binding 的拆分方案（未设计任何新对象）
❌ 未裁决 F1/F2/F3 是否要做（属独立的产品语义决策）
❌ 未触碰 Authorization（Gate 2 范围）与 Runtime order
❌ 未提出实现步骤、文件清单或字段命名
```

**本文件为只读审计产出，不构成实现批准。** 若决定推进 F1/F2/F3，须先有一次
独立的 Gate（语义决策 + 契约变更清单 + 回归证明），再进入 Implementation。
