# Cloud-0.1.1-B —— Tenant / Multi-Tenant Persistence & Isolation Gate

> 阶段：Architecture Gate（设计 + 影响审计）。**本次只交付设计，不实现。**
> 方法：只读审计 + 机器化护栏（`tests/test_cloud_0_1_1_b_tenant_architecture_gate.py`）。
> 配套契约：ACP V0.1 RC → ACP V0.2 Artifact Contract → Cloud-0.1.1-A（PostgreSQL + Alembic + 8 Core Tables）→ **本 Gate（Tenant Isolation）**。

## 0. 结论先行（GO / BLOCKED）

**GO。** 10 个问题全部回答，且能证明：

- **Tenant 可以引入，而不改变 Runtime 语义** —— 红线（AgentRequest / AgentResponse / RuntimeExecutionResult / Session / InboundInteraction 均无 `tenant_id`）在静态测试中锁定。
- **隔离闭环可形成** —— Ingress Tenant Resolution（显式 `TenantScope`）→ Application / Repository 显式 `tenant_id` 过滤 → `SqlAlchemyUnitOfWork.__enter__` 内事务级 `SET LOCAL app.tenant_id` → PostgreSQL RLS（FORCE）→ 8 张 Core Table 行级隔离。
- **无隐式全局 tenant 上下文** —— 全工程扫描无 `current_tenant` / `global_tenant`；`TenantScope` 是显式对象，沿调用链传入，绝不进 `contextvars` 隐式全局。

最终结构（与用户给定的目标一致）：

```
                  Cloud
                    │
               TenantScope        ← 显式对象（非全局）
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
    Application          PostgreSQL RLS
          │                   │
          ▼                   ▼
     Repository ────────→ tenant_id
                              │
                              ▼
                         Core Tables

  Runtime 仍是：Session → Interaction → AgentRequest → Agent → AgentResponse
               完全不知道 Tenant。
```

---

## 1. Tenant Root Model

新建 `tenants` 表（`app/persistence/models/tenant.py`），作为租户根目录（root catalog）：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | `INTEGER` PK autoincrement | 与现有 8 表 PK 风格一致 |
| `name` | `VARCHAR(255)` NOT NULL | 租户名 / Workspace 名 |
| `status` | `VARCHAR(32)` NOT NULL DEFAULT `'active'` | 租户状态 |
| `config` | `JSON` NULL | 租户级配置（可选） |
| `created_at` | `TIMESTAMPTZ` NOT NULL DEFAULT now() | |
| `updated_at` | `TIMESTAMPTZ` NOT NULL DEFAULT now() onupdate | |

**根表特殊性原则**：`tenants` 本身**没有** `tenant_id`（它自己就是 tenant）。它不套用「按 `tenant_id` 过滤」的普通 RLS（见 §7）。

---

## 2. 8 张 Core Table 的 `tenant_id` 放置

8 张表全部新增一列（**加性、非破坏性**）：

```
users        └── tenant_id  INTEGER NOT NULL REFERENCES tenants(id)
identities   └── tenant_id  INTEGER NOT NULL REFERENCES tenants(id)
channels     └── tenant_id  INTEGER NOT NULL REFERENCES tenants(id)
agents       └── tenant_id  INTEGER NOT NULL REFERENCES tenants(id)
connections  └── tenant_id  INTEGER NOT NULL REFERENCES tenants(id)
sessions     └── tenant_id  INTEGER NOT NULL REFERENCES tenants(id)
interactions └── tenant_id  INTEGER NOT NULL REFERENCES tenants(id)
activities   └── tenant_id  INTEGER NOT NULL REFERENCES tenants(id)
```

**审计结论（Q1/Q2）**：现行 8 个 ORM Model 均无 `tenant_id`、无 `relationship()`、无 `tenants` 外键（证据：`app/persistence/models/*.py`，各表 `id` 自增主键完整）。`tenant_id` 是一个**正交于现有业务关系图的纯分区维度**——它只新增一个列 + 一个 FK，不改变任何既有表间关系（users↔identities、channels↔connections、sessions↔interactions……全部原样保留）。因此可在不改变现有业务语义的前提下建立 `tenant_id → tenants.id`。

`sessions` / `interactions` / `activities` 属于 Runtime evidence chain，但它们同样是普通数据行，加 `tenant_id` 列不影响其语义——证据链按 `(tenant_id, ...)` 自然归并到各自租户，无需改变其业务含义。

此项以静态测试锁定基线：`test_eight_core_tables_exist_with_integer_pk_and_no_tenant_id` / `test_existing_business_fks_intact_and_no_tenant_fk_yet`。

---

## 3. FK 策略

- `tenant_id INTEGER NOT NULL REFERENCES tenants(id)` 加到 8 张表（**新增 FK，不改动任何既有 FK**）。
- 现有业务 FK 完整保留（证据基线测试）：
  - `identities.user_id → users.id`、`identities.channel_id → channels.id`
  - `connections.channel_id → channels.id`、`connections.agent_id → agents.id`
  - `sessions.identity_id → identities.id`、`sessions.channel_id → channels.id`、`sessions.connection_id → connections.id`、`sessions.agent_id → agents.id`
  - `interactions.channel_id → channels.id`、`interactions.session_id → sessions.id`
  - `activities.interaction_id → interactions.id`、`activities.session_id → sessions.id`
- `tenant_id` FK 与以上无任何冲突：它是「行归属哪个租户」的元维度，与「行业务实体如何互联」正交。

---

## 4. Repository Isolation 策略（候选裁决）

**裁决：显式 `XxxRepository(session, tenant_id)` + UoW 作为唯一注入点 + RLS `SET LOCAL` 作为 defense-in-depth 兜底。**

候选对比（用户给定：`repository = XxxRepository(db, tenant_id)` 或 `repository.find(..., tenant_id=tenant_id)`，但禁止隐式全局）：

| 方案 | 评估 |
|---|---|
| (a) 仅靠 RLS `SET LOCAL`（repo 构造不变） | DB 层强制，但应用层不可见 tenant，若 RLS 配错则静默失效；不满足「应用层也正确」 |
| (b) `XxxRepository(session, tenant_id)` | **采用**：tenant 显式入参，无隐式全局；每查询 `.where(Model.tenant_id == self._tenant_id)`；UoW 已是 repo 唯一构造处，注入成本最低；Port 方法签名不变（tenant 来自实例，非每调用点） |
| (c) `repository.find(..., tenant_id=tenant_id)` | 调用面爆炸（每个 call site 都要传），且 Application Service 本身没有 tenant 在作用域，需全链路透传，易遗漏 |
| (d) 全局 `current_tenant` / `contextvars` | **禁止**：隐式全局，破坏可测试性 / 并发安全，直接违反红线 |

**与现有 Port / UoW 的兼容性**：当前 `Repository.__init__(self, session)`（Port 在 `app/repositories/`，Adapter 在 `app/persistence/repositories/`）。改为 `Repository.__init__(self, session, tenant_id)` 仅扩展构造签名；UoW（`SqlAlchemyUnitOfWork.__enter__`）已经持有 `session` 且是 8 个 repo 的唯一构造点，只需在此注入 `tenant_id`（从 `TenantScope` 取得）。Port 方法（`get_by_id` / `create` / `find_*`）签名不变。

**高风险面优先 scope**（审计发现，必须优先加 `tenant_id` 过滤）：
- 运行时读路径：`ConnectionRepository.find_active_by_channel`、`SessionRepository.find_active`、`InteractionRepository.find_pending` / `find_by_channel_and_external_id`。
- 管理面 `list_*` 方法（当前按 `id` 升序**返回全表**，无任何过滤，是隔离最高风险点）。
- 条件化 UPDATE：`claim_processing` / `record_outcome` / `reclaim_stale_processing`（CAS / Attempt Fence）——必须同步加 `tenant_id` 条件（或与 RLS 配合），否则存在跨租户误改。

此项以静态测试锁定：`test_repositories_build_sqlalchemy_queries_with_injectable_session`（确认每个 repo adapter 直接用 `sqlalchemy.select` + `self._session`，过滤可注入）+ `test_no_implicit_global_tenant_context`（确认无全局 tenant）。

---

## 5. TenantScope 生命周期

**Tenant 是 Control Plane Data Isolation Context，不是 Agent Runtime Context。**

```
HTTP Request / Webhook
   ↓
TenantResolver         ← 用 routing hint + 签名/凭据校验 解析 tenant
   ↓
TenantScope(tenant_id) ← 显式对象（dataclass），沿调用链传入，非全局变量
   ↓
Application Service(handler)   ← 接收 TenantScope（或仅 tenant_id）
   ↓
SqlAlchemyUnitOfWork(uow_factory, tenant_id)   ← 唯一注入点
   ├─ session.execute(SET LOCAL app.tenant_id = :tid)   ← RLS 钩子
   └─ repos = XxxRepository(session, tenant_id)         ← 显式过滤
   ↓
PostgreSQL RLS（行级强制）
   ↓
Core Tables（tenant_id 过滤）
```

Runtime 侧（AgentRequest → Agent → AgentResponse、Session、Interaction、SessionResolution）**永远看不到 TenantScope**。此项以静态测试锁定：`test_runtime_contracts_have_no_tenant_id`（6 个运行时契约均无 `tenant_id`）。

---

## 6. PostgreSQL RLS 策略

**裁决：必须使用 transaction-scoped `SET LOCAL app.tenant_id`（而非 session-scoped `SET`）。**

理由（连接池安全）：Cloud 使用连接池（SQLAlchemy 对 PostgreSQL 默认 `QueuePool`）。请求 A 占用 Connection 1 设 `tenant=1` 归还后，请求 B 复用 Connection 1——若为 session 级 `SET`，tenant 上下文会跨请求残留 → 跨租户污染。`SET LOCAL` 在 `COMMIT` / `ROLLBACK` 时**自动清除**，天然事务级，与连接池安全共存。

**钩子点（实现时）**：`SqlAlchemyUnitOfWork.__enter__` 拿到 `session` 后、构造 repo 前：
```python
session.execute(text("SET LOCAL app.tenant_id = :tid"), {"tid": tenant_id})
```
（`SET LOCAL` 必须在事务内执行；`session.execute` 即开启事务。）

**RLS 策略（8 张 Core Table 各一份）**：
```sql
ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <t> FORCE ROW LEVEL SECURITY;          -- 连表 owner 也强制，因 Cloud 用单一 DB role
CREATE POLICY tenant_isolation ON <t>
  USING     (tenant_id = current_setting('app.tenant_id')::integer)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::integer);
```
`app.tenant_id` 是 user-settable 自定义 GUC（`app.` 命名空间允许自定义参数），无需预注册。

**连接池证明（静态测试）**：`test_pg_engine_uses_connection_pool_justifying_set_local` 断言 `database._engine_kwargs("postgresql://...") == {}` → 对 PG 无自定义 `poolclass` → 采用 SQLAlchemy 默认 `QueuePool` → 必须用 `SET LOCAL`。当前 `database.py` 仅有 SQLite 的 `connect_args`，**无任何 PG 事件监听器**（`_enable_sqlite_foreign_keys` 仅对 sqlite 生效），确认是绿地引入。钩子点由 `test_uow_transaction_lifecycle_provides_set_local_hook` 锁定（`__enter__` 注入 session、有 `commit`/`__exit__` 事务边界）。

---

## 7. `tenants` 表的特殊处置

`tenants` 是 root catalog，**不带 `tenant_id`**，不能简单套 `tenant_id = current_setting(...)`（它没有所属租户）。但它的行（租户元数据）也须受保护。

**策略**：`tenants` 启用 RLS，但策略允许两种读取：
```sql
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenants_bootstrap ON tenants
  USING (
    current_setting('app.tenant_id', '') = ''          -- bootstrap：未设 tenant 上下文可读全部（用于解析）
    OR id::text = current_setting('app.tenant_id', '')  -- 已设上下文：仅可读自身
  );
```
- **Bootstrap 路径**（TenantResolver 在 `SET LOCAL` 之前解析租户）：`app.tenant_id` 尚未设置 → `''` 分支命中 → 可读全部 `tenants` 以按 subdomain/key 解析。
- **租户作用域路径**：仅可读自身 `tenants` 行。
- 备选：用 `SECURITY DEFINER` 函数 `resolve_tenant(key)` 绕过 RLS 做解析（更显式，避免 bootstrap 分支误解）。

此项以静态测试锁定：`test_tenants_table_absent_as_root_catalog`（当前 `Base.metadata` 无 `tenants`，确认是待引入的 root 表）。

---

## 8. 迁移策略（OSS SQLite → Cloud PostgreSQL）

**基线**：Alembic 当前唯一迁移 `0001_baseline_core_tables.py`（`down_revision = None`，仅 8 张业务表，注释明确「不引入任何 tenant / RLS / auth」）。Tenant 必须作为 **revision `0002`** 引入。

**Fresh Cloud DB（PostgreSQL）迁移顺序（0002）**：
1. `CREATE TABLE tenants (...)`。
2. 插入 bootstrap Tenant `#1`。
3. 8 张表各 `ALTER TABLE <t> ADD COLUMN tenant_id INTEGER NOT NULL REFERENCES tenants(id) DEFAULT 1;`（默认 1 用于回填；Fresh Cloud 无存量行）。
4. Drop 旧 unique 约束，创建含 `tenant_id` 前导列的新复合约束（见 §9）。
5. `CREATE INDEX ix_<t>_tenant_id`。
6. `ENABLE/FORCE ROW LEVEL SECURITY` + 各表 RLS policy（含 `tenants` 特殊策略）。
7. 回填后 `ALTER TABLE <t> ALTER COLUMN tenant_id DROP DEFAULT`（强制显式 tenant，防御性；OSS 单租户兼容可保留默认 1）。

**既有 SQLite 数据迁移**：当前**不存在** SQLite→PG 迁移脚本（审计确认）。需新建一次性数据导入工具：读取 SQLite 8 表 → 写入 PG（tenant_id 全部 = 1）。`tenant_id NOT NULL` 因此要求导入时必须赋值（统一归到 Tenant #1）。此工具属**实现期新增产物**，非本次 Gate 范围。

迁移顺序纪律：先建 `tenants` + 插 Tenant#1 → 再加列/FK → 再扩唯一约束 → 再建索引 → 最后 RLS（保证回填期间 RLS 未启用时不挡写入）。

---

## 9. Unique Constraints / Indexes 影响

**关键证明（Q9 核心）**：PostgreSQL RLS **不保护 UNIQUE 约束**。UNIQUE 由全局 B-tree 索引强制，RLS 策略不作用于唯一性检查。若两个租户各插入 `(channel_id=5, external_id='wx_a')`，即便有 RLS，第二条仍会撞唯一冲突。

⇒ **现有 3 个复合唯一约束必须扩展为 `(tenant_id, ...)` 前导**，否则跨租户唯一碰撞。这是改变约束的**明确且必要的理由**（用户要求「除非 Gate 证明现有约束无法满足 Cloud isolation」——此处已证）。

| 表 | 当前唯一约束 | 0002 目标 |
|---|---|---|
| `identities` | `(channel_id, external_id)` | `(tenant_id, channel_id, external_id)` |
| `connections` | `(channel_id, agent_id)` | `(tenant_id, channel_id, agent_id)` |
| `interactions` | `(channel_id, external_interaction_id)` | `(tenant_id, channel_id, external_interaction_id)` |
| `sessions` | 无（语义键在 `SessionRepository.find_active` 应用层强制：`identity_id × connection_id × external_conversation_id`） | 建议新增 `(tenant_id, identity_id, connection_id, external_conversation_id)` 作隔离约束（**不改变语义键**，仅加租户作用域） |

**索引**：
- 8 张表各加 `ix_<t>_tenant_id`（RLS policy 扫描 + 租户作用域查询必需）。
- 现有单列索引（`user_id` / `channel_id` / `identity_id` / `session_id` 等）可保持；热点租户作用域查询（如 `(tenant_id, channel_id)`、`(tenant_id, identity_id)`）在实现期按需扩展前导列。

此项以静态测试锁定基线：`test_existing_unique_constraints_baseline` 断言当前 3 约束列集且均无 `tenant_id`、sessions 无约束——记录基线并编码「实现时必须扩展为含 tenant_id」。

**重要**：`Session semantic key = (identity_id, connection_id, external_conversation_id)`（domain 冻结，见 `session_resolution.py`）**语义不变**；加 `tenant_id` 到 DB 唯一约束是隔离考量，不是语义键变更。

---

## 10. Ingress Tenant Resolution 策略

**冻结原则：Tenant routing hint ≠ authentication proof。**

| 入口 | routing hint | 解析 + 信任依据 |
|---|---|---|
| Browser | 已认证会话 | JWT/OAuth（未来）↔ User.tenant_id。**tenant 取自已认证身份，不取 URL**。 |
| WeCom webhook | URL `/api/channels/{channel_id}/wecom/webhook` 的 `channel_id` | 加载 Channel（DB）→ **校验 WeCom 签名**（`WeComAdapter.parse_message` 失败抛 `InboundSignatureError`→403）→ 仅在校验**成功**后信任该 Channel → `tenant_id = Channel.tenant_id`（DB）。 |
| ACP Agent 连接 | Connection | 入站连接 → Connection → Agent → `tenant_id`（DB），经凭据校验后信任。 |

**WeCom 反伪造**：URL 中的 `channel_id` 仅是**加载 Channel 的键**，tenant 取自**已校验 Channel 的 `tenant_id`**，从不取自 URL。攻击者伪造 `channel_id=999` 只会命中 404 或该 Channel 实际所属租户；要冒充另一租户必须持有该租户 WeCom 签名——签名校验守门，URL hint 不授信任。

**TenantResolver（实现期新增 Port）**：在 UoW 构造**之前**解析出 `TenantScope`；解析失败/无权限 → 403/404。

---

## 11. Runtime Boundary Proof（红线确认）

静态测试 `test_runtime_contracts_have_no_tenant_id` 断言以下 6 个契约字段面**均无 `tenant_id`**（证据：`app/domain/*.py`）：

- `AgentRequest`：`{agent_id, control_plane_session_id, content, external_session_id, metadata}`（docstring 明确禁止携带 user/identity/channel/connection/...，tenant 同理）。
- `AgentResponse`：`{content, external_session_id, metadata, artifacts}`。
- `RuntimeExecutionResult`：`{session_mode, session_id, agent_response}`（docstring「字段面恰好三个，禁止未经审计扩展」）。
- `SessionResolution`：`{mode, identity_id, connection_id, external_conversation_id, existing_session_id, external_session_id}`。
- `Session`：`{identity_id, channel_id, connection_id, agent_id, external_session_id, external_conversation_id, id, status, metadata, created_at, updated_at}`。
- `InboundInteraction`（Runtime 入口 DTO）：`{channel_id, external_interaction_id, external_conversation_id, external_identity_id, content, metadata}`——**不含 `tenant_id`**；tenant 在基础设施层解析，不进入 Agent 面对的消息。

⇒ Agent Runtime 完全租户无关；隔离仅落在持久层（models + repositories + UoW `SET LOCAL`），不污染运行时契约。

---

## 12. 精确的下一批实现文件集（仅规划，本次不实现）

**NEW**：
- `app/core/tenant.py` — `TenantScope`(dataclass) + `TenantResolver`(Protocol Port)
- `app/persistence/models/tenant.py` — `TenantModel`
- `app/persistence/repositories/tenant.py` — `SqlAlchemyTenantRepository`（root catalog 读；经 bootstrap 策略/SECURITY DEFINER 免 per-tenant RLS）
- `alembic/versions/0002_tenant_isolation.py` — 迁移（§8 顺序）
- `tools/sqlite_to_pg_import.py`（或 `migrations/`）— SQLite→PG 一次性导入（tenant_id=1）
- `tests/test_cloud_0_1_1_b_tenant_isolation_impl.py` — 实现验证

**MODIFY**：
- `app/persistence/models/{user,identity,channel,agent,connection,session,interaction,activity}.py` — +`tenant_id` 列
- `app/persistence/models/__init__.py` — 导出 `TenantModel`
- `app/repositories/{user,identity,channel,agent,connection,session,interaction_record,activity_record}.py` — Port 构造加 `tenant_id`
- `app/persistence/repositories/{...}.py` — 注入 `tenant_id`，查询 `.where(Model.tenant_id == self._tenant_id)`（含 CAS UPDATE）
- `app/persistence/unit_of_work.py` — 接收 `tenant_id`；`__enter__` 内 `SET LOCAL` 钩子；向 repo 注入 tenant
- `app/repositories/unit_of_work.py` — Port doc 更新（tenant 经 UoW 流转，无新全局）
- `app/application/channel_inbound.py` — 签名校验后从 Channel 解析 tenant → 传 UoW
- `app/api/runtime/channels.py` + 管理面 routers — 入口接 `TenantResolver`

---

## 13. 明确 NOT TOUCHED（冻结边界）

- `app/domain/agent_request.py` / `agent_response.py` / `runtime_execution_result.py` / `session.py` / `session_resolution.py` / `inbound_interaction.py`（无 `tenant_id`）
- `agent_runtime_sdk/**`（完全租户无关，静态测试锁定）
- `app/adapters/agents/acp.py` / `minimal.py` / `local.py`（Runtime 不变）
- `app/application/agent_dispatch.py` / `interaction_recording.py`（Runtime 语义不变；经 UoW 获得已租户作用域 repo）
- `Interaction` ORM **列集仅加性**（`tenant_id`，不删不改列）；`response_payload` 形状不变
- `AgentRequest` / `RuntimeExecutionResult` / Coordinator 执行语义 / OutboundResponse / Delivery / WeCom / Local / Tencent SSE / Frontend / Artifact Storage

---

## 14. GO / BLOCKED

**GO。** 10 问全答；Runtime 边界经静态测试锁定（红线可守）；隔离闭环（Repository 显式过滤 + RLS `SET LOCAL` + Ingress 解析）可证成；无禁止项被触碰。进入实现期时，按 §12 文件集 + §4/§6/§7/§8/§9/§10 规则落地，且不得破坏 §11 运行时边界与 §13 冻结清单。
