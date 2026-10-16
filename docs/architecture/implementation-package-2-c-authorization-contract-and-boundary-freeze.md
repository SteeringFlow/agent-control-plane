# Implementation Package 2-C — Authorization Contract & Implementation Boundary Freeze

> 状态：**只读设计**。未改生产代码、未改测试、未建 migration、未改 schema、未改冻结契约。
> 性质：**Package 2 的最后一个纯设计 Gate**。落入 6 项 Required Decisions 的裁决，
> 并产出可直接实现的 production contract。
> 继承：ADR-1 ~ ADR-10（FROZEN）、Gate 1 / 2 / 2.5 / 2.6 / 3、Package 1（`f43689a`）、
> Gate 2-A、Package 2-B。
>
> 上游裁决（本次即为此 6 项的正式化）：
> D-1 ADR-11 签发 · D-2 加性 UNIQUE 接受 · D-3 独立资源 · D-4 known-conversations 进 V0（只读）·
> D-5 Session status 假设登记 · D-6 Fixture Impact Inventory（分类，不改 55 个文件）

---

## 0. 结论

```
Package 2-B                        = PASS / DESIGN FROZEN
Package 2-C（本文件）               = 最后一个纯设计 Gate → FREEZE
6 项 Required Decisions            = 全部关闭（§1–§6）
Production Contract                = 完整（§7）
Migration 0007 = schema + RLS + backfill       = 闭合
Runtime = Identity → Authorization → Routing → Session  = 闭合
Implementation                     = NOT STARTED（等 ADR-11 签发）
```

**本文件不宣称任何实现已完成。**

---

## 1. D-1 — ADR-11 签发内容冻结

### 1.1 一句话（签发时必须写明的核心句）

> **Package 2 introduces explicit conversation-to-connection authorization persistence
> while preserving the existing ACP domain boundaries.**

### 1.2 ADR-11 必须**显式取代**的既有条款

| # | 被取代物 | 现在写着什么 | 取代理由 |
|---|---|---|---|
| S1 | **Gate 2 §3.2 的 `Permit` = 全量（恒 ALLOW）** | 「不查任何 deny/grant 记录 → 判定恒 ALLOW」 | 被明确禁止的 placeholder；且使 `UNAUTHORIZED` 永不可达 |
| S2 | **Gate 2 §6 的 0-table assumption** | 「0 new tables / 0 new columns / NO migration ✅」 | 该结论的成立前提是「接受代价 #2（DENY 不可达）」；前提已被否 → 依其 §6.2 代价 #3 的升级触发条件，升级为 Persistence = YES |
| S3 | **Gate 2 §7 第 8 项「Routing 零改动」** | 「Routing … 全部不变 / 锁定测试零更新」 | 唯一性语义与 `agent_id` 派生**保持不变**；但候选集必须从 `active(channel)` 收窄为 `active(channel) ∩ granted(conversation)`（ADR-8 要求 Router 只消费已授权集合） |
| S4 | **ADR §八「任何 schema 变更（C2 已裁决：不需要）」** | 禁止 schema 变更 | 见 §1.3 —— 该句在 ADR 内部本就带出口，ADR-11 走该出口 |

### 1.3 一处**精确化**（避免误读为「ADR 被违反」）

ADR C2 原文：

> 因此 `identities.user_id` **不需要**放宽；本 ADR 不要求 schema 变更。
> **若实现步发现确有「已绑定渠道身份但无管理账号」的需求，须单独裁决**（不在本 ADR 授权范围内）。

所以 ADR §八 的正确读法是「**本 ADR 自身不要求 schema 变更；任何变更须单独裁决**」——
这正是 ADR-11 的法定入口。

**并且记录一个事实**：Package 1（`0006`）已经走过一次这个出口
（Gate 2.6 D1/D2 单独裁决 → `identities.user_id` 放宽为可空）。
ADR-11 是**第二次**走同一出口，不是例外之外的特例。

### 1.4 关于「ADR-6 中与 persistence 假设冲突的部分」—— **我建议不下取代，而是补齐**

⚠️ **此处我与你给的措辞有一处不同，先明确标出，你可以否决。**

逐条核对 ADR-6 后，我认为它**没有任何条款需要被取代**：

| ADR-6 条款 | 与本设计的关系 |
|---|---|
| 「授权默认 = deny（缺授权记录 = 不允许）」 | ✅ **正是本设计所实现的**（`granted = ∅ → DENY`，deny 由缺行表达） |
| 「允许必须显式成立」 | ✅ **正是 grant 表要提供的东西** |
| 「Discovery 可以发生…但**不产生**授权状态」 | ✅ D-4 的 known-conversations 严格遵循（纯读投影，不写授权） |
| ❌ 不得「首次收到消息即写入授权」 | ✅ 继续有效（回填是**一次性迁移**，不是运行时路径） |
| ❌ 不得把「Routing 选中」当授权结论 | ✅ 继续有效（授权在 Routing 之前独立成阶段） |

**ADR-6 反而是本设计的依据**，而非障碍。真正与 persistence 假设冲突的是
它在 Gate 2 §3.2 里的**实现承诺**（恒 ALLOW），那是 **S1**，不是 ADR-6 本体。

因此 ADR-11 对 ADR-6 的动作应写成：

```
ADR-6 的 default-deny 语义完整保留；
其"缺授权记录 = 不允许"所**预设但未言明**的持久化前提，
由 ADR-11 补齐（**补充**，而非取代）。
```

若你坚持用「取代 ADR-6 中与 persistence 假设冲突的部分」这一措辞，请指示——我会照写，
但会在 ADR-11 里注明「被取代的是 Gate 2 对 ADR-6 的实现承诺，而非 ADR-6 语义」，
以免未来的读者以为 ADR-6 被削弱。

### 1.5 窄范围 persistence exception 的正式批准

```
APPROVED（窄范围且唯一）：
    connection_conversations

它**不是** ACL / 权限表（无 effect / 优先级 / principal；作用域 = conversation；deny = 缺行）。
它是 V0 **唯一**的授权状态存储（此唯一性应由新增守卫机器化，见 §6.4 G-4）。
```

---

## 2. D-2 — `connections` 加性 UNIQUE（接受）

### 2.1 语义定性（写进 ADR-11，避免读者以为是业务变化）

```
UNIQUE(tenant_id, channel_id, id)  on  connections

不是新的业务唯一性；
是让 (tenant_id, channel_id, connection_id) 能成为合法 FK target 的
**结构性 supporting constraint**；

不改变 Connection 的业务语义
（Connection 的业务唯一性仍是 UNIQUE(tenant_id, channel_id, agent_id)）。
```

### 2.2 已实测的零守卫影响（复核结论保留）

| 断言点 | 判定方式 | 影响 |
|---|---|---|
| `test_models.py::test_b_connections_schema`（`:144-154`） | 只断言**列清单** + 目标约束存在 | 无 |
| `test_step10_6_1::test_a3`（`:190-200`） | `any(...)` | 无 |
| `test_cloud_0_1_1_b_tenant_architecture_gate::test_unique_constraints_lead_with_tenant_id`（`:179-192`） | `in uc["connections"]` 成员判定 | 无 |
| `test_cloud_0_1_1_b_tenant_isolation_dod`（`:69-72`） | `any(...)` | 无 |

---

## 3. D-3 — API = 独立资源（冻结）

### 3.1 冻结的是什么（**仅一条**）

```
Authorization Grant 是**独立资源**，不嵌入 Connection resource。
```

**理由**：ADR-2 红线要求 `Connection` 不得长成授权对象
（guard `_AUTH_FIELD_FRAGMENTS` 含 `grant`，会检查 Connection 的字段与方法名）；
把它做成 Connection 的子资源会在 API 面把二者重新耦合成一个对象。

### 3.2 **刻意不冻结**的是什么（明确留给 implementation design）

```
❌ 最终 URL 命名
❌ query parameters
❌ request / response shape（字段名、包装、分页形态）
❌ 状态码细节（除沿用既有先例这一纪律）
```

概念形态（**仅为示意，非冻结**）：

```
GET    <grant resource>
POST   <grant resource>
DELETE <grant resource>/{id}
```

### 3.3 已冻结的**纪律**（沿用既有先例，实现步不得背离）

| 情形 | 纪律 |
|---|---|
| 重复授权 | 409（`DuplicateIdentityError` 先例） |
| 引用不存在的 channel / connection | 404（`ChannelNotFoundError` 先例） |
| 空串 / 非法类型 | 422（`InvalidIdentityInputError` 先例） |
| 租户作用域 | 经既有 `tenant_scope_provider` 注入 tenant-scoped service |
| 无 Update | 行不可变（改 = 删 + 建） |

---

## 4. D-4 — "Where should it work?" 候选会话 Discovery（进 V0，**严格只读**）

### 4.1 裁决

```
进入 V0。理由不是 Authorization 本身，而是 **UX 完整性**：
default-deny 下若只能手工输入 external_conversation_id，
会把「让这个 Agent 在这个群里工作」变成「请找出一串外部系统 conversation ID」，
违背 Simple by Default（ADR-10）。
```

### 4.2 契约（纯读 projection）

```
GET <channels>/{channel_id}/known-conversations

    sessions
        ↓ DISTINCT
    known conversations（external_conversation_id 集合）
```

**硬约束**：

```
❌ known-conversations → 创建 Grant          （Discovery ≠ Authorization，ADR-6）
❌ 进入 Runtime 链路                          （它是管理面只读投影，不是授权输入）
❌ 把「出现在 known-conversations 里」当作任何授权状态
✅ 纯读；不修改任何行；不产生授权状态
```

### 4.3 与回填口径一致的一处选择

**不按 `sessions.status` 过滤**（与 §5 回填同一口径）：
「known」= 被观察到过。两处口径一致，避免出现「回填算它、列表不算它」的割裂。

### 4.4 自然边界（**不是 bug，登记为设计事实**）

```
若某 Conversation 从未产生过 Session
        ↓
known-conversations 自然发现不到它
        ↓
V0 无法通过 Discovery 授权它
        ↓
除非用户知道其 external_conversation_id 并显式创建 Grant
```

这是当前 discovery model 的自然边界；ADR-11 应原文登记，避免未来被当成缺陷反复讨论。

---

## 5. D-5 — Session status 假设（登记，但**不改** migration 逻辑）

### 5.1 登记到 ADR-11 的假设

```
V0 当前 Session **没有结束生命周期**；
历史 Session 全部作为 migration backfill candidate。
（实测依据：sessions 只有 create / find_active / bind_external_session 三条路径，
  status 恒为 'active'，无任何跃迁写入）
```

### 5.2 回填 SQL（最终形态）

```sql
SELECT DISTINCT
    tenant_id,
    channel_id,
    external_conversation_id,
    connection_id
FROM sessions
WHERE connection_id IS NOT NULL
  AND external_conversation_id IS NOT NULL
  AND trim(external_conversation_id) <> ''
```

**明确不加**：

```sql
✗ AND status = 'active'
```

理由：现有 schema 根本没有这个语义（status 无跃迁写入）——加了会引入一个
**虚假的过滤条件**，并在未来引入 `ended` 时静默改变回填结果。

> 该假设被登记的意义：未来 Session 生命周期落地时，回填语义必须**重新裁决**，
> 而不是被这次 SQL 的隐含假设悄悄决定。

---

## 6. D-6 — Fixture Impact Inventory（分类，**不改 55 个文件**）

> 术语澄清（回应上一轮疑问）：「fixture」= pytest fixture 与本仓库各测试文件自己的
> **seed 辅助函数**；「面枚举」= 范围分类。本节的产物是**分类**，不是修改。

### 6.1 为什么不能机械地给所有测试加 Grant

```
若为「全绿」给 55 个文件一律加 Grant：
    Authorization test suite 通过，
    但 fixture 把「授权是前提」这件事**全部隐藏掉**
        ↓
    测试不再能证明「没有授权就 DENY」——授权的前提被 fixture 吞了
```

### 6.2 分类方法（4 类）

| 类 | 定义 | 是否需要 Grant |
|---|---|---|
| **A** | 已显式创建 Session / Identity / Connection，并走完整 Runtime 成功路径 | **需要** |
| **B** | 通过自己的 helper（`_build_chain` / `_run_fake_bot` / `_seed_*`）间接获得 Session 并走成功路径 | **需要** |
| **C** | 只测 Agent / Router 等单一组件，**不进入**完整 Runtime | **不需要**（但需适配 Routing 新签名，见 §6.5） |
| **D** | 只测失败路径 | **不需要** |

### 6.3 首轮分类结果（基于实际 grep，**启发式**，需实现步逐文件确认）

信号集（测试目录）：

| 信号 | 含义 | 文件数 |
|---|---|---|
| `P` = `PersistingCoordinator` / `RuntimeCoordinator(` / `Worker(` / `Driver(` | 走完整管线 | **44** |
| `R` = `RoutingExecutor` | 触及路由（组件级 or 管线级） | **40** |
| `H` = 有 builder helper（`_build_chain` / `_build_runtime` / `_run_fake_bot` / `_run_group_bot` / `_seed_runtime` / `_build_app` / `_build_worker`） | 数据经 helper 播种 | **9** |
| `S` = `PROCESSING_SUCCEEDED` / `DELIVERY_DELIVERED` | 断言成功终态 | **11** |

集合关系（由上述实际文件清单做差集得出）：

```
P ∩ R  = 31 个文件   → 管线 + 路由 → 多数属 Class A/B
P \ R  = 13 个文件   → 走管线但不直接调 Routing（fanout / RLS / phase API / driver / attempt fence …）
                        → 逐个判定「只装配」还是「跑成功」
R \ P  =  9 个文件   → 组件级（未进入完整管线）→ Class C 的主力：
                        test_step9_4_routing_execution
                        test_step9_7_session_creation_execution
                        test_step9_8_1_execution_ordering
                        test_step10_3_1_routing_isolation
                        test_step10_3_1_multi_agent_isolation_guard
                        test_step10_6_1_agent_selection_semantics_audit
                        test_step6_7_management_closure
                        test_step9_A_runtime_slice_closure
                        test_external_identity_decoupling_package_1   ← Package 1 自己
```

**已确认的 Class A/B 锚点**（断言成功终态，default-deny 下必然转 DENY）：
`test_3b_2_2` · `test_3b_3_3` · `test_step9_8_3` · `test_step10_1_4*` · `test_step10_2_2` ·
`test_step10_3_1_multi_agent_e2e` · `test_step10_5_3` · `test_step12_6` · `test_step12_9_4_9` ·
`test_step12_9_4_12` · `test_step15_2_3` · `test_step15_5_2` · `test_cloud_0_1_4_1` · `test_cloud_0_1_5`

**已确认的 Class C 锚点**：`test_step9_4` / `test_step9_7` / `test_step9_8_1` / `test_step10_6_1` 等。

**诚实边界**：以上是 **pattern 命中 + 集合差集**，不是逐文件语义判定
（例如某文件可能只在 mutation guard 的字符串里提到 `RoutingExecutor`）。
**逐文件确认属于实现步的第一步**，本 Gate 只交付分类方法 + 首轮结果。

### 6.4 最终目标：让 Authorization 成为**显式测试前提**

```
authorized_conversation(db_engine, *, channel_id, external_conversation_id, connection_id)
        ↓
显式插入**恰好一条** Grant
```

**契约（冻结）**：

| 项 | 规定 |
|---|---|
| 显式 opt-in | 只有**显式调用**它，才产生授权；不调用 = DENY（这正是 default-deny 的证明） |
| 单条语义 | 一次调用 = 一条 Grant；需要多条就调多次（避免隐式批量） |
| 不做默认播种 | 不进 `conftest` 的 `db` / `db_engine` 夹具 |
| **禁止 blanket 授权** | ❌ 不得引入「授权所有会话」「自动授权」的测试夹具——那是把被禁的恒 ALLOW placeholder **搬到测试层复活**（应加守卫 G-5） |
| 反例可证 | 任何**不调用**它却断言成功的测试，在 default-deny 下必然失败 → 前提是可见的，不是隐藏的 |

### 6.5 Class C 的真实影响不是 Grant，而是**签名**

```
Class C 绕过 coordinator → 不会 DENY；
但 Routing 接受「已授权候选集」后，直接调 RoutingExecutor(...).execute(channel_id) 的调用点
        ↓
需要适配新参数（含 Package 1 的 test_external_identity_decoupling_package_1）
```

**因此 D-6 的完整范围是两类改造，不是一类**：
(A/B) 补 Grant 前提 · (C) 适配 Routing 签名。

---

## 7. Production Contract（完整）

### 7.1 语义链

```
Authorization Grant（概念）
        ↓ 持久化
connection_conversations（1 张表，V0 唯一授权状态存储）
        ↓ 语义
Conversation（channel_id + external_conversation_id）
        ↓
Connection
```

### 7.2 DDL（PostgreSQL；SQLite 路径由 `create_all` 承担）

```sql
-- ① supporting constraint（D-2）
ALTER TABLE connections
  ADD CONSTRAINT uq_connections_tenant_channel_id
  UNIQUE (tenant_id, channel_id, id);

-- ② 授权状态表（D-1 窄豁免）
CREATE TABLE connection_conversations (
    id                        SERIAL PRIMARY KEY,
    tenant_id                 INTEGER      NOT NULL REFERENCES tenants(id),
    channel_id                INTEGER      NOT NULL REFERENCES channels(id),
    external_conversation_id  VARCHAR(255) NOT NULL,
    connection_id             INTEGER      NOT NULL,
    created_at                TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_connection_conversations_tenant_channel_conv_conn
        UNIQUE (tenant_id, channel_id, external_conversation_id, connection_id),
    CONSTRAINT fk_connection_conversations_tenant_channel_connection
        FOREIGN KEY (tenant_id, channel_id, connection_id)
        REFERENCES connections (tenant_id, channel_id, id)
);

CREATE INDEX ix_connection_conversations_tenant_id
    ON connection_conversations (tenant_id);
CREATE INDEX ix_connection_conversations_connection_id
    ON connection_conversations (connection_id);
```

**零 CHECK 约束**（与既有 12 张表同纪律）；非空串由应用层校验（422）。

索引最小性：授权判定主查询 `(tenant_id, channel_id, external_conversation_id)`
由 UNIQUE 的**前导列**覆盖，不重复建索引。

### 7.3 RLS（第二层防线）

```sql
ALTER TABLE connection_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE connection_conversations FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON connection_conversations
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::integer)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::integer);
```

表达式必须用 **0005 的 `NULLIF(...)` 形态**（不是 0002 的原始形态）——
因为 0005 的 `CORE_TABLES` 循环**不会覆盖新表**，新表必须在 0007 里直接写出正确形态。

### 7.4 Migration 0007 构成（原子）

```
0007_connection_conversations_and_backfill.py   (revises 0006)
    upgrade():
      1. ALTER TABLE connections ADD CONSTRAINT uq_connections_tenant_channel_id UNIQUE (tenant_id, channel_id, id)
      2. CREATE TABLE connection_conversations（§7.2）
      3. CREATE INDEX ×2
      4. ENABLE + FORCE RLS + CREATE POLICY（§7.3）
      5. 回填（§5.2 SQL），ON CONFLICT DO NOTHING
    downgrade():
      DROP TABLE → DROP CONSTRAINT uq_connections_tenant_channel_id
      （注：downgrade 会丢失全部授权 → **有损**，须在 docstring 明写）
```

**原子性是硬要求**：表存在但无 Grant 的中间态 = 全量 DENY = 服务中断。

### 7.5 Runtime 管线（闭合）

```
InboundInteraction
      ↓
Identity Resolution     ← Package 1：Identity 命中即 RESOLVED（User 缺失不影响）
      ↓
★ Authorization         ← granted(channel, conversation)；∅ → DENY
      ↓
Routing                 ← 候选 = active(channel) ∩ granted；!= 1 → ROUTING_FAILED
      ↓
Session Resolution
      ↓
[Session Creation?]
      ↓
Agent Dispatch
      ↓
Delivery
```

真值表（Package 2-B §B6.3 冻结）：

| `granted` | `active ∩ granted` | Authorization | Routing | code |
|---|---|---|---|---|
| ∅ | — | **DENY** | 不执行 | `UNAUTHORIZED` |
| ≠ ∅ | ∅ | ALLOW | 候选 0 → fail | `ROUTING_FAILED` |
| ≠ ∅ | 1 | ALLOW | 选中 + `agent_id` 派生 | — |
| ≠ ∅ | ≥ 2 | ALLOW | 候选 >1 → fail | `ROUTING_FAILED` |

### 7.6 Failure Contract（拒绝路径）

```
processing_status = failed
delivery_status   = pending
failure_stage     = authorization      ← 新取值（既有 String(32) 列）
failure_code      = UNAUTHORIZED       ← 新取值（既有 String(64) 列）
retryable         = false
session_id        = NULL               ← authorization 不在 SESSION_LINKED_STAGES
response_payload  = NULL
+ activities 一条：type=stage_failure, stage=authorization, status=failed
```

必需生成侧改动（缺失即落成 `UNEXPECTED_FAILURE`，不诚实）：
`STAGE_AUTHORIZATION` 常量 · `STAGE_FAILURE_MAP` 注册 · `RUNTIME_EXECUTION_ORDER` +1。

### 7.7 管理面契约（冻结边界见 §3）

```
写：
    Create Grant   （显式 opt-in，default-deny 下唯一的开新会话途径）
    Delete Grant   （可撤销）
    List Grants    （回显「它在哪工作」）
读（只读 projection）：
    known-conversations（§4）

❌ 无 Update（行不可变）
❌ 无 bulk / import / export / priority / effect（ADR-9）
❌ 无 blanket / 自动授权（任何形式）
```

---

## 8. 闭合确认

| 闭合项 | 状态 |
|---|---|
| `Authorization Grant → connection_conversations → Conversation → Connection` 完整 contract | ✅ §7 |
| **Migration 0007 = schema + RLS + backfill** | ✅ §7.4（原子） |
| **Runtime = Identity Resolution → Authorization → Routing → Session** | ✅ §7.5 |
| Failure contract（UNAUTHORIZED 五元组 + Evidence） | ✅ §7.6 |
| Connection lifecycle（阻断式；status 不碰 Grant） | ✅ Package 2-B §B3 |
| Tenant consistency（复合 FK 结构性防止） | ✅ Package 2-B §B2 |
| 6 项 Required Decisions | ✅ §1–§6 |
| ADR-11 待签发 | ⏳ 唯一剩余的前置 |

---

## 9. NOT STARTED（实现步才做）

```
✗ ADR-11 文件本身（本 Gate 只冻结其内容）
✗ migration 0007
✗ ORM 表 + port + adapter + mapper
✗ Authorization 阶段（executor / typed error / coordinator 接线 / routing 候选集）
✗ STAGE_AUTHORIZATION / STAGE_FAILURE_MAP / RUNTIME_EXECUTION_ORDER
✗ 管理面 3 端点 + known-conversations 只读面 + schemas + main 注入
✗ authorized_conversation(...) 测试夹具
✗ Fixture Impact Inventory 的逐文件确认（Class A/B/C/D）
✗ Targeted Tests（7 项）
✗ Authorization regression / Real WeCom regression
✗ 守卫 G-1 ~ G-6（下表）
```

### 实现步应新增的守卫（供 ADR-11 授权范围）

| # | 守卫 | 目的 |
|---|---|---|
| G-1 | `connection_conversations` 是**唯一**授权状态表 | 防第二张授权表 / 策略表 creep |
| G-2 | 该表**无** `status` / `effect` / `priority` / `expires_at` 列 | 防 ACL 化 |
| G-3 | `deny` **不落库**（不存在 deny 行语义；deny = 缺行） | 防 effect 模型 |
| G-4 | 授权表**无** `identity_id` / `user_id` 列 | ADR-1 / ADR-3 红线 |
| G-5 | **禁止 blanket / 自动授权测试夹具** | 防 placeholder ALLOW 在测试层复活 |
| G-6 | known-conversations 只读（无写调用、不 import grant 写路径） | Discovery ≠ Authorization |

---

## 10. 本次未做（只读声明）

```
❌ 未改生产代码 / 未改测试 / 未建 migration / 未改 schema / 未改冻结契约
❌ 未创建 ADR-11 文件（仅冻结其内容）
❌ 未创建表 / 类 / port / API
❌ 未修改 55 个 runtime 测试文件中的任何一个（D-6 只做分类）
❌ 未在 ADR-6 上做任何取代（§1.4 已标出与该指令的差异，等你确认）
❌ 未执行回填
```

**本文件为设计产出，不构成实现批准。**
下一步：**ADR-11 签发** → Package 2 实现（`0007` → ORM/port/adapter → 阶段 → routing/coordinator → 管理面 → Fixture Inventory 确认 → Targeted Tests）。
