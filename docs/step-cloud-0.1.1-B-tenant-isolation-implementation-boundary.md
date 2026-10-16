# Cloud-0.1.1-B —— Tenant Isolation Implementation Boundary（实现边界）

> 阶段：**Implementation Boundary（实现边界 / 落地契约）**。
> 颗粒度：精确到「哪一行 schema、哪一个 repo 方法、哪一段 UoW、哪条 SQL」。
> 配套：
> - 上游设计锁：`docs/step-cloud-0.1.1-B-tenant-isolation-gate.md`（Architecture Gate，已 GO）。
> - 上游基础：`docs/step-cloud-0.1.1-A-postgres-alembic-foundation.md`（PG + Alembic 0001 + 8 表）。
> 本文件是 **Gate 之后的实现契约**——Gate 证明「能加 tenant 而不改 Runtime 语义」，
> 本文件把「怎么加、谁改、谁不碰、两层防线怎么落地」**精确写死**。
>
> 范围红线（本文件再次确认，强调）：**tenant 仅存在于持久层（ORM + DB + RLS）与入口解析层（TenantScope）；绝不进入 Domain、Runtime 契约、Adapter、SDK。** 因此 Runtime 业务语义 = ZERO 变化（见 §14 证明）。
>
> **状态：GO（owner 已确认）。** 以下裁决已冻结：`tenant_id` 只进 Persistence；两层隔离必须同时存在、不可互替；UNIQUE 演进为 `UNIQUE(tenant_id, ...)` + `sessions` 新复合唯一约束（是必要隔离语义变化，非范围扩张）；异步 Worker 是最大实际风险点（tenant fan-out = Scope/运行方式变化，**不是** Runtime execution logic 变化）；`functools.partial` 工厂桥可接受（关键是 tenant 显式进入 UoW 生命周期，非全局）；`get_by_id` 改 scoped select。实施红线与 DoD 见 §18。

---

## 0. 安全架构原则（写死）：Tenant Isolation 的两层防线

> 这是 **SteeringFlow Cloud 的硬性安全架构原则**，后续所有多租户相关实现必须遵守，不得削弱。

```
┌──────────────────────────────────────────────────────────────────┐
│  第一层 ：Application Repository Scope（显式隔离 / Explicit）      │
│                                                                  │
│   Repository 在「每一个」查询显式携带                              │
│       .where(Model.tenant_id == self._tenant_id)                  │
│   + 写入时显式绑定                                                │
│       row.tenant_id = self._tenant_id   （在 to_orm 之后、add 之前）│
│                                                                  │
│   职责：正常应用路径的「显式租户隔离」。                          │
│   特征：代码可见、可单测、可审计、可随业务查询演进。              │
└───────────────────────────────┬──────────────────────────────────┘
                                 │  不替代、不重叠、必须同时成立
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│  第二层 ：PostgreSQL RLS（强制兜底 / Forced）                     │
│                                                                  │
│   ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;                     │
│   ALTER TABLE <t> FORCE  ROW LEVEL SECURITY;                     │
│   CREATE POLICY tenant_isolation ON <t>                           │
│     USING     (tenant_id = current_setting('app.tenant_id')::int) │
│     WITH CHECK (tenant_id = current_setting('app.tenant_id')::int);│
│                                                                  │
│   职责：防止「代码 bug / 漏加过滤 / 新路径遗漏 / 重构回归」        │
│         导致跨租户数据泄露的「最后兜底」。                        │
│   特征：数据库级强制，连表 owner 也强制（FORCE）。                │
└──────────────────────────────────────────────────────────────────┘
```

**铁律（本原则不可违反的判定）：**

1. **RLS 不是 Repository filtering 的替代品。**
   - Repository Scope = 正常应用路径的**显式隔离**（工程师写得出、看得到、测得了）。
   - RLS = 防止代码缺陷导致越界的**强制兜底**（DB 最后一道闸）。
   - 二者**职责正交**，必须**同时成立**。
2. **去掉任一层都违反本原则：**
   - 只有 RLS、Repo 不显式过滤 → 应用层对隔离「不可见、不可测、不可审计」，且 RLS 配错会静默失效；
   - 只有 Repo 过滤、无 RLS → 任一新查询 / 重构漏加 `.where(tenant_id==)` 即形成跨租户泄露面。
3. **`tenants` 是 root catalog，不适用普通 per-tenant RLS**（见 §9 特殊策略），但仍是被保护对象。

本原则由 §13 的「必须修改 / 绝对不能碰」清单与 §16 的验证门共同守护。

---

## 1. `tenants` ORM / Model（NEW）

新文件：`app/persistence/models/tenant.py`

```python
from datetime import datetime
from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.persistence.database import Base


class TenantModel(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
```

约束要点（与现有 8 表风格一致，便于迁移 0002 对齐）：
- PK `INTEGER` 自增；`created_at/updated_at` 用 `DateTime(timezone=True)` + `func.now()`（与 `users/channels/...` 完全同形）。
- `tenants` **自身不带 `tenant_id`**（它自己就是 tenant，见 §9 特殊处置）。
- 在 `app/persistence/models/__init__.py` 导出 `TenantModel`（与现有 8 个导出同一写法）。

> Bootstrap 路径涉及的 `TenantRepository` 只读且不走普通 per-tenant RLS（见 §9 / §10）。

---

## 2. 8 张 Core Table 的 `tenant_id` 放置（加性、非破坏性）

对 `app/persistence/models/{user,identity,channel,agent,connection,session,interaction,activity}.py`
各加一列（**仅新增列 + 一个 FK，不改任何既有列 / 既有 FK**）：

```python
from sqlalchemy import ForeignKey
# 在各 Model 的既有 mapped_column 之后追加：
tenant_id: Mapped[int] = mapped_column(
    ForeignKey("tenants.id"), nullable=False, index=True)
```

- 放在 `id` 之后、业务列之前或之后均可，**不影响既有列顺序语义**（`create_all` / 0002 `ALTER TABLE ... ADD COLUMN` 均为尾部追加）。
- `index=True` → 生成 `ix_<t>_tenant_id`（与 §4 索引一致）。
- `nullable=False`：强制行必属某租户；回填 / 默认策略见 §5 / §11。
- 既有业务 FK（`identities.user_id→users.id`、`connections.channel_id→channels.id` 等）**原样保留不动**，`tenant_id` FK 与它们正交，无任何冲突。

`tenant_id` **只存在于 ORM 与 DB**；Domain 实体（`User`/`Channel`/`Agent`/`Identity`/`Connection`/`Session`/`Interaction`/`Activity`）**不加 `tenant_id` 字段**（见 §14 证明与 §13 冻结清单）。写入时由 Repo 在 `to_orm` 之后显式 `row.tenant_id = self._tenant_id` 绑定（§8）。

### 2.1 向后兼容裁决（实现关键，冻结）

需要同时满足：**(a)** Cloud 多租户 `tenant_id NOT NULL`；**(b)** OSS / 本地单租户 SQLite 行为 **逐字节不变**（现有 100+ 测试不改即绿，DoD 第 9 项「existing SQLite behavior」）；**(c)** DB 级 UNIQUE 在单租户下仍生效（否则 `identity/interaction` 幂等测试会被 `tenant_id=NULL` 打破——SQL 唯一约束把 NULL 视为互异）。

裁决（三层配合）：

1. **ORM 列声明**：`tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, default=1, server_default="1", index=True)`
   - `default=1`（客户端）+ `server_default="1"`（服务端）→ 既有 ORM 写入不显式给 tenant 时落 `1`，UNIQUE 键非空 → 幂等语义保留。
2. **Bootstrap Tenant 种子**：`tenants` 表创建时自动注入 `#1`——在 `TenantModel` 注册 `after_create` 事件（对 `create_all` 的测试 / 本地路径生效）；Cloud 由 migration 0002 显式 `INSERT ... (id=1)`（`op.create_table` 不触发 ORM 事件，故迁移内显式插）。
   - 结果：任何 `create_all` 出来的库（含测试内存库）FK 目标 `tenants#1` 必然存在 → 既有测试零改动通过。
3. **Repo / UoW 入参 `tenant_id: int | None`**：
   - `tenant_id is None` → 单租户 / legacy 路径：**不追加** tenant 过滤、**不执行** `SET LOCAL`（行为 == 隔离前；本地 SQLite 无 RLS）。
   - `tenant_id` 非空（Cloud 唯一路径）→ 第一层显式过滤 + 第二层 `SET LOCAL` 全生效。
   - 生产装配（`main.py` / API 入口）**永远传非空 tenant_id**（见 §7 / §12）；`None` 仅出现在本地/测试单租户上下文。

> 该裁决把「Cloud 强制」与「OSS/本地兼容」分离：Cloud schema 由 migration 0002 保证 `NOT NULL` + RLS；本地由 `default=1` + 种子 Tenant#1 保证行为恒等。`tenant_id=None` 分支在 PG 上因 `current_setting('app.tenant_id')` 未设而**fail-closed**（RLS 拒绝），不存在「漏过滤即越权」。

---

## 3. 必要的 UNIQUE Constraint 调整（写死）

> 关键事实（Gate Q9 已证）：**PostgreSQL RLS 不保护 UNIQUE 约束**。UNIQUE 由全局 B-tree 索引强制，RLS policy 不作用于唯一性检查。若两租户各插入 `(channel_id=5, external_id='wx_a')`，即便有 RLS，第二条仍撞唯一冲突 → 跨租户唯一碰撞。故现有复合唯一约束**必须扩展为 `(tenant_id, ...)` 前导**（这是唯一且必要的约束变更理由）。

| 表 | 当前 `UniqueConstraint`（基线，已静态锁定） | 0002 目标（前导 `tenant_id`） |
|---|---|---|
| `identities` | `(channel_id, external_id)` 名 `uq_identities_channel_external_id` | `(tenant_id, channel_id, external_id)` 名 `uq_identities_tenant_channel_external_id` |
| `connections` | `(channel_id, agent_id)` 名 `uq_connections_channel_agent_id` | `(tenant_id, channel_id, agent_id)` 名 `uq_connections_tenant_channel_agent_id` |
| `interactions` | `(channel_id, external_interaction_id)` 名 `uq_interactions_channel_external_id` | `(tenant_id, channel_id, external_interaction_id)` 名 `uq_interactions_tenant_channel_external_id` |
| `sessions` | 无（语义键在 `SessionRepository.find_active` 应用层强制） | **新增** `(tenant_id, identity_id, connection_id, external_conversation_id)` 名 `uq_sessions_tenant_identity_conn_conv` |

实现变更点（每个 model 的 `__table_args__`）：

```python
# identities.py（其余同理）
__table_args__ = (
    UniqueConstraint("tenant_id", "channel_id", "external_id",
                     name="uq_identities_tenant_channel_external_id"),
)
# sessions.py（新增，注意 connection_id / channel_id 允许 NULL；
#             PG 对 NULL 视为互异，不会误判 NULL 行碰撞，语义恰合）
__table_args__ = (
    UniqueConstraint("tenant_id", "identity_id", "connection_id",
                     "external_conversation_id",
                     name="uq_sessions_tenant_identity_conn_conv"),
)
```

**重要**：`Session` 语义键 `(identity_id, connection_id, external_conversation_id)`（domain 冻结，见 `session_resolution.py`）**语义不变**；加 `tenant_id` 仅扩展隔离作用域，不改变去重语义。其他表唯一约束除前导 `tenant_id` 外**列集不变**。

---

## 4. 必要的 Indexes（写死）

- 8 张表各加 `ix_<t>_tenant_id`（由 §2 的 `index=True` 自动生成；RLS policy 扫描 + 租户作用域查询必需）。
- 现有单列索引（`ix_identities_user_id`、`ix_identities_channel_id`、`ix_connections_agent_id/channel_id`、`ix_sessions_*`、`ix_interactions_session_id`、`ix_activities_*` 等）**全部保留不动**。
- 热点租户作用域复合查询（如 `(tenant_id, channel_id)`、`(tenant_id, identity_id)`）在实现期**按需**扩展前导列，本边界不强制（保持「无真实消费者前不过度建设」纪律；但 `tenant_id` 单列索引是 RLS 必须的，已含）。

---

## 5. Alembic Migration：`0002_tenant_isolation.py`（NEW）

基线：`0001_baseline_core_tables.py`（`down_revision=None`，仅 8 业务表，无 tenant）。
新迁移 `revision="0002"`，`down_revision="0001"`。

**顺序纪律（先建根 / 再回填 / 再扩约束 / 再建索引 / 最后 RLS，保证回填期 RLS 未启用不挡写入）：**

1. `CREATE TABLE tenants (...)`（结构同 §1）。
2. 插入 bootstrap Tenant `#1`（`INSERT INTO tenants (id, name, status) VALUES (1, 'default', 'active')`）。
   - PG `SERIAL`/`IDENTITY` 自增；显式指定 `id=1`，使既有 OSS 导入数据（§11）的 `tenant_id=1` 指向此行。
3. 8 张表各 `ALTER TABLE <t> ADD COLUMN tenant_id INTEGER NOT NULL REFERENCES tenants(id) DEFAULT 1;`
   - `DEFAULT 1` 用于回填期 / Fresh Cloud 无存量行场景；回填完成后见第 6 步。
4. Drop 旧唯一约束，Create 含 `tenant_id` 前导的新复合约束（§3）。
5. `CREATE INDEX ix_<t>_tenant_id ON <t> (tenant_id);`（若未用 ORM `index=True` 生成则显式建）。
6. `ENABLE/FORCE ROW LEVEL SECURITY` + 各表 RLS policy（§9，含 `tenants` 特殊策略）。
7. 回填完成后 `ALTER TABLE <t> ALTER COLUMN tenant_id DROP DEFAULT;`
   - 目的：强制显式 tenant（防御性）。OSS 单租户本地兼容见 §11（本地保留默认 1，不 drop）。

`downgrade()`：按反向顺序 `DROP POLICY` → `DISABLE RLS` → drop 新唯一约束 / 重建旧 → drop `tenant_id` 列 → drop `tenants` 表（注意 `tenant_id` 为非空 FK，drop 列前无需再处理；但若有数据需先清空）。

> RLS 在 0002 末尾才启用，保证步骤 3–5 的 `ALTER` / 回填写入不被 RLS 拦截（bootstrap 阶段 `app.tenant_id` 尚未设置，即便提前启用也有 bootstrap 分支，但顺序上仍放最后最稳）。

---

## 6. `TenantScope`（NEW：`app/core/tenant.py`）

Tenant 是 **Control Plane Data Isolation Context**，**不是** Agent Runtime Context。

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class TenantScope:
    """显式租户隔离上下文。沿调用链显式传入，绝不进 contextvars 隐式全局。"""
    tenant_id: int
```

**Port（解析契约，仅在边界定义，§12 不实现）：**

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class TenantResolver(Protocol):
    """入口处把 routing hint + 凭据 解析为 TenantScope。

    实现期才落地；本边界只锁接口：
      - resolve(...) -> TenantScope
      - 解析失败 / 无权限 -> 抛 IngressTenantError（映射 403/404）
    """
    def resolve(self, request_context) -> TenantScope: ...
```

**Provisioner / Bootstrap Port（root 写入，仅在边界定义）：**

```python
@runtime_checkable
class TenantProvisioner(Protocol):
    """唯一被允许写入 tenants 表的入口（创建新租户 + 初始 admin）。
    必须经 bootstrap RLS 分支（app.tenant_id 未设）执行。"""
    def provision(self, name: str, *, config=None) -> TenantScope: ...
```

全工程扫描红线（Gate 已静态锁定，实现期不得破坏）：无 `current_tenant` / `global_tenant` 隐式全局。

---

## 7. UoW `SET LOCAL`（精确机制）

文件：`app/persistence/unit_of_work.py`（唯一 repo 注入点，已静态锁定）。

**构造签名扩展（仅加 `tenant_id`，不动 repo 暴露集合 / 事务边界）：**

```python
class SqlAlchemyUnitOfWork:
    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        tenant_id: int | None = None,          # NEW：租户作用域
    ) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id
        self.session = None
        # ... 其余属性初始化不变

    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        session = self._session_factory()
        self.session = session
        # —— NEW：RLS 钩子（事务内执行，SET LOCAL 在 commit/rollback 自动清除）——
        if self._tenant_id is not None:
            session.execute(
                text("SET LOCAL app.tenant_id = :tid"),
                {"tid": self._tenant_id},
            )
        # —— NEW：向每个 repo 注入 tenant_id（第一层显式隔离的承载点）——
        self.users       = SqlAlchemyUserRepository(session, self._tenant_id)
        self.channels    = SqlAlchemyChannelRepository(session, self._tenant_id)
        self.agents      = SqlAlchemyAgentRepository(session, self._tenant_id)
        self.identities  = SqlAlchemyIdentityRepository(session, self._tenant_id)
        self.connections = SqlAlchemyConnectionRepository(session, self._tenant_id)
        self.sessions    = SqlAlchemySessionRepository(session, self._tenant_id)
        self.interactions= SqlAlchemyInteractionRecordRepository(session, self._tenant_id)
        self.activities  = SqlAlchemyActivityRecordRepository(session, self._tenant_id)
        return self
    # commit / rollback / close / __exit__ 不变
```

**为何必须 `SET LOCAL` 而非 `SET`：** Cloud 用连接池（SQLAlchemy 对 PG 默认 `QueuePool`；`database._engine_kwargs("postgresql://...") == {}` 已静态锁定无自定义 poolclass）。请求 A 占用连接设 `tenant=1` 归还后，请求 B 复用同一连接——若为 session 级 `SET`，tenant 上下文跨请求残留 → 跨租户污染。`SET LOCAL` 在事务 `COMMIT/ROLLBACK` 时自动清除，与连接池安全共存。当前 `database.py` 仅对 sqlite 设 `connect_args`，无任何 PG 事件监听器（绿地引入）。

**工厂绑定桥（连接 TenantScope ↔ Repository 的关键）：**

`TenantScope` 解析后，API 层用 `functools.partial` 把 tenant 绑进工厂，再交给 Application Service：

```python
# API 层（§12）：解析到 scope 后
uow_factory = functools.partial(SqlAlchemyUnitOfWork, tenant_id=scope.tenant_id)
service = SomeApplicationService(uow_factory=uow_factory)   # 服务签名不变
```

Application Service 内部仍 `with self._uow_factory() as uow:` —— 拿到的已是 tenant-scoped UoW。**Service 代码零改动**（见 §13 / §14）。

---

## 8. Repository Tenant Filtering（逐方法写死）

每个 adapter（`app/persistence/repositories/{user,identity,channel,agent,connection,session,interaction_record,activity_record}.py`）：

1. **构造加 `tenant_id`**：`def __init__(self, session: Session, tenant_id: int | None) -> None: self._session = session; self._tenant_id = tenant_id`。
2. **读查询全加 `.where(Model.tenant_id == self._tenant_id)`**（第一层显式隔离）。
3. **写显式绑定**：`create` 中 `row = to_orm(entity); row.tenant_id = self._tenant_id; self._session.add(row)`。
   - 关键：Domain / Mapper **不感知 tenant**（`User.to_orm` 仍只产 `name/status`），tenant 由 Repo 在持久层绑定 → Domain 零污染（§14 证明核心）。
4. **`get_by_id` 改 scoped select**（原 `session.get(Model, id)` 无法带 tenant 过滤）：
   ```python
   row = self._session.execute(
       select(Model).where(Model.id == id, Model.tenant_id == self._tenant_id)
   ).scalar_one_or_none()
   ```
5. **异步 / 条件化路径必须 tenant-scoped（最高风险面）：**
   - `InteractionRecordRepository.find_pending(limit)`：`select(...).where(tenant_id==tid, processing_status==PENDING, ...)`。
   - `reclaim_stale_processing(threshold, limit)`：`stale_ids` 子查询与 `update().where(id.in_(...))` 均须带 `tenant_id==tid`（否则会回收 / 改写**他租户**的 PROCESSING 孤儿）。
   - `claim_processing(interaction_id)` / `record_outcome(interaction_id, ...)`：USING 已按 `id` 定位；为防跨租户误 claim，WHERE 追加 `tenant_id==tid`（与 `find_pending` 的租户范围一致）。
   - `find_active_by_channel`、`find_by_channel_and_external_id`、`find_by_channel_and_agent`、`list_users/list_channels/list_agents/list_identities/list_connections`：全部加 `tenant_id==tid`（管理面 `list_*` 当前返回**全表**，是隔离最高风险点，必须显式租户作用域）。

> 注意：`find_pending` / `reclaim_stale_processing` 一旦 tenant-scoped，单条 Worker 只能看到**一个租户**的待处理行。因此 **Worker 必须在 tenant-scoped UoW 下运行**（Cloud 下按租户 fan-out：每租户一个处理循环 / 或逐行解析所属租户后再以该租户作用域处理）。Worker / Driver 的**代码逻辑不变**，仅其 `uow_factory` 被绑到具体 tenant（本地单租户 = tenant #1，行为不变）。fan-out 具体实现细节留给实现期，但**不变量**写死：每次 `find_pending`/`claim`/`reclaim` 都在 `SET LOCAL app.tenant_id = 该行 tenant` 的事务内执行。

---

## 9. PostgreSQL RLS Policies（精确 SQL）

8 张业务表各一份（在 0002 末尾启用）：

```sql
ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <t> FORCE  ROW LEVEL SECURITY;   -- 连表 owner 也强制（Cloud 单一 DB role）
CREATE POLICY tenant_isolation ON <t>
  USING     (tenant_id = current_setting('app.tenant_id')::integer)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::integer);
```

`<t>` ∈ {users, identities, channels, agents, connections, sessions, interactions, activities}。

`app.tenant_id` 是 user-settable 自定义 GUC（`app.` 命名空间允许自定义参数，无需预注册）。

**`tenants` 特殊策略（root catalog，不带 tenant_id）：**

```sql
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenants_bootstrap ON tenants
  USING (
    current_setting('app.tenant_id', '') = ''             -- bootstrap：未设上下文可读全部（解析用）
    OR id::text = current_setting('app.tenant_id', '')    -- 已设上下文：仅可读自身
  );
```

- Bootstrap 路径（TenantResolver 在 `SET LOCAL` 之前解析租户）：`app.tenant_id` 未设 → `''` 分支命中 → 可读全部 `tenants` 按 key 解析。
- 租户作用域路径：仅可读自身 `tenants` 行。
- 备选（更显式，避免 bootstrap 分支误解）：用 `SECURITY DEFINER` 函数 `resolve_tenant(key)` 绕过 RLS 做解析。实现期二选一，接口行为一致。

---

## 10. Bootstrap / Provisioning Path（边界定义）

- **唯一写 `tenants` 的入口**：`TenantProvisioner`（§6 Port），经 bootstrap RLS 分支（未设 `app.tenant_id`）执行，创建新租户 + 初始 admin User/Identity。
- **Fresh Cloud 启动顺序**：migration 0002 第 2 步已插 Tenant `#1`（默认 / 系统租户）。首个真实租户的创建走 `TenantProvisioner`，**不在**普通业务事务内（普通事务已 `SET LOCAL`，受 per-tenant RLS 约束，无权写 `tenants`）。
- **TenantResolver 解析顺序**（在 UoW 构造**之前**）：
  1. 以 bootstrap 模式（无 `app.tenant_id`）读 `tenants` 按 routing hint 定位候选租户；
  2. 加载关联对象（如 Channel）并**校验凭据 / 签名**（WeCom 签名、未来 Browser JWT/OAuth）；
  3. 校验成功后从已校验对象取 `tenant_id`（绝不取 URL hint）；
  4. 解析失败 / 无权限 → `IngressTenantError` → 403/404。
- **第一阶段交付范围**：仅定义 `TenantResolver` / `TenantProvisioner` Port 与 bootstrap RLS 分支；**不实现**具体凭据校验逻辑（见 §12）。

---

## 11. 既有 SQLite 数据的 `tenant_id=1` 策略

**现状审计**：当前**不存在** SQLite→PG 迁移脚本（Gate 已确认）。策略如下：

- **OSS → Cloud 一次性导入工具（NEW，`tools/sqlite_to_pg_import.py`）**：读 SQLite 8 表 → 写 PG（每行 `tenant_id = 1`）。因 `tenant_id NOT NULL`（§2），导入时必须赋值，统一归到 Tenant `#1`。属实现期新增产物。
- **本地 SQLite dev（单租户）兼容**：保留 `tenant_id` 行为为「单租户 tenant #1」：
  - UoW 在无显式 `TenantScope` 的本地模式下，`tenant_id` 取常量 `TENANT_LOCAL = 1`；
  - `init_database()`（仅 SQLite 生效）在 `create_all` 后确保 `tenants` 表被种子为 Tenant `#1`（或在本地用最简方式保证 FK 可满足）；
  - 迁移 0002 第 7 步的 `DROP DEFAULT` **仅对 Cloud PG 执行**；本地路径保留 `DEFAULT 1`，使 `create_all` 路径与既有本地行为零差异。

→ 本地单租户下，所有行 `tenant_id=1`，所有查询的 `tenant_id==1` 过滤与 RLS 均为**恒等过滤**（全集），观测行为与隔离前**逐字节一致**（§14 证明的关键支点）。

---

## 12. Tenant Ingress Resolution（仅定义边界，不实现 Browser Auth / Webhook Routing）

**冻结原则：Tenant routing hint ≠ authentication proof。**

| 入口 | routing hint | 解析 + 信任依据（边界定义） |
|---|---|---|
| Browser | 已认证会话 | 未来 JWT/OAuth ↔ `User.tenant_id`；**tenant 取自已认证身份，不取 URL**。 |
| WeCom webhook | URL `/api/channels/{channel_id}/wecom/webhook` 的 `channel_id` | 加载 Channel（bootstrap 读）→ **校验 WeCom 签名**（Adapter 失败抛 `InboundSignatureError`→403）→ 仅校验成功后信任 → `tenant_id = Channel.tenant_id`（DB）。 |
| ACP Agent 连接 | Connection | 入站连接 → Connection → Agent → `tenant_id`（DB），凭据校验后信任。 |

**反伪造**：URL 中 `channel_id` 仅是**加载 Channel 的键**，tenant 取自**已校验 Channel 的 `tenant_id`**，从不取自 URL。伪造 `channel_id=999` 只命中 404 或该 Channel 实际所属租户；冒充他租户须持有该租户签名——签名校验守门，URL hint 不授信任。

**本边界明确 NOT IMPLEMENTED（留给后续 Step）：**
- Browser Auth（JWT/OAuth 颁发 / 校验）不在此实现；
- Webhook Routing 的签名校验代码不在此新增（`app/adapters/channels/wecom.py` 的验签保持原状，仅作为其**下游**被 TenantResolver 调用）。
- 本阶段只定义：`TenantResolver` / `TenantProvisioner` Port（§6）、bootstrap RLS 分支（§9）、API 层「先解析 TenantScope → 再绑 UoW 工厂」的装配点（§7 / §13）。

---

## 13. 必须修改 vs 绝对不能碰（写死清单）

### 13.1 MUST MODIFY（必须修改）

| 层 | 文件 | 修改内容 |
|---|---|---|
| Model | `app/persistence/models/{user,identity,channel,agent,connection,session,interaction,activity}.py` | 各加 `tenant_id` 列（FK→tenants.id, NOT NULL, index）；`identities/connections/interactions` 改 `UniqueConstraint` 前导 `tenant_id`；`sessions` 新增复合唯一约束。 |
| Model | `app/persistence/models/tenant.py`（NEW） | `TenantModel`（§1）。 |
| Model | `app/persistence/models/__init__.py` | 导出 `TenantModel`。 |
| Repo adapter | `app/persistence/repositories/{user,identity,channel,agent,connection,session,interaction_record,activity_record}.py` | 构造加 `tenant_id`；每个查询加 `.where(tenant_id==)`（含 `get_by_id` 改 scoped select、`find_pending`/`reclaim_stale_processing`/CAS 全加租户条件）；`create` 显式 `row.tenant_id = self._tenant_id`。 |
| Repo adapter | `app/persistence/repositories/tenant.py`（NEW） | `SqlAlchemyTenantRepository`：经 bootstrap RLS 分支读（§9 / §10）。 |
| UoW | `app/persistence/unit_of_work.py` | 接收 `tenant_id`；`__enter__` 内 `SET LOCAL` + 向 repo 注入 tenant（§7）。 |
| Core | `app/core/tenant.py`（NEW） | `TenantScope` dataclass + `TenantResolver` / `TenantProvisioner` Port（§6）。 |
| API（入口） | `app/api/runtime/channels.py` + `app/api/management/{users,identities,channels,connections,agents,interactions}.py` | 入口先解析 `TenantScope` → `functools.partial(SqlAlchemyUnitOfWork, tenant_id=scope.tenant_id)` → 构造 service（§7 桥）。 |
| 组合根 | `app/main.py` | 服务由「启动时单例 + 无 tenant 工厂」改为「按请求以 tenant-bound 工厂构造」（§7 / §12）。 |
| Migration | `alembic/versions/0002_tenant_isolation.py`（NEW） | 按 §5 顺序。 |
| 迁移工具 | `tools/sqlite_to_pg_import.py`（NEW） | SQLite→PG 导入，`tenant_id=1`（§11）。 |
| 测试 | `tests/test_cloud_0_1_1_b_tenant_isolation_impl.py`（NEW） | 实现验证门（§16）。 |

### 13.2 MUST NOT TOUCH（绝对不能碰 —— 冻结边界）

| 类别 | 文件 / 范围 | 理由 |
|---|---|---|
| **Runtime 契约（红线）** | `app/domain/agent_request.py`、`agent_response.py`、`runtime_execution_result.py`、`session.py`、`session_resolution.py`、`inbound_interaction.py` | 字段面**零 `tenant_id`**（静态测试 `test_runtime_contracts_have_no_tenant_id` 锁定）。 |
| **Domain 实体** | `app/domain/{user,channel,agent,identity,connection,session,interaction_record,activity_record}.py` | 保持租户无关；`tenant_id` 仅落持久层，Domain 不加字段（§14 证明核心）。 |
| **Mapper** | `app/persistence/mappers/*.py` | `to_orm` 不产 tenant（Repo 在之后绑定），保持 Domain↔ORM 翻译纯业务。 |
| **Port 方法签名** | `app/repositories/{user,identity,channel,agent,connection,session,interaction_record,activity_record}.py` | tenant 经 UoW 实例流转，非每调用点参数；方法签名不变（§4 Gate 裁决）。 |
| **SDK** | `agent_runtime_sdk/**` | 完全租户无关（静态测试锁定 `tenant` 不在 SDK 文本）。 |
| **Adapters** | `app/adapters/agents/{acp,minimal,local}.py`、`app/adapters/channels/{wecom,local}.py` | Runtime 不变；WeCom 验签保持原状，仅作为 TenantResolver 下游被调用（§12）。 |
| **Runtime Application** | `app/application/agent_dispatch.py`、`interaction_recording.py`（PersistingCoordinator）、`coordinator.py`、`routing.py`、`session_resolution.py`、`session_creation.py`、`identity_resolution.py`、`delivery.py` | 经 tenant-scoped UoW 获得已隔离 repo；**逻辑与签名不变**（§14）。 |
| **Worker / Driver** | `app/application/worker.py`、`driver.py` | 代码逻辑不变；仅其 `uow_factory` 被绑到具体 tenant（§8 末）。 |
| **HTTP Schemas** | `app/schemas/**` | `tenant_id` 永不进入 HTTP body（ingress 规则 §12）。 |
| **Interaction ORM 列集** | `app/persistence/models/interaction.py` 既有列 | 仅加性加 `tenant_id`，不删不改列；`response_payload` 形状不变。 |

---

## 14. 证明：Runtime diff = ZERO semantic change

**命题**：引入 Tenant Isolation 后，任意一次业务操作的**运行时语义**（输入输出、状态跃迁、失败语义、证据链）与隔离前**逐字节一致**。

**证明链（逐层，均有代码事实支撑）：**

1. **Runtime 契约零 tenant** —— 6 个运行时契约（`AgentRequest`/`AgentResponse`/`RuntimeExecutionResult`/`Session`/`SessionResolution`/`InboundInteraction`）字段面无 `tenant_id`，且 `app/domain/**` 的 8 个业务实体也无 `tenant_id`（§13.2 冻结）。Agent Runtime 完全不知道 Tenant。
2. **Domain 层零污染** —— `tenant_id` 只在 ORM 与 DB。写路径：`to_orm(entity)` 仍只产业务字段，Repo 在 `add` 前显式 `row.tenant_id = tid`（§8.3）。读路径：Mapper `to_domain(row)` 不读 `tenant_id`（如 `User(id=row.id, name=row.name, status=row.status)`）。→ Domain 对象构造逻辑与隔离前**完全相同**。
3. **Application / Runtime 执行代码零改动** —— `agent_dispatch`/`coordinator`/`routing`/`session_*`/`identity_resolution`/`delivery`/`interaction_recording`/`worker`/`driver` 的方法签名与内部逻辑不变，仅接收 tenant-scoped UoW（§13.2）。它们调用的 repo 方法名 / 参数 / 返回类型不变（Port 签名冻结，§13.2）。
4. **Adapter / SDK 零改动** —— `app/adapters/**`、`agent_runtime_sdk/**` 完全不动（§13.2）。
5. **唯一运行时 delta = 加性分区过滤** —— 每个查询多带 `tenant_id == <current>` 条件 + RLS `SET LOCAL`。在**单租户**场景（OSS 本地 / Cloud 单租户 tenant #1，§11）：所有行 `tenant_id=1`，该过滤是**恒等过滤**（全集），RLS 亦恒等放行。→ 观测到的查询结果与隔离前**完全一致**。
6. **状态机 / 证据链不变** —— `processing_status`/`delivery_status` 双状态、`failure_stage`/`failure_code`、Attempt Fence（`claim_processing` CAS + `record_outcome` 围栏）、`reclaim_stale_processing` 孤儿回收语义**全部保留**（仅其 WHERE 加租户范围，不改变状态跃迁判定 / 围栏等式 / cutoff 由 DB 时钟计算等冻结语义，§8.5）。

**结论**：Tenant Isolation 是**纯加性数据分区**，叠加在不变的商业语义之上；对单租户（含所有现有 OSS / 本地场景）而言 delta 为恒等变换 ⇒ **Runtime diff = ZERO semantic change**。多租户只是同一套语义在 `tenant_id` 维度上的并行实例。

**守护测试（已实现 + 待补，§16）**：`test_runtime_contracts_have_no_tenant_id`、`test_no_implicit_global_tenant_context`、`test_sdk_is_tenant_agnostic`、`test_import_boundaries` 共同机器化锁定上述不变式。

---

## 15. 一个额外建议（Implementation Boundary 补充）

**建议：把「第一层 Repository Scope」做成 CI 静态护栏（AST / 代码扫描），与 RLS（第二层）形成互补的纵深。**

具体：在仓库加一个轻量检查（pre-commit / CI step），对任意 `app/persistence/repositories/*.py` 中 8 个 adapter 的查询构造做 AST 扫描，断言**每个 `select(Model)` / `update(Model)` / `session.get(Model, ...)` 都最终带 `Model.tenant_id == self._tenant_id`**（或落在已知豁免白名单，如 `SqlAlchemyTenantRepository` 的 bootstrap 读）。

- 价值：RLS 是运行时兜底，但**无法防止「应用层本该显式隔离却漏写」这种回归**；静态护栏把第一层变成**编译期 / CI 期可失败**的不变量，使两层防线都能被机器守护。
- 成本极低：纯 AST 文本扫描，无运行时开销，无 DB 依赖。
- 与本原则的绑定：§0 铁律 2 要求两层「同时成立」，此建议正是把铁律落成可自动验证的工程约束。

（支撑性次要措施，非必需：新增 `TenantIsolationTestCase` 基类，在两条 tenant scope 下运行同一 repo 用例，断言跨租户不可见 / 不可改——作为运行时回归网。）

---

## 16. Implementation Verification Gate（实现验证门 / 待补测试清单）

新增 `tests/test_cloud_0_1_1_b_tenant_isolation_impl.py`，至少覆盖：

1. `test_tenant_model_and_fk_present`：9 表（含 `tenants`）注册；8 表含 `tenant_id` FK→tenants.id；`tenants` 无 `tenant_id`。
2. `test_unique_constraints_lead_with_tenant`：3 表唯一约束前导 `tenant_id`；`sessions` 新增复合唯一约束存在。
3. `test_repo_queries_carry_tenant_filter`：8 adapter 每个读查询含 `tenant_id==self._tenant_id`（AST 或运行时 assert）；`get_by_id` 为 scoped select。
4. `test_repo_write_stamps_tenant`：`create` 后行 `tenant_id == 注入值`，Domain/Mapper 未携带 tenant。
5. `test_uow_sets_local_and_injects_tenant`：`__enter__` 执行 `SET LOCAL app.tenant_id` 且 8 repo 均带 `tenant_id`。
6. `test_cross_tenant_not_visible`（运行时回归网）：tenant A 写入不可被 tenant B 的 repo/UoW 读出 / 改写（第一层 + 第二层共同生效）。
7. `test_rls_policy_enforced_on_postgres`（仅 PG 跑）：未设 `app.tenant_id` 时，普通 role 读 8 表返回空集；`SET LOCAL` 后仅见本租户。
8. `test_async_paths_tenant_scoped`：`find_pending` / `reclaim_stale_processing` / `claim_processing` / `record_outcome` 均在租户范围内（防跨租户误处理）。
9. `test_runtime_contracts_still_tenant_free`（回归）：6 契约 + 8 domain 实体无 `tenant_id`（守 §14）。
10. `test_local_sqlite_single_tenant_identity`：本地 `tenant_id=1` 下，行为 == 隔离前（守 §11 / §14）。

---

## 17. GO / 边界闭合

本 Implementation Boundary 已把 Cloud-0.1.1-B 的 Tenant Isolation **精确写死**于：

- `tenants` model（§1）、8 表 `tenant_id` FK（§2）、UNIQUE 调整（§3）、indexes（§4）、Alembic 0002（§5）、`TenantScope`（§6）、UoW `SET LOCAL`（§7）、Repository filtering（§8）、PG RLS（§9）、bootstrap/provisioning（§10）、SQLite `tenant_id=1`（§11）、ingress 边界（§12）；
- 并**正式确立**「第一层 Application Repository Scope ⊕ 第二层 PostgreSQL RLS，二者正交、必须同时成立、RLS 不替代 Repo filtering」为 **SteeringFlow Cloud 安全架构原则**（§0）；
- 明确 **MUST MODIFY / MUST NOT TOUCH** 清单（§13）；
- 再次**证明 Runtime diff = ZERO semantic change**（§14）；
- 给出**一个额外建议**：CI 静态护栏守护第一层（§15）。

进入实现期时，严格按本边界 + §13 清单落地，不得破坏 §0 原则、§14 运行时边界与 §13.2 冻结清单；新增实现以 §16 验证门为合入门。

---

## 18. 实施三红线 + Implementation DoD（owner 追加，冻结）

### 18.1 实施三红线

**红线 A：不要为了 `tenant_id` 修改 Domain / Mapper。**
保持方向 `Domain object → to_orm() → row.tenant_id = tenant_id`。**禁止**出现 `Domain(..., tenant_id=...)` 或 `Session(..., tenant_id=...)`——tenant 不得进入 Domain 构造。

**红线 B：不要为了 Repository scope 修改 Port 方法签名。**
tenant 在 **Repository 实现 / UoW Scope 层**解决，**不得**把 `get_agent(agent_id)` 污染成 `get_agent(agent_id, tenant_id)`。否则 Tenant 会从 Persistence Concern 漏进 Domain / Application Contract（违反 §13.2）。

**红线 C：RLS 测试不得做成「只有 PG 才能证明应用隔离」。**
必须**分别、独立**验证两层：
- `Application layer`：Tenant A 的 Repository/UoW **无法** select / update Tenant B；
- `PostgreSQL direct access`：在 Tenant A 的 `SET LOCAL` 下**直连 DB**也看不到 Tenant B。
否则两层防线没有被独立证明。

### 18.2 Implementation DoD（直接采用 §16 十项，不再增加功能性 DoD）

1. fresh PG migration（`alembic upgrade head` 从空库到 head 成功）。
2. `tenants` + 8 tables schema。
3. `tenant_id` / FK / indexes / UNIQUE。
4. `TenantScope` lifecycle。
5. `SET LOCAL` cleanup（事务结束自动清除，连接池复用无残留）。
6. Repository cross-tenant isolation。
7. RLS cross-tenant isolation。
8. Worker/Driver tenant-scoped processing。
9. existing SQLite behavior（本地单租户行为逐字节不变）。
10. Runtime semantic diff = ZERO。

**外加必做：fresh clone validation。**
本次涉及 migration + requirements + runtime infrastructure boundary，**working tree 通过远远不够**：须在干净 clone（无缓存 / 无本地 .db / 无未跟踪文件）上从零安装依赖并跑通 migration + 全量测试。

---

## 19. 测试演进面清单 + 实施顺序（实现前必读）

### 19.1 原则：区分「pre-tenant 基线锁」与「运行时边界锁」

- **运行时边界断言（KEEP，不得削弱）**：`test_runtime_contracts_have_no_tenant_id`、`test_no_implicit_global_tenant_context`、`test_sdk_is_tenant_agnostic`、`test_import_boundaries`、`RUNTIME_EXECUTION_ORDER` / 契约字段面冻结——**全部保留**，实现后必须继续绿。
- **pre-tenant 基线锁（EVOLVE，合法演进）**：大量测试把「8 表集合 / 精确列清单 / 精确唯一约束名」写死为**实现前基线**。加 `tenants` + `tenant_id` 后，这些是**预期的 schema 演进**，须改为断言**实现后**的不变量（9 表、含 `tenant_id` 的列集、`(tenant_id, ...)` 前导唯一约束）。这些测试的存在正是为防止「悄悄漂移」，演进它们是本步的**合法组成部分**，不是破坏。

### 19.2 会因 schema 演进而必须更新的既有测试（已勘察）

| 测试文件 | 锁定的内容 | 演进方向 |
|---|---|---|
| `tests/conftest.py` | `CORE_TABLES`（8 业务表） | 保留 8 业务表常量；另设 `TENANT_TABLE="tenants"`；`db_engine` 依赖 ORM `after_create` 自动种 Tenant#1（零改动即满足 FK）。 |
| `tests/test_models.py` | Test A 恰 8 表 / 每表精确列清单 / 唯一约束名与列 | 9 表；各表列尾追加 `tenant_id`；3 唯一约束改 `tenant_id` 前导新名；`sessions` 增新复合唯一约束。 |
| `tests/test_database.py` | `set(Base.metadata.tables) == set(CORE_TABLES)` | 改为 `set(CORE_TABLES) \| {"tenants"}`。 |
| `tests/test_cloud_0_1_1_b_tenant_architecture_gate.py` | 「tenant 尚未引入」全基线（8 表无 tenant / tenants 不存在 / 仅 0001 / UoW 源码串） | **重写为「实现后」不变量**：9 表含 tenants、8 表含 tenant_id FK、唯一约束前导 tenant_id、存在 0002、UoW 注 tenant；**保留运行时契约无 tenant 断言**。 |
| `tests/test_cloud_0_1_1_a_postgres_alembic_foundation.py` | baseline 0001 恰 8 表（KEEP）；PG head 恰 8 表（EVOLVE） | 0001 断言不动（0001 不改）；PG `upgrade head` 后表集改 `8 + tenants + alembic_version`。 |
| `tests/test_step14_2_2_fresh_sqlite_schema_init.py` | `EXPECTED_TABLES` 恰 8 / 「无额外业务表」 | 期望集加 `tenants`；「无额外业务表」语义 = 8 业务表 + tenants 根。 |
| `tests/test_step5_7_closure.py` | `EXPECTED_8_TABLES` 集合 / 目录文件面 | 表集加 `tenants`；`models/` 文件面 +`tenant.py`。 |
| `tests/test_step12_9_4_12_attempt_fence.py` | `test_f_still_exactly_eight_core_tables` | 表集加 `tenants`（释放语义不变）。 |
| `tests/test_step10_5_1` / `test_step10_5_2` | interaction 唯一约束名/列 | 改 `uq_interactions_tenant_channel_external_id` + 含 `tenant_id`。 |
| `tests/test_step10_6_1` | connection 唯一约束 | 改 `uq_connections_tenant_channel_agent_id` + 含 `tenant_id`。 |
| `tests/test_step10_6_2` | session 唯一约束（原断言 `len==?`） | 断言新 `uq_sessions_tenant_identity_conn_conv`。 |
| `tests/test_step11_1` | agents ORM 无 `UniqueConstraint` | 保持不变（agents 仍无业务唯一约束；`tenant_id` 只加列）。 |
| `tests/test_acp_v0_2_artifact_implementation.py` | `metadata.tables` 键集 | 随 9 表更新（如断言全集）。 |
| 其余引用 `create_all` / `CORE_TABLES` 的测试 | 多为建表辅助，非 schema 断言 | 多数无需改；逐一 `pytest` 验证。 |

> 目标：**除上表 schema 基线锁外，其余测试（约 100+）零改动通过**（靠 §2.1 的 `default=1` + Tenant#1 种子实现）。

### 19.3 实施顺序（依赖序，逐增量保持绿）

1. **Increment 1 — 边界最终化 + Schema 根基**：本文件 §18/§19；`app/core/tenant.py`；`app/persistence/models/tenant.py`（+`after_create` 种子）；`models/__init__` 导出；8 模型 `tenant_id` + 唯一约束；同步演进 §19.2 的 schema 基线锁 → 全量绿。
2. **Increment 2 — UoW + Repository Scope**：`unit_of_work.py`（`tenant_id` 可选 + `SET LOCAL` + 注入）；8 repo adapter（tenant 过滤 + 写入 stamping + scoped `get_by_id`）；`repositories/tenant.py`；新实现测试（§16 的 3/4/5/6）。
3. **Increment 3 — Ingress + 组合根**：`app/api/**` 入口解析 `TenantScope` + `functools.partial` 工厂桥；`main.py` 按请求装配；Worker/Driver tenant fan-out（不变量：每次 `find_pending`/`claim`/`reclaim` 都在该行 tenant 的 `SET LOCAL` 事务内）。
4. **Increment 4 — Cloud Schema + 数据迁移**：`alembic/versions/0002_tenant_isolation.py`（§5 顺序 + RLS §9）；`tools/sqlite_to_pg_import.py`（§11）；PG-gated RLS 测试（§16 的 7）。
5. **Increment 5 — DoD 收口**：§16 全 10 项 + fresh clone validation（§18.2）。
