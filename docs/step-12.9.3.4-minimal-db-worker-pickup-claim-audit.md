# Step 12.9.3.4 — Minimal DB-backed Worker Pickup & Claim Audit

> 只读审计（承接 12.9.1 / 12.9.3.1 / 12.9.3.2 / 12.9.3.3）。聚焦**最小 DB 支撑的
> Worker 拾取与 claim** 的可用性与事务边界。**不实现**。
>
> 证据基线：
> - Repository：`persistence/repositories/interaction_record.py`（无 `find_pending`；
>   `mark_processing`/`record_outcome` 均非 CAS）
> - DB 抽象：`persistence/database.py`（SQLite 为 MVP 真实库，PostgreSQL 为未来；
>   `DATABASE_URL` 切换）
> - `processing_status` 字面量：`domain/interaction_record.py:33-36`
>   （`pending`/`processing`/`succeeded`/`failed`）
> - 现有 claim/执行边界：`application/interaction_recording.py:152-181`（两短事务）
>   + `:195-224`（终态独立短事务）；UoW 短事务设计 `persistence/unit_of_work.py`

---

## A. Pickup —— 现有 repository / DB 能否安全 find PENDING？

**结论：当前「能力缺失」（无 `find_pending`），但可最小新增一个只读 SELECT，且安全。**

- 现有 Repository（`interaction_record.py`）读路径只有：
  - `get_by_id`（`:59`，按主键）
  - `find_by_channel_and_external_id`（`:65`，10.5.2 Interaction Identity 精确读）
  - **没有「按状态批量查找」的方法**——`find_pending` / `list_by_status` 均不存在。
  生命周期注释（`:5-7`）明确「读路径 = get_by_id + find_by_channel_and_external_id」，
  即 pickup 能力**尚未实现**。
- 能否最小新增且安全？**能。** pickup 只是：
  ```sql
  SELECT * FROM interactions
   WHERE processing_status = 'pending'
   ORDER BY created_at ASC
   LIMIT :n;
  ```
  这是一个**只读、无锁的 SELECT**，SQLite 与 PostgreSQL 均原生支持，不触碰任何写、
  不持锁、不阻塞 intake 的 PENDING 插入。
- 「安全」的边界：pickup 本身**不 claim**，因此多次扫描、候选集重叠都不会造成双执行——
  序列化点只在 claim（见 B/C）。且 PENDING 在 intake 返回 200 前已 committed
  （12.9.3.3 §1），Worker 扫描到的 PENDING 都是 durable 的，不存在读到未提交行的问题。

→ **A 答：现有 repo 不能直接 find PENDING（缺方法）；但该能力是「最小只读 SELECT」，
   可安全新增，无 schema 变更、无锁、无阻塞风险。**

---

## B. Claim —— SELECT candidate → CAS → rowcount=1 → execute 能否保证两 Worker 不双执行？

**结论：能。靠「带 `processing_status='pending'` 守卫的单语句条件 UPDATE + rowcount 门控」即可，DB 无关、无需 FOR UPDATE。**

- 当前 `mark_processing`（`interaction_record.py:30-33`）是**非原子**的：
  ```python
  row = self._require(interaction_id)        # SELECT ... WHERE id=X
  row.processing_status = "processing"        # 无条件赋值
  self._session.flush()
  ```
  没有 `WHERE processing_status='pending'`，没有 rowcount 检查。两个 Worker 都会成功。
- 最小改造为 CAS claim（复用现有 `processing_status` 列，**无需改 schema**）：
  ```python
  from sqlalchemy import update
  stmt = update(InteractionModel).where(
      InteractionModel.id == interaction_id,
      InteractionModel.processing_status == "pending",
  ).values(processing_status="processing",
           processing_started_at=func.now())   # 12.9.3.2 §6 最小字段
  rowcount = self._session.execute(stmt).rowcount
  # rowcount == 1 → claim 成功；== 0 → 他人已 claim，放弃
  ```
- 两 Dialect 可行性：标准 `UPDATE ... WHERE ...` + `rowcount` 在 **SQLite 与
  PostgreSQL 都正确返回受影响行数**（SQLAlchemy `Result.rowcount`）。无需行锁。
- **保证**：同一行只有一条 `WHERE processing_status='pending'` 的 UPDATE 能命中
  （rowcount=1），另一条命中 0 行 → 第二个 Worker 立即放弃。**因此同一 interaction
  绝不会被两个 Worker 同时 execute。**

→ **B 答：能。CAS（`WHERE processing_status='pending'` + rowcount）即足够，DB 无关，
   不依赖 FOR UPDATE / SKIP LOCKED。**

---

## C. Multi-worker 窗口（A→#1 AND B→#1）—— 是否存在？最小解是什么？

**结论：CAS 下该双执行窗口不存在；最小解 = 原子 CAS，而非 SELECT FOR UPDATE SKIP LOCKED。**

- 场景：Worker A 与 B 同时扫描 PENDING {#1, #2, #3}，候选集**可能重叠**（二者都含 #1）。
  - A 逐行 claim：claim(#1) → rowcount=1（A 持有 #1）；claim(#2) → 1。
  - B 逐行 claim：claim(#1) → rowcount=0（失败，跳过）；claim(#3) → 1（B 持有 #3）。
  - 结果：A→{#1,#2}，B→{#3}。**没有 A→#1 且 B→#1 的双执行。**
- 关键认知：**pickup 的重叠无关紧要，claim 的原子性才是序列化点**。只要 claim 是 CAS，
  扫描阶段怎么重叠都安全——重叠只意味着某些 claim 返回 rowcount=0（廉价 no-op）。
- **为何不依赖 SKIP LOCKED**（按用户要求先看实际支持）：
  1. **SQLite（MVP 真实库，`database.py:23`「SQLite 就是 MVP 的真实数据库」）根本
     不支持 `SELECT ... FOR UPDATE` / `SKIP LOCKED`**——SQLite 的锁在**库级**
     （`BEGIN IMMEDIATE`/事务），没有行级 `FOR UPDATE`。若采用 SKIP LOCKED，SQLite
     路径直接不可用，破坏「换 PostgreSQL 只改 DATABASE_URL」的设计前提
     （`database.py:23`）。
  2. **即便在 PostgreSQL**，SKIP LOCKED 只是 SELECT 阶段「跳过已被锁行」的**竞争优化**
     （减少无谓的 rowcount=0 回退），其**正确性保证仍来自 CAS**——SKIP LOCKED 本身
     不替代 claim 的原子性。
  3. **与事务边界的危险组合**：若 claim 用 `SELECT ... FOR UPDATE` 且在**同一个长事务**
     内执行 16s 的 `Runtime.execute()`（即 D 节要否决的第二种形态），该行锁将被持有
     整整 16s，反而把 Worker 池**串行化**——这与引入 Worker 的初衷相悖。
- **最小解决方式**：纯 CAS claim（B 节的 `WHERE processing_status='pending'` +
  rowcount）。可选的小优化：pickup 用 `ORDER BY created_at ASC LIMIT n`，让最老的
  PENDING 先被拾取；竞争只是更多 rowcount=0 的廉价回退，MVP 规模完全可接受。

→ **C 答：CAS 下 A→#1 与 B→#1 的双执行窗口不存在。最小解 = 原子 CAS，无需也
   不应依赖 SKIP LOCKED（SQLite 不支持；Postgres 下也仅是可选竞争优化，且有长事务
   锁风险）。**

---

## D. Transaction Boundary —— 证据

**结论：claim 必须是 `BEGIN; CAS; COMMIT` 的短事务，随后 `Runtime.execute()` 在
【无任何打开事务】下运行，再 `BEGIN; record; COMMIT`。第二种（把 16s 执行裹进事务）必须
否决。以下为证据。**

### 证据 1：现有代码已经是「短事务 claim → 无事务执行 → 短事务终态」三明治
`interaction_recording.py` 的 `PersistingCoordinator._start` 与 `_record_*`：

```python
# _start（:161-181）—— 两个各自提交的【短事务】
with self._uow_factory() as uow:
    record = uow.interactions.create(...)   # 事务①
    uow.commit()
with self._uow_factory() as uow:
    uow.interactions.mark_processing(...)    # 事务②（claim-like 短事务）
    uow.commit()
# ← 此处【没有打开的 UoW / Session】
result = self._inner.execute(inbound)       # :143 —— 执行在【任何事务之外】
# ← 终态用【新的短事务】
with self._uow_factory() as uow:             # _record_success :195
    uow.interactions.record_outcome(...)
    uow.commit()
```

即**当前同步设计已经把 `RuntimeCoordinator.execute` 放在两个短事务之间、自身不持事务**。
Worker 的 claim 应原样复用这一形态：CAS 短事务提交 → 关 Session → `Runtime.execute()`
（无 Session）→ 终态短事务。这是项目已建立、已测试（test_step12_6 装配守护）的模式，
不是新发明。

### 证据 2：UoW 是「每操作一个短事务」设计
`unit_of_work.py:80-87`：`__exit__` 仅 rollback(异常)+close，`commit()` 显式调用。
一个 `with uow` 块 = 恰好一个短事务；**不存在「长事务」原语**。要把 16s 执行裹进事务，
只能错误地让一个 `with uow` 块跨越 `Runtime.execute()`——这违背了 UoW 的边界纪律
（`unit_of_work.py:11-25`：「Repository 只 flush，commit/rollback 永远属于 UoW」、
「不引入事务框架，现在就是 Session+commit+rollback」）。

### 证据 3：SQLite（MVP 真实库）库级锁 —— 16s 长事务灾难性
`database.py:23` 明确「SQLite 就是 MVP 的真实数据库」；`:50-60` 显示 SQLite 需
`PRAGMA foreign_keys=ON` 且默认库级锁。若 claim 与 16s 执行同处一事务：
- 该写事务持有 SQLite 的库级写锁整整 16s → **期间所有其他写入（其它 Worker 的 claim、
  甚至 intake 的 PENDING 插入）被完全阻塞** → Worker 池被串行化为 1，intake 卡死。
这是 MVP 开发/测试路径上的直接不可用。

### 证据 4：PostgreSQL —— 连接池耗尽 + 行锁 16s
即便未来切 PostgreSQL（`database.py:23`「换 PostgreSQL 只需改 DATABASE_URL」）：
- 16s 打开的事务占用一个连接池连接 → 并发下连接池迅速耗尽（`SessionLocal`
  `database.py:86` 为固定 `sessionmaker` 池）。
- 若 claim 用了 `SELECT ... FOR UPDATE`，该行锁被持有 16s → 阻塞 12.9.3.2 §6 的
  孤儿回收扫描（`WHERE processing_status='processing' AND processing_started_at < now()-TTL`
  也想访问同一行/表）。

→ **D 答（强证据）：claim 用 `BEGIN; CAS; COMMIT` 短事务；`Runtime.execute()` 在零打开
   事务下运行；终态用独立短事务。第二种形态被 SQLite 库级锁 + Postgres 连接池/行锁
   双重否决，且违反现有 UoW 短事务纪律与已建立的 `_start→execute→_record` 三明治。**

---

## E. Worker 与 Runtime 的边界 —— 最终验证

**结论：边界成立且干净。Worker 拥有 pickup / claim / execute / record outcome；
RuntimeCoordinator 只拥有六段编排，无 DB 访问故根本不具备 pickup / ownership 能力。**

- **Worker（驱动者）职责**：
  - `pickup`：调用新增的 `find_pending`（只读 SELECT，A 节）；
  - `claim`：调用 CAS `claim_processing(id, token)`（B/C 节，原子短事务）；
  - `execute`：调用 `RuntimeCoordinator.execute(inbound)`（裸引擎，无事务，D 节）；
  - `record outcome`：调用 `record_outcome(id, result|exc)`（独立短事务）。
- **RuntimeCoordinator（`coordinator.py:85`）职责**：仅六段——
  Identity → Routing → Session Resolution → [Session Creation?] → Agent Dispatch →
  Delivery（`:85-159`）。
- **为何边界天然干净**：`coordinator.py:26-27, :65` 明确「本模块不 import 任何
  Repository / Adapter / Persistence / SQLAlchemy」「不触碰 ORM」。RuntimeCoordinator
  **没有任何 DB 句柄**，因此它在物理上**无法**做 pickup 或 ownership——这两个概念只
  存在于 Worker + persistence 层。这正是 12.9.3.3 §9「一个且仅一个 Runtime execution
  path」的保证：引擎只懂「执行一次交互」，何时执行、执行哪一条由 Worker 决定。
- 与 12.9.3.3 一致：RuntimeCoordinator **零改动**；ownership/pickup 是 Worker 新增
  的驱动职责，不反向污染引擎。

→ **E 答：验证通过。Worker = 驱动（pickup/claim/execute/record）；RuntimeCoordinator
   = 纯执行引擎（六段），无持久化、无 ownership 能力，边界不可逾越。**

---

## 速查表

| 节 | 问题 | 答案 |
| --- | --- | --- |
| A | 现有能否安全 find PENDING | 能力缺失（无 `find_pending`）；可最小新增**只读 SELECT**，安全、无锁、无 schema 变更 |
| B | CAS 能否保证不双执行 | 能；`UPDATE ... WHERE processing_status='pending'` + rowcount，DB 无关，无需 FOR UPDATE |
| C | A→#1 且 B→#1 窗口 | CAS 下**不存在**；最小解=原子 CAS；**不用 SKIP LOCKED**（SQLite 不支持；PG 下仅可选竞争优化且有长事务锁风险） |
| D | 事务边界 | `BEGIN;CAS;COMMIT` → 无事务 `Runtime.execute()` → `BEGIN;record;COMMIT`；证据=现有三明治(`interaction_recording.py:161-224`)+UoW 短事务+SQLite 库级锁+PG 连接池/行锁；第二种否决 |
| E | Worker/Runtime 边界 | 成立：Worker 拥有 pickup/claim/execute/record；RuntimeCoordinator 仅六段、无 DB 访问、无 ownership 能力 |
