# M6.6-C4 / CP4-1-B — Session Concurrency Implementation

> 上一阶段：`M6.6-C4 / CP4-1-A` 只读审计（Final Gate = PASS）。
> 本阶段：实现 **Session Resolver / Persistence Concurrency Closure**。
>
> **Final Gate：PASS**（12 项门控全绿；PG 侧 2 项 env 门控默认 skip，已具备真实 PG 断言）。

---

## 1. Implementation Summary

并发 `CREATE_NEW` 下，两条消息对**同一四元组**
`(tenant_id, identity_id, connection_id, external_conversation_id)` 各自 `find_active → none`
→ 各自 `INSERT`。`sessions` 的租户前导唯一约束
`uq_sessions_tenant_identity_conn_conv`（0002 物化）令其中一条命中 `IntegrityError`，
旧实现把它直接重新抛成 `SESSION_RESOLUTION_FAILED` ⇒ 合法并发消息被 STOP。

修复：新增 `SessionRepository.find_or_create`（**与 `Identity.find_or_create` /
Grant 同构**）——写冲突时回滚嵌套事务（SAVEPOINT）后**按精确四元组回读**：
- 回读到 ⇒ **复用**（两条消息落到同一 Session）；
- 回读不到 ⇒ 原样抛出 ⇒ `SessionCreationExecutor` 转 `SESSION_RESOLUTION_FAILED`（Fail Closed）。

**恢复的成功判据不是「捕获了异常」，而是「回读确实命中」**。唯一约束**不改、不删、不放宽**。

## 2. Files Changed

| 文件 | 变更 |
|---|---|
| `backend/app/repositories/session.py` | Port 增 `find_or_create`；修正过时 docstring（「无唯一约束」已被 0002 翻转） |
| `backend/app/persistence/repositories/session.py` | Adapter 实现 `find_or_create`（SAVEPOINT + 精确四元组回读）；import `IntegrityError, OperationalError` |
| `backend/app/application/session_creation.py` | `uow.sessions.create` → `uow.sessions.find_or_create`；组件不再承担竞争恢复；修正过时 docstring |
| `backend/tests/test_step9_7_session_creation_execution.py` | Port/Adapter 方法面集合 +`find_or_create`；`test_h_reselect_call_is_caught` 替换目标同步；docstring 锁定项 D/F 校正 |
| `backend/tests/test_step9_3_session_execution.py` | Port/Adapter 方法面集合 +`find_or_create` |
| `backend/tests/test_m6_6_c4_cp4_1_session_concurrency.py` | **新增**：B1~B13 + PG 门控 B4/B4b |

**零** migration、**零** schema 变更、**零** 新错误码、**零** 新配置项、**零** Runtime 阶段变更。

## 3. Repository API

```python
class SessionRepository(Protocol):
    def find_or_create(self, session: Session) -> Session: ...
```

- 输入承载 = `Session` 实体（`identity_id` / `connection_id` / `external_conversation_id`）
  **+ UoW 作用域的 `tenant_id`** ⇒ 完整四元组；
- 返回 = 既有或新建的 `Session`；
- **不引入** created/reused Result 抽象（保持既有 repository 风格，调用方只关心「拿到 Session」）；
- 回读**只走** `find_active`（tenant 作用域 + 精确三键），**不做** identity-only /
  connection-only / 全局查找。

> 方法名与语义对齐 `Identity.find_or_create` / `ConnectionConversation.find_or_create`。

## 4. Transaction Recovery

```
find_active(key)  →  命中 ⇒ 返回（不写）
        │ miss
        ▼
begin_nested()  →  create()  →  成功 ⇒ 返回新行
        │                    └  抛错 ⇒ SAVEPOINT 回滚
        ▼
find_active(key)（精确四元组）  →  命中 ⇒ 复用
                              └  miss ⇒ raise（→ SESSION_RESOLUTION_FAILED）
```

**为什么是 SAVEPOINT（`begin_nested`）而不是整体 rollback**：
UoW 在进入事务时执行 `set_config('app.tenant_id', …, true)`（`unit_of_work.py:104-108`）——
它是**事务级** RLS 上下文。整体 rollback 会清掉它，随后的回读将失去租户作用域
（PG `FORCE RLS` 下回读会 fail-closed 返回空 ⇒ 变成伪失败）。SAVEPOINT 保留外层事务
及其 RLS 上下文，回读仍在租户作用域内。这与 `Identity.find_or_create` 的既有理由完全一致。

**调用方语义**：`SessionCreationExecutor` 只表达创建意图并委派；`except Exception →
SessionResolutionFailedError` 保持不变——它现在**只在回读落空时**才生效。

## 5. PostgreSQL Behavior

- 隔离级 `READ COMMITTED`：输家的 INSERT 会等赢家在唯一索引上的事务落定；赢家 commit 后
  输家收到 `IntegrityError`；
- SAVEPOINT 回滚后，同事务内的后续 `SELECT` 拿到**新的语句快照** ⇒ 可见已提交的赢家行；
- 回读走 tenant 作用域 + RLS 双层防线 ⇒ 不跨租户。
- 断言：`test_b4_postgres_unique_race_recovers_to_reuse`（两条都成功、恰好 1 行）、
  `test_b4b_postgres_reread_stays_within_tenant`（跨租户不可见）。**env 门控**
  （`CLOUD_TEST_DATABASE_URL`，未设则 skip，与既有 PG 测试同形）。

## 6. SQLite Behavior

- 唯一冲突通常在 `flush` 抛 `IntegrityError`；
- 真正的多线程写竞争下，输家可能抛 `OperationalError: database is locked` ⇒ 实现**同时捕获
  两者**；
- **不把「捕获到 OperationalError」当成功**：必须回读命中才算恢复；
- 若某类写锁导致回读落空 ⇒ 原样抛出 ⇒ `SESSION_RESOLUTION_FAILED`（**宁可 fail-closed，
  也不错误复用**）；
- 断言：`test_b5_sqlite_write_lock_is_recovered_by_exact_reread`（确定性构造写锁 +
  首查落空 ⇒ 验证恢复必须经第二次回读，且不产生第二行）。

## 7. Test Matrix

| # | 用例 | 断言 |
|---|---|---|
| B1 | `test_b1_existing_session_is_reused_not_duplicated` | 复用、不重复 |
| B2 | `test_b2_sequential_create_materializes_one_row` | 顺序创建一行 |
| B3 | `test_b3_concurrent_same_four_tuple_yields_one_session` | 两条都成功、1 行、同 id |
| B4 | `test_b4_postgres_unique_race_recovers_to_reuse` (PG) | 冲突 → 回滚 → 回读 → 复用 |
| B4b | `test_b4b_postgres_reread_stays_within_tenant` (PG) | 跨租户不可见 |
| B5 | `test_b5_sqlite_write_lock_is_recovered_by_exact_reread` | 写锁恢复、不产生第二行 |
| B6 | `test_b6_reread_miss_fails_closed` | 回读落空 ⇒ `SESSION_RESOLUTION_FAILED` |
| B7 | `test_b7_cross_tenant_lookup_does_not_reuse` | 跨租户不复用（2 行） |
| B8 | `test_b8_different_conversation_yields_two_sessions` | conversation 维度隔离 |
| B9 | `test_b9_different_identity_yields_two_sessions` | identity 维度隔离 |
| B10 | `test_b10_multiple_interactions_share_one_session` | 1 Session : 3 Interaction |
| B11 | `test_b11_replay_does_not_create_a_second_session` | replay 不新建 Session |
| B12 | `test_b12_worker_reclaim_reuses_existing_session` | reclaim 重跑复用原 Session |
| B13 | `test_b13_concurrent_first_messages_one_identity_grant_session` | 1 Identity + 1 Grant + 1 Session + 2 Interaction |

## 8. Concurrency Evidence

- **B3**（`isolated_db_engine`：文件型 SQLite + 真实连接池 + `timeout=30`，**事务隔离不弱于
  生产**）：两条并发均成功、`len({id}) == 1`、`count == 1`。
- **B13**：并发首条消息（Bootstrap + Session 双重竞争）下，
  `users=1`（不建 ACP User）、`identities=1`、`grants=1`、`sessions=1`、`interactions=2`，
  且两条 Interaction 的 `session_id` 均等于唯一 Session、两条 `processing_status='succeeded'`。
- **B4**（PG）：同形并发断言（env 门控）。

## 9. Bootstrap Integration Evidence

B13 是 CP3-G 的**加强版**：CP3-G 只保证「1 Identity + 1 Grant；至少一条走完管线」，
并显式登记「并发第二条可能落 `SESSION_RESOLUTION_FAILED`（既有 M3 缺陷）」。CP4-1 修复后：

```
两条并发首条消息
      ↓ Bootstrap CAS（赢家消费 Arm，输家复用 Identity/Grant）
      ↓ Identity.find_or_create ⇒ 同一个 identity_id
      ↓ Session.find_or_create ⇒ 冲突 → 回读 → 同一个 Session
  Identity=1  Grant=1  Session=1  Interaction=2（两条 succeeded）
```

⇒ CP3-G 登记的那条缺陷**已关闭**；reuse **不是** Interaction 的去重（两条消息各留一条 Interaction）。

## 10. Regression Result

```
CP4-1 targeted :  12 passed, 2 skipped   (tests/test_m6_6_c4_cp4_1_session_concurrency.py)
full regression:  2318 passed, 40 skipped
```

被修正/增补的既有守卫（`test_step9_3` / `test_step9_7`）在全量回归中通过。

## 11. Scope Integrity

| 项 | 结论 |
|---|---|
| migration / schema | **零新增**（`git status backend/alembic` 仅含上一阶段 CP3-G 的 `0008`） |
| Session 唯一约束 | **未改、未删、未放宽** |
| 新 error code | **无**（复用 `SESSION_RESOLUTION_FAILED`） |
| Runtime 阶段 | **无变化**（`RUNTIME_EXECUTION_ORDER` 断言零改动，全绿） |
| Bootstrap 语义 | **无变化**（未碰 Bootstrap 组件/Arm） |
| 配置项 | **无新增** |
| 越界文件 | 无（仅 §2 的 5 改 1 增；UI / Router / AgentDispatcher / Session 生命周期 / 分布式锁均未触碰） |

## 12. Remaining Observations

1. **SQLite 写锁的读快照**：`begin_nested`（SAVEPOINT）保留外层事务，因此回读与外层事务同快照。
   PG（`READ COMMITTED`）下每次 SELECT 取新快照，必然可见已提交赢家；SQLite 在极端写竞争
   时序下，理论上回读可能仍看不到赢家 ⇒ 该情形**回读落空即 fail-closed**（符合 CP4-1-B §8
   「宁可 fail-closed 也不错误复用」），不会错误复用、不会产生第二行。B5 已确定性覆盖该分支。
2. **`_session_count` 等测试 helper 用原始 session 直查**：仅测试用，不触碰生产路径。
3. **文档漂移已顺手修正**：`SessionRepository` / `SessionCreationExecutor` 中
   「sessions 表当前无唯一约束」的陈述已按 0002 物化事实更正（CP4-1-A §5 登记的漂移，属纯文档）。

## 13. Final Gate

| 门 | 项 | 结果 |
|---|---|---|
| B1 | existing Session reuse | PASS |
| B2 | normal create | PASS |
| B3 | concurrent same 4-tuple → 恰好 1 Session | PASS |
| B4 | PG IntegrityError recovery | PASS（env 门控；断言就绪） |
| B5 | SQLite concurrency recovery | PASS |
| B6 | reread miss → SESSION_RESOLUTION_FAILED | PASS |
| B7 | tenant isolation | PASS |
| B8 | different conversation isolation | PASS |
| B9 | different identity isolation | PASS |
| B10 | multiple interactions share Session | PASS |
| B11 | replay | PASS |
| B12 | worker reclaim | PASS |
| B13 | 并发首条消息 → 1 Identity + 1 Grant + 1 Session | PASS |
| B14 | no migration | PASS |
| B15 | no new error code | PASS |
| B16 | no Runtime stage change | PASS |
| B17 | no Bootstrap semantic change | PASS |
| B18 | full regression | PASS（2318 passed / 40 skipped） |
| B19 | scope integrity | PASS |

```
Final Gate：PASS
```

CP4-1 仅关闭 **Session concurrency**；Session 生命周期 / 过期 / Agent 状态 / memory /
分布式锁 / 多 Agent 语义均**不在**本轮范围，未触碰。
