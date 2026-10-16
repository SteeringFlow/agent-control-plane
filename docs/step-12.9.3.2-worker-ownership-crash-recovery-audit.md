# Step 12.9.3.2 — Worker Ownership / Crash Recovery Audit

> 只读审计（承接 12.9.1 ACK 边界、12.9.2 duplicate ACK、12.9.3.1 record 充分性）。
> **不改生产代码、不引入 lease/queue/Job 实体。** 逐条回答 8 个具体问题。
>
> 事实面：`interaction_recording.py:152`（`_start` 两事务）、
> `persistence/repositories/interaction_record.py:30`（`mark_processing` 实现）、
> `persistence/unit_of_work.py`（事务边界）、`interaction.py`（ORM 列）、
> 全代码 claim/lease/owner 检索（仅命中 `interaction_recording.py:47` 的「不做」声明）、
> 8-table schema（users / channels / connections / identities / agents / sessions /
> interactions / activities）。

---

## 0. 先行裁定（浓缩）

当前**没有任何 ownership / claim / lease 机制**（全代码检索仅命中
`interaction_recording.py:47` 的「不做 Idempotency/Retry/Replay/Outbox/Delivery
queue/lease」声明）。PENDING→PROCESSING 是一句**无条件 `UPDATE`**，既非原子 claim、
也不携带 owner/时间戳。因此：**今天没有 Worker，也没有崩溃恢复问题**；但在「Worker
模型」被实际构建之前，必须补两类最小事实——(1) 一个**条件化 UPDATE**（用现有
`processing_status` 列即可，**无需改 schema**）防双执行；(2) 一个**声明式
claim 时间戳列**（需最小 schema 增量）支撑 TTL 孤儿回收。两者都落在 `interactions`
现有行上，**不引入 Job/Worker/Execution 新实体**。

---

## 1. PENDING → PROCESSING 现在在哪里发生？

`PersistingCoordinator._start`（`interaction_recording.py:152`）的**第二个事务**：

```python
with self._uow_factory() as uow:            # 事务①：create PENDING + received Evidence
    record = uow.interactions.create(...)
    uow.commit()
with self._uow_factory() as uow:            # 事务②：mark_processing
    uow.interactions.mark_processing(interaction_id=record.id)   # ← 此处
    uow.commit()
```

落到 `SqlAlchemyInteractionRecordRepository.mark_processing`
（`persistence/repositories/interaction_record.py:30`）：

```python
row = self._require(interaction_id)         # SELECT ... WHERE id=X
row.processing_status = "processing"         # 无条件赋值
self._session.flush()
```

→ **就是一条 `UPDATE interactions SET processing_status='processing' WHERE id=:id`，
不检查当前 state、不返回 rowcount、不写任何 owner/timestamp。**

---

## 2. 能否形成真正的 atomic claim？

**当前代码：不能。** `mark_processing` 是无条件赋值，多个调用方都会成功。

**但当前 8-table schema：能表达，且不需改 schema。** 真正的 CAS claim 用现有
`processing_status` 列即可实现（只是今天的 repository 没这么做）：

```sql
UPDATE interactions
   SET processing_status = 'processing'
 WHERE id = :id AND processing_status = 'pending';   -- 单语句条件更新
-- 仅一个并发方 rowcount=1（claim 成功），其余 rowcount=0（放弃）
```

配合 SQLAlchemy 的 `update(...).where(...).values(...)` + `.rowcount` 即为进程级/
事务级原子 claim（DB 行锁保证单语句原子性）。**结论：原子 claim 是代码/repository
改动，不是 schema 改动。**

---

## 3. 两个 Worker 同时 claim 时会发生什么？

**今天（无任何保护）会发生双执行（double execution）。** 推演：

1. 两个 Worker 都从某 PENDING 行被调度（例如都 `find_by_channel_and_external_id`
   命中同一行，或都 poll 到同一个 PENDING id）；
2. 各自调用 `mark_processing` → 两条无条件 `UPDATE` **都成功**（id 相同、幂等赋值）；
3. 两个 Worker 各自拿着同一个 `interaction_id` 进入 `inner.execute(inbound)`；
4. → **Agent 被执行两次、Delivery 触发两次 → 业务副作用重复。**

这正是 10.5.2 / UNIQUE 在 create 层挡住的东西，但在「Worker 按 status 拾取已存在行」
的模型下，UNIQUE（只防同 MsgId 的第二次 create）**不再提供保护**——行已存在，
竞争发生在 `mark_processing` 这一步，而它非原子。

**若采用 §2 的 CAS claim**：仅一个 Worker 的 `WHERE processing_status='pending'`
UPDATE 命中（rowcount=1），其余 rowcount=0 → 立即放弃 → **恰好一次执行**。这正是
引入 Worker 前的硬前置条件。

---

## 4. 当前 8-table schema 能否表达 execution ownership？

**不能。** 8 张表（users / channels / connections / identities / agents / sessions /
interactions / activities）中，`interactions` 的列（`interaction.py`）为：id /
channel_id / session_id / direction / external_interaction_id / request_payload /
response_payload / processing_status / delivery_status / failure_stage /
failure_code / retryable / created_at / updated_at。**没有任何 owner / worker_id /
claimed_by / lease_token / claimed_at / 心跳列。** ownership 事实在 schema 层完全缺失。

---

## 5. Worker crash 后 PROCESSING 是否可被可靠识别为 orphaned？

**不能可靠识别。** 一行停在 `processing` 可能是 (a) Worker 仍活着在执行，也可能是
(b) Worker 已崩溃、永无 `record_outcome`。当前 schema 无任何事实能区分二者：

- `processing_status='processing'` 只表达「执行中」，不含「被谁持有 / 从何时开始」；
- 唯一的时序信号 `updated_at` 是**通用列**（任何写都刷新，含最终 `record_outcome`），
  用它做 `updated_at < now()-TTL` 的回收启发式会**误杀慢但存活的执行**（>TTL 的长任务
  被错判为孤儿 → 二次 claim → 又回到双执行）。`updated_at` 语义与 claim 语义混用，
  不可靠。

**更根本的限制**：即便加了时间戳，也无法在原理上 100% 区分「崩溃」与「只是慢」——
除非 Worker 持续续租（heartbeat/lease），而续租机制本身又是一个 schema + 代码增量。
所以「可靠识别孤儿」在原理上只能做到 **TTL 概率性回收**，做不到确定性判定。这与
10.5.3 §6 的「PROCESSING 不是锁 / 卡死阴影无恢复路径（E3）」完全一致。

---

## 6. 如果不能，最小缺失事实是什么？

分两层，对应两层不同的「最小性」：

**(a) 防双执行（safety）——零 schema 增量。** 仅靠 §2 的**条件化 UPDATE**
（`WHERE processing_status='pending'` + rowcount 检查）即可，复用现有
`processing_status` 列。这是 repository/代码改动，不需要新列。

**(b) 支撑 TTL 孤儿回收（recovery）——最小 schema 增量 = 1 个时间戳列。**
需要一个**声明式 claim 时间戳**，在 CAS claim 成功时原子写入：

- `processing_started_at`（`DateTime`, nullable）—— claim 成功时设为 `now()`。
  回收扫描 = `WHERE processing_status='processing' AND processing_started_at < now()-TTL`。

可选（非 safety 必需，仅用于归属/可观测）：

- `claimed_by`（`String`/UUID, nullable）—— 持有该行的 Worker 身份。仅当「只有持有
  者才能 finalize」或需要排查时才需要；纯防双执行 + TTL 回收**不需要它**。

→ **最小缺失事实 = 1 个 nullable `processing_started_at` 列**（可选 +1 个
`claimed_by`）。它同时可被 CAS 语句在单条 `UPDATE` 中赋值，保持原子性。

---

## 7. 这个缺失是否真的足以证明需要改 schema？

**要分清楚「证明」的触发条件——对今天不证明，对未来 Worker 是前置条件。**

- **对 today 的代码**：没有 Worker、没有并发 claim、没有崩溃，因此不存在该缺陷的
  运行实例。「需要改 schema」在今天**不被证明**。仓促 ADD COLUMN 属 pre-emptive
  migration，违反 12.8.6「记录、不混入运行模型」与 10.5.3「真实并发证据出现前不引入
  lease/queue」的纪律。
- **对「构建 Worker」这一未来动作**：一旦你真正引入按 status 拾取的 Worker 池，§3 的
  双执行与 §5 的孤儿不可识别就变成**真实缺陷**，此时 `processing_started_at`（+可选
  `claimed_by`）从「可选项」升格为**必需前置**。换言之：schema 增量是「Worker 存在」
  的必要条件，而不是「修今天 bug」的必要条件。

**审计建议**：把 `processing_started_at`（+可选 `claimed_by`）列为「Worker 运行模型
就绪清单」的 **schema 前置项**，与 12.9.1 的 async 边界、12.9.2 的 lookup-before-
execute 一并归入「下一阶段运行模型裁决」，现在只记录、不迁移。

---

## 8. 如果需要 schema，是否只需要最小字段，而不是 Job/Worker/Execution 新实体？

**是——只需最小字段，绝不引入新实体。** 论据：

1. **ownership 是现有 `interactions` 行的固有属性**，不是独立概念。10.5.2 已把
   「一次交互 = 一个 MsgId 的一次执行单元」物化为 `UNIQUE(channel_id,
   external_interaction_id)`；ownership/claim 只描述「这一行当前由谁、从何时在执行」，
   天然是行的属性，挂在行上最贴合模型。
2. **新建 Job/Worker/Execution 实体是过度设计**：
   - Job：与 `interactions` 行 1:1 冗余（每行就是一次执行任务）；
   - Worker 注册表：多_worker 协作才需要，单进程/少量 worker 下无需；
   - Execution 实体：把「一次执行尝试」与「interaction 终态」割裂，反而破坏
     10.5.2「行即执行单元」的清晰语义。
3. **最小增量 = `interactions` 加 1~2 个 nullable 列**：
   - `processing_started_at` DateTime NULL（必需，TTL 回收）；
   - `claimed_by` String NULL（可选，归属/可观测）。
   迁移零风险：ADD COLUMN ... NULL，存量行 = 未 claim = 与今天语义一致；TTL 扫描
   自动忽略 NULL。8-table schema 仍是 8-table。

---

## 9. 结论速查表

| 问题 | 答案 |
| --- | --- |
| PENDING→PROCESSING 在哪 | `PersistingCoordinator._start` 事务② → `mark_processing` 无条件 `UPDATE`（`interaction_recording.py:179` / `persistence repo:30`） |
| 能否原子 claim | 当前**不能**（非条件更新）；但**现有 schema 能**用 `WHERE processing_status='pending'` + rowcount 表达，无需改 schema |
| 两 Worker 同 claim | 当前**双执行**（都成功 UPDATE → 都跑 `inner.execute`）；CAS 下仅一个 rowcount=1 |
| schema 能否表达 ownership | **不能**（8 表无 owner/lease/时间戳列） |
| 崩溃后能否可靠识别孤儿 | **不能**（无 owner/时间戳；`updated_at` 是通用列，TTL 误杀存活执行） |
| 最小缺失事实 | (a) 防双执行=条件 UPDATE（零 schema）；(b) 回收=1 个 `processing_started_at`（可选 +`claimed_by`） |
| 是否证明需改 schema | 对 today **不证明**（无 Worker）；对未来 Worker 是**前置条件**，归入运行模型裁决，现在只记录 |
| 是否只需最小字段 | **是**——`interactions` 加 1~2 个 nullable 列，**不引入** Job/Worker/Execution 实体 |
