# M6.6-C4-CP4-1 — Session Concurrency Audit

> 性质：**只读审计**。不修改代码 / migration / 测试 / ADR / 文档 / schema。
> 目标：定位 Session 并发创建冲突的精确根因，判定是否可复用
> "winner insert → loser rollback → reread → reuse" 的事务恢复模式，并给出可实施的
> CP4-1 Implementation Design。
>
> **结论：PASS（可实施）**。根因、语义、恢复模式、DB 行为、修改点全部明确，无新的架构未决项。

---

## 1. Scope

仅覆盖 **同一 `(tenant_id, identity_id, connection_id, external_conversation_id)` 下的并发 Session 创建冲突**。
**不在范围**：Session 过期 / 生命周期重设计 / Agent 会话态 / memory / context / Session locking /
多 Agent / 分布式编排（见 §24 Non-Goals）。

---

## 2. Current Session Domain

`app/domain/session.py`：

- `Session` 是 **frozen dataclass**；关键字段：`identity_id, channel_id, connection_id(可空),
  agent_id(可空), external_session_id(可空), external_conversation_id(可空), status="active",
  metadata, created_at/updated_at`。
- 领域注释：一条 Control Plane 会话 = 「**针对某个 Channel conversation** 的 1:N Interaction 载体」。
  ⇒ 一个 Session **天然承载多个 Interaction**（见 §9）。
- 创建瞬间状态：**没有**任何「创建唯一」的可变语义：
  - `created_at` / `updated_at` 由 DB `server_default=now()` 生成（`persistence/models/session.py`），会话侧不写；
  - `status` 恒 `"active"`；
  - `external_session_id` 初始为 `None`（领域注释：Agent 首次响应后回填，但 `SessionRepository`
    当前**无 update 方法**，MVP 不经 repo 回填）；
  - 无 UUID、无 counter、无 Agent 专属状态、无 activity。

⇒ 两个请求创建的 Session 在「创建时刻」内容**逐字节等价**（除自动 `id`/`created_at`）；
reuse winner 行 = 复用，不是复制（§8 详述）。

---

## 3. Current Runtime Position

```
InboundInteraction
   ↓ Identity Resolution           (app/application/identity_resolution.py)
   ↓ Access Authorization          (ADR-7：在 Session Resolution 之前)
   ↓ Routing                        (决定 connection_id / agent_id)
   ↓ Session Resolution            (app/application/session_resolution.py)
   ↓ Session Creation / Reuse      (app/application/session_creation.py)
   ↓ Dispatch (Agent execution)     (app/application/routing.py / agent_dispatch)
   ↓ Delivery
```
调用序列在 `app/application/coordinator.py:120-145`：
`self._session_resolution.execute(...)` → 若 `CREATE_NEW` 则
`self._session_creation.execute(...)`。

**关键事实**：`session_resolution` 与 `session_creation` 各自 `with self._uow_factory()`
开**独立事务**（§7）。

---

## 4. Current Session Creation Path

`app/application/session_creation.py:46-58`（`SessionCreationExecutor.execute`）：

```python
with self._uow_factory() as uow:
    session = Session(identity_id=..., channel_id=..., connection_id=...,
                      agent_id=..., external_conversation_id=..., status="active")
    created = uow.sessions.create(session)   # flush → 命中唯一约束即抛 IntegrityError
    uow.commit()
    return created
```

`SqlAlchemySessionRepository.create`（`app/persistence/repositories/session.py:14-19`）：
`self._session.add(row); self._session.flush()` —— **直接 INSERT，无 find-or-create**。

`SessionResolutionExecutor`（`app/application/session_resolution.py`）只做 `find_active`
→ 返回 `REUSE_ACTIVE`(命中) / `CREATE_NEW`(未命中)。它是**只读**判定，不持有创建锁。

⇒ 创建是 **find-then-insert**，且 find 与 insert 分属两个事务。

---

## 5. Current Unique Constraint

**权威约束确实存在，且 tenant 前导**：

```python
# app/persistence/models/session.py:74-77
UniqueConstraint(
    "tenant_id", "identity_id", "connection_id", "external_conversation_id",
    name="uq_sessions_tenant_identity_conn_conv")
```

```python
# alembic/versions/0002_tenant_isolation.py:96-99
op.create_unique_constraint(
    "uq_sessions_tenant_identity_conn_conv", "sessions",
    ["tenant_id", "identity_id", "connection_id", "external_conversation_id"])
```

`tests/test_step9_7_session_creation_execution.py:312-327`（`test_f_sessions_unique_constraint_materialized`）
已断言该约束物化。**这正是用户描述的四元组约束，是当前实际 schema 的权威约束。**

⚠️ **文档漂移**：`app/repositories/session.py:34-41` 的 `SessionRepository` docstring 仍写
「sessions 表**当前没有**覆盖该隔离键的唯一约束；第一版依赖 find_active → create 的事务顺序」。
该陈述已被 0002（Cloud-0.1.1-B 隔离演进）**翻转**——实现/测试都已物化约束，唯独 docstring 未同步。
实现阶段应顺手修正此 docstring（**不属本次范围**，仅登记）。

---

## 6. Exact Race Timeline

按真实代码（**非**假设）：

```
T1 (请求 A，Connection 已授权)          T2 (请求 B，同 4 元组)
─────────────────────────────          ─────────────────────────────
SessionResolutionExecutor:               SessionResolutionExecutor:
  txn A1: find_active → None               txn B1: find_active → None
  → CREATE_NEW                            → CREATE_NEW
SessionCreationExecutor:                 SessionCreationExecutor:
  txn A2: create → INSERT Session S         txn B2: create → INSERT
  flush → OK                                flush → UNIQUE violation
  COMMIT (成功)                              IntegrityError
                                            （UoW __exit__ → rollback，见 §7）
                                            except Exception → 重新包装为
                                            SessionResolutionFailedError
                                            ↑ 请求 B 被 Stopped（本可复用 S）
```

注意：A1/B1 与 A2/B2 各自独立事务，**A1、B1 都在任一 INSERT 前完成** ⇒ 两条请求都判定
CREATE_NEW，唯一约束是**唯一**阻止双行的机制，但它以「抛错」形式生效，而非「优雅复用」。

---

## 7. Transaction Boundaries

- `SqlAlchemyUnitOfWork.__exit__`（`app/persistence/unit_of_work.py:128-139`）：
  异常时 `self.rollback()`（整事务回滚），随后 `close()`。
  ⇒ 失败方 `create` 抛 `IntegrityError` 后，其 UoW 自动回滚，**pending 对象被 expunge**，
  连接恢复正常。这是 recover 的前提（回滚后可在新事务 reread）。
- `flush` 位置：`SqlAlchemySessionRepository.create` 内（INSERT 即时 flush）。
- `commit` 位置：`SessionCreationExecutor.execute` 内。
- `IntegrityError` 抛出点：flush（PG/SQLite 均在写行时校验唯一索引）。
- **无 retry**：`SessionCreationExecutor` 的 `except Exception as exc` 直接重新抛
  `SessionResolutionFailedError`，**不 reread、不复用**（这是根因）。

---

## 8. Session Semantics（复用是否成立）

唯一约束语义 = 「同一 4 元组至多一条 active Session」。
⇒ **两个并发请求若 4 元组完全相同，业务上必然属于同一条 Session**（DB 已保证至多一条）。

winner 创建 S；loser 不应失败，而应 **reread + reuse S**：
- 正确性：两条消息落到同一会话，正是 1:N Interaction 的预期（§9）；
- 安全性：S 无「创建唯一」可变态（§2），reuse = 复用行，不复制；
- 结论：**find-or-reuse 语义成立，且不破坏任何 Session 不变量**。

> REUSE（返回 winner 已提交的行）≠ COPY（克隆字段造新行）。本方案取前者：reread
> `find_active` 返回既有 `Session` 域对象，不新建。

---

## 9. Interaction Relationship

- `interactions.session_id` 外键（`0001_baseline_core_tables.py:148`，可空）→ **Session 1:N Interaction**。
- Domain 注释明确 Session 是「针对某 conversation 的 1:N Interaction 载体」⇒
  多个 Interaction 映射同一 Session 是**系统设计允许的正常状态**。
- `Interaction A → Session S`、`Interaction B → Session S` 已是预期（per-conversation 会话）。
- Session 不持有 processing state（无 `status` 区分 processing/idle，只有 active）；
  并发 Interaction 由 interaction 层的 claim/processing 状态机串行化（Worker reclaim，§11），
  与 Session 复用正交。

⇒ Session find-or-reuse 在关系模型上无碍。

---

## 10. Replay Analysis

把 concurrency 与 replay 分开：

- **Case A — 同一条消息重复送达**：`interactions` 表有 `uq_interactions_channel_external_id`
  （`0001:150`）⇒ 真重复在 Interaction 层被拦（与 Session 无关）。**不触发**本竞态。
- **Case B — 同一 conversation 的两条不同消息并发**：即 §6 竞态，修复后两条都成功、共享 S。
- **Case C — worker reclaim**（PENDING→PROCESSING→crash→reclaim）：reclaim 是**按
  interaction 单点 claim** 串行重跑（`interaction_recording` 状态机），**不与新请求并发**；
  且若首次运行已 commit Session，reclaim 的 `session_resolution` 命中 → `REUSE_ACTIVE`，
  根本不进入 create。修复对 C 无副作用。
- **Case D — delivery retry**：Session 早已存在，`session_resolution` → `REUSE_ACTIVE`，
  不进入 create。无影响。

⇒ 修复只改变 Case B 的终态（失败→成功），其余场景行为不变。

---

## 11. Worker Reclaim Analysis

见 §10 Case C。reclaim 复用既有 `REUSE_ACTIVE` 路径，串行化于单 interaction，不引发 Session 竞态。
修复保持 reclaim 现有语义不变。

---

## 12. Delivery Retry Analysis

见 §10 Case D。Delivery retry 发生在 Agent 执行成功之后，Session 已存在，`REUSE_ACTIVE` 命中。
无影响。

---

## 13. Identity Interaction（与 Bootstrap 的关键耦合）

Session 唯一键含 `identity_id`；而 **Bootstrap 已通过 `Identity.find_or_create` 保证
「同一 (channel, external_id) 至多一个 Identity」**（`app/persistence/repositories/identity.py`，
SAVEPOINT + reread 模式）。

⇒ 两条并发首条消息经 Bootstrap（CP3-G）后，得到**相同 identity_id**；加上同 Connection +
同 conversation ⇒ **完全相同 4 元组**。这正是 §6 竞态的输入形态，且因其 4 元组必然相同，
§8 的「reuse 必然正确」结论在 Bootstrap 链路下被进一步坐实。

> 换言之：CP3-G 的 Identity CAS 已经把「两条消息是否同主体」收敛为确定值，Session 竞态
> 因此是一个**干净的同 4 元组问题**，不存在「同 conversation 不同 identity」的歧义。

---

## 14. Existing Find-or-Create Patterns

仓库已有两处现成模式，可**复用其事务恢复 pattern**（非复制代码）：

- `Identity.find_or_create`（`app/persistence/repositories/identity.py`）：
  `find_by_channel_and_external_id` → 命中返回；否则 `with begin_nested(): create`，
  `except IntegrityError` → 回滚至 savepoint → reread → 返回；仍无则 re-raise。
- `Grant.find_or_create`（`app/persistence/repositories/connection_conversation.py`）：同构。

两者都在**同一 UoW 内**用 `begin_nested()` 隔离冲突、reread 复用，不需要开第二个 UoW。
CP3-G 的并发测试 `test_concurrent_first_messages_same_conversation` 已验证该模式在并发下
「至多一个 Identity / Grant」成立——**Session 可照搬同一结构**。

---

## 15. Bootstrap CAS Comparison

| 维度 | Bootstrap Arm（CAS） | Session（当前） |
|---|---|---|
| 冲突触发 | `UPDATE ... WHERE status='armed'` rowcount==0 | `INSERT` 命中唯一约束 |
| 失败表现 | 输家回滚 → 新事务 reread → 复用 | 输家 `IntegrityError` → **被重新抛为 SESSION_RESOLUTION_FAILED** |
| 是否 reread 复用 | 是 | **否（根因）** |
| 恢复模式 | `begin_nested` + reread | 无 |

结论：**可采用相同事务恢复模式**（find-or-create + SAVEPOINT + reread），只需把
「`UPDATE` CAS 输家」换成「`INSERT` 唯一违例输家」。`Identity/Grant.find_or_create` 是
现成的同结构参照。

---

## 16. SQLite Concurrency

- Engine：`app/persistence/database.py:75-78` 仅设 `check_same_thread=False`，
  **无 `BEGIN IMMEDIATE`、无 `StaticPool`**。
- 唯一约束违反在 flush（INSERT）时校验 ⇒ 通常直接 `IntegrityError`。
- **但**真正的多线程并发写 SQLite 时，loser 也可能在 commit 阶段拿到
  `sqlalchemy.exc.OperationalError: database is locked`（SQLite DB 级写锁），
  **而非 `IntegrityError`**。
- 回滚后可开新事务 reread；RLS 不存在（SQLite 无 RLS，走 repo 显式过滤），tenant 由
  UoW 注入，reread 仍受 tenant 边界约束。

⇒ 恢复逻辑**必须同时捕获 `IntegrityError` 与 `OperationalError`**（广义 `SQLAlchemyError`
的子集），否则 SQLite 下 loser 仍可能漏网。

---

## 17. PostgreSQL Concurrency

- `READ COMMITTED`：winner commit 后，loser 的后续 SELECT（新事务）可见 winner 行。
- 唯一索引冲突：loser 的 INSERT 在 winner commit 前会被**短暂阻塞**等待，winner 提交后
  loser 立即收到 `IntegrityError`；loser rollback 后 reread 可见已提交 winner。
- RLS：`SqlAlchemyUnitOfWork` 在事务内 `set_config('app.tenant_id', …, true)`
  （`unit_of_work.py:104-108`），reread 事务自动带 tenant 上下文 ⇒ RLS 仍生效，reread
  不越租户。
- 失败模式单一：`IntegrityError`（无 `database is locked` 类问题）。

⇒ PG 下恢复只需捕获 `IntegrityError`。

---

## 18. RLS / Tenant Isolation

reuse 的 reread 必须保持在 tenant 边界内：
- 复用同一 `uow_factory`（带 `tenant_id`）⇒ repo 显式过滤 + （PG）RLS 上下文双重生效；
- `find_active` 命中键已含 `identity_id + connection_id + external_conversation_id`，
  全部参与 reread；
- **绝不**做跨租户全局 lookup。

满足要求，无新 RLS 风险。

---

## 19. Error Semantics

`SESSION_RESOLUTION_FAILED`（`app/application/errors.py:242-259`）当前**语义过宽**：
它同时代表
(a) 输入/结构非法（identity_id 非正整数、conversation 为空、未知 decision mode）；
(b) **并发唯一冲突**（本可恢复为 reuse）。

这是 **observability defect**：合法的并发第二条消息被错误归类并 Stopped，挤占了真实的
解析失败信号。修复后，(b) 不再抛错（直接 reuse），该 failure code 重新只表达 (a) 的真失败。
**无需新增 error code**（默认 NO；修复是 pure recovery，不改错误分类）。

---

## 20. Root Cause

**Bug（非 expected conflict）**。理由：
- 唯一约束语义保证「同 4 元组至多一条 Session」，故并发第二条消息**本应**复用而非失败；
- 当前实现把「find-then-insert 跨两个 UoW 事务」的良性竞态，错误转换为
  `SESSION_RESOLUTION_FAILED` 并 Stopped 合法消息；
- 这直接破坏「First Message / First Success」在并发下的可达性（CP3-G 并发测试正是
  为同源问题而设）。

根因定位：**`SessionCreationExecutor` 缺 find-or-create 恢复；`SessionRepository` 仅有
`create`（直接 INSERT），唯一约束以抛错而非复用形式生效**。

---

## 21. Proposed Resolution

**推荐：在 `SessionRepository` 增加 `find_or_create`（Port + Adapter），与 Identity/Grant
同构；`SessionCreationExecutor` 改为调用它。** 理由：
- 恢复逻辑（唯一约束）与约束同处持久层，单一可信位置；
- 与 Bootstrap 的 `Identity/Grant.find_or_create` 完全对称，复用已验证的
  `begin_nested` + reread 模式；
- `SessionCreationExecutor` 保持瘦（仅做 fail-closed 字段校验 + 委派），其既有
  `except → SessionResolutionFailedError` 对「reread 仍无行」（极罕见，如 winner 回滚）
  继续 fail-closed。

实现草图（**只读，不落地**）：

```python
# SqlAlchemySessionRepository.find_or_create（同 UoW 内）
existing = self.find_active(identity_id, connection_id, external_conversation_id)
if existing is not None:
    return existing
try:
    with self._session.begin_nested():
        return self.create(session)        # flush；命中唯一约束 → 抛
except (IntegrityError, OperationalError):  # PG: IntegrityError / SQLite: 可能 locked
    found = self.find_active(identity_id, connection_id, external_conversation_id)
    if found is not None:
        return found
    raise                                     # 真无行 → 交由 executor 转 SESSION_RESOLUTION_FAILED
```

```python
# SessionCreationExecutor.execute（改为委派）
with self._uow_factory() as uow:
    created = uow.sessions.find_or_create(Session(...))   # 不再直接 create
    uow.commit()
    return created
```

`SessionResolutionExecutor` **不变**（仍只读判定 CREATE_NEW / REUSE_ACTIVE）。
并发时第二条消息的 decision 仍为 CREATE_NEW，但 creation 的 `find_or_create` 命中 winner 行
→ 返回既有 id → `control_plane_session_id` 正确。

**轻量替代**（最小改动、不扩 Port 面）：把上述 try/except 直接放进 `SessionCreationExecutor`
（用既有 `create` + `find_active`，捕获失败开后新 UoW reread）。两种都正确；推荐前者以与
Bootstrap 对称、便于单测。

---

## 22. Implementation Boundary

| 项 | 是否需要 | 说明 |
|---|---|---|
| 改 `SessionRepository` Port | 是（推荐） | 增 `find_or_create`（与 Identity/Grant 对称） |
| 改 `SqlAlchemySessionRepository` | 是 | 实现 `find_or_create`（SAVEPOINT + reread） |
| 改 `SessionCreationExecutor` | 是 | `create` → `find_or_create`；generic except 保留 |
| 改 `SessionResolutionExecutor` | 否 | 只读判定不变 |
| 改 `coordinator.py` | 否 | 分支逻辑不变 |
| 改 `UnitOfWork` | 否 | 现有 rollback/reread 机制足够 |
| **改 migration / schema** | **否** | 唯一约束已在 0002 物化 |
| **新增 error code** | **否** | 修复是 pure recovery |
| 修 `SessionRepository` 过时 docstring | 建议 | 「无唯一约束」陈述已翻转（治理性修正） |

---

## 23. Test Requirements

1. **`find_or_create` 单测**：同 4 元组二次调用 → 返回同一 `id`，表内仅 1 行。
2. **并发创建测试**（核心，对标 `test_m6_6_c3_cp3_g_bootstrap_runtime.py::
   test_concurrent_first_messages_same_conversation`）：两线程对同一 4 元组各跑
   `coordinator.run` / `session_creation` → 断言**两条 Interaction 均成功**，
   `sessions` 表恰好 1 行，`control_plane_session_id` 相同。
3. **异常捕获覆盖**：SQLite 下显式验证 loser 在并发写下可能被 `OperationalError` 命中，
   修复逻辑仍复用（断言 `catch (IntegrityError, OperationalError)`）。
4. **reuse 不破坏 REUSE_ACTIVE**：单消息正常路径 `find_active` 命中 → 不进 create。
5. **failure code 不变**：断言 `SESSION_RESOLUTION_FAILED` 仍代表真失败（输入非法），
   且并发场景**不再**产生该码。

---

## 24. Non-Goals

不解决：Session expiration、生命周期重设计、Agent 会话态 / memory / context、Session locking、
多 Agent Session 重设计、分布式 Session 编排。仅做 **Session Resolver / 持久化并发闭环**。

---

## 25. Final Gate

```
PASS
```

- 根因明确：跨两事务的 find-then-insert + 缺 find-or-create 恢复（§20）。
- Session 语义允许 reuse（§8，1:N Interaction 预期）。
- 事务恢复模式可复用（§14/§15，与 Identity/Grant `find_or_create` 同构）。
- DB 行为明确：PG=IntegrityError、SQLite=可能 OperationalError，两者用同一应用层
  find-or-reuse 语义（§16/§17/§18）。
- 修改点收敛：`SessionRepository.find_or_create` + executor 委派；**无 migration、无新错误码**（§22）。
- 唯一需实现期验证的是测试覆盖（含 SQLite locked 分支），属 Test Requirements（§23），非架构未决项。

**实施前置**：无架构阻塞；进入 CP4-1 实现时按 §21 草图落地 + §23 测试即可。
