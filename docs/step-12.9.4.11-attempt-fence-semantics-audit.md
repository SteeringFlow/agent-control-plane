# Step 12.9.4.11 — Attempt Fence Semantics Audit

> **只读设计审计，不改任何代码。**
> 本步只钉死 12.9.4.10 §B.2 条件 2 的最小围栏机制：attempt fence 的 primitives、
> 调用形态、miss 语义、以及最致命的时间精度一致性。

---

## 0. 结论

| 议题 | 裁决 |
| --- | --- |
| 1. Fence primitive | Worker **必须持有** `processing_started_at`；但**值必须来自 DB 回读**，禁止 app 自己生成 |
| 2. Outcome API | 唯一落点是 `record_outcome` 改为**单条条件 UPDATE**（带 `expected`）；`record_success`/`record_failure` 透传一个 `expected_processing_started_at`；推荐封装成 `ClaimHandle` 避免散落 |
| 3. Fence miss | `rowcount == 0` = `OutcomeRejectedBecauseOwnershipLost` = **正常竞争结果，非 DB 错误，绝不抛** |
| 4. 时间精度 | **三处全部锚定 DB `func.now()`，app 时钟不参与任何比较**；reclaim 传 `threshold` 不传 app 算出的绝对 `before`；碰撞安全由 A.3 阈值纪律（threshold ≫ 1s）+ reclaim 置 NULL 形式化保证 |

---

## 1. Fence primitive 的精确定义

### 1.1 Worker 是否必须持有这个值？—— 是

fence 的等式

```sql
UPDATE ... WHERE id = :id
            AND processing_status = 'processing'
            AND processing_started_at = :expected
```

需要 `:expected` = **当前 attempt 在 claim 那一刻该行持有的 `processing_started_at`**。
Worker 是唯一横跨「claim → execute → record」的调用方，因此**只有它能把这个值带进
`record_outcome`**。所以它必须持有。

### 1.2 值从哪来 —— 必须是「DB 回读」，不是「app 生成」

**正确形态（推荐）**：DB 是唯一时间源。

```
claim_processing()
    ↓  SQL: SET processing_status='processing',
                processing_started_at = func.now()   ← DB 时钟，UTC
claim 返回 True
    ↓  Worker: started_at = uow.interactions.get_by_id(id).processing_started_at  ← 读回 DB 实值
    ↓  （持有 started_at）
execute(...)                                 ← ~16s
    ↓  record_success(id, result, expected=started_at)
    ↓  record_outcome(... WHERE processing_started_at = :started_at)
```

- `claim_processing` **仍返回 `bool`**（12.9.4.10 冻结的 Port 不变）；
- Worker 在 claim 成功后**多一次 `get_by_id`** 把 DB 写入的真实值读回；
- 之后 `record_outcome` 的等式是**「列 vs 列」**：把读回的 DB 值作为 bound param 传回，
  与 DB 列逐字节比较 → **精确、无时钟偏移、无精度问题**。

**为什么禁止 app 自己 `datetime.now()`？** 若 claim 写 `func.now()`（DB 时钟），
而 Worker 用 `datetime.now()`（app 时钟）去构造 `:expected`，则两值可能因
**app/DB 时钟偏移、时区、微秒截断** 而不等 → 一个**合法**（未超阈值）的 attempt
被误判 REJECTED，本应 COMMITTED 却变成 `OutcomeRejectedBecauseOwnershipLost`。
→ 围栏的持有值**只能来自 DB 回读**，app 绝不能生成 claim 时间戳。

### 1.3 备选：让 `claim_processing` 返回 handle（更大改动）

`claim_processing` 返回 `ClaimHandle{interaction_id, started_at}` 而非 `bool`。
Worker 直接持有 handle，透传给 record。

- 优点：封装更干净，raw timestamp 不散落；
- **代价**：改变 `claim_processing` 的返回类型 → 打破冻结的 `-> bool` 契约，
  且 `worker.py:71` 与 `PersistingCoordinator.execute`（`interaction_recording.py:161`）
  的 `if not self.claim_processing(...)` 惯用法都要改 → 更大爆炸半径。

→ 审计推荐 **1.2（读回）** 作为最小路径；1.3 作为「愿接受 Port 变更换更好封装」的备选。

---

## 2. Outcome API 的最小变化

### 2.1 唯一落点：`record_outcome` 必须变成单条条件 UPDATE

当前 `record_outcome`（`interaction_record.py:64-74`）是：

```python
row = self._require(interaction_id)   # 读
row.processing_status = ...            # 改
self._session.flush()                  # 写
```

**绝不能把 fence 叠在这套「读-改-写」上** —— 那正是 12.9.3.2 §3 指出的双执行来源
（非原子读-改-写）。fence 必须与 `claim_processing` 同构：**单条条件化 UPDATE，
靠 `rowcount` 判定成败**。

```python
result = self._session.execute(
    update(InteractionModel)
    .where(
        InteractionModel.id == interaction_id,
        InteractionModel.processing_status == PROCESSING_PROCESSING,  # 只转 processing 行
        InteractionModel.processing_started_at == expected,           # ← fence
    )
    .values(processing_status=..., delivery_status=..., ...)
)
return result.rowcount == 1
```

`processing_status='processing'` 同时作为 WHERE 条件，保留了既有不变量
「只有 processing 行能被转成终态」——终态行（started_at 已 NULL/变更）自然得到 0 → REJECTED。

### 2.2 两层透传：避免把 timestamp 到处散开

`record_outcome` 是等式的唯一落点；要把 `expected_processing_started_at` 透传上去，
两条路：

**(a) 显式参数（最小 diff）：**

```python
record_success(id, result, *, expected_processing_started_at)
record_failure(id, inbound, exc, *, expected_processing_started_at)
```

- 优点：改动最小，只在 `worker.py` 两个调用点 + `PersistingCoordinator` 两个方法 + repo 一处；
- 缺点：raw timestamp 在 Worker 与 coordinator 调用点各出现一次（「散开」）。

**(b) 封装成 `ClaimHandle`（符合你「避免散开」的倾向）：**

```python
@dataclass(frozen=True)
class ClaimHandle:
    interaction_id: int
    started_at: datetime

record_success(handle: ClaimHandle, result)
record_failure(handle: ClaimHandle, inbound, exc)
```

- Worker 在 claim 成功后构造 `handle = ClaimHandle(id, get_by_id(id).started_at)`，
  之后只传 `handle`；`expected` 在 repo 层从 `handle.started_at` 取出；
- 字段只在一处（handle）出现，调用点干净；
- 代价：两个 record 方法签名从 `(id, ...)` 变 `(handle, ...)`（仍属 Port 变更）。

→ 审计推荐 **(b)**（契合「避免散开」），但提示它与 1.3 一样需要一次 Port 签名裁决；
若想零签名变更，用 **(a)**。`record_outcome` 的等式才是真正要写对的地方，两种传参只是形态。

### 2.3 同步路径也要读回并透传

`PersistingCoordinator.execute`（`interaction_recording.py:146-171`）现在同步走
create→claim→execute→outcome。它也要在 claim 后 `get_by_id` 读回 `started_at` 并透传，
否则出现两条不一致路径。在尚无 daemon 时同步路径永远不会被 reclaim，fence 恒过，
但为统一**只留一条写入路径**，必须一并接上。

---

## 3. Fence miss 的语义

### 3.1 `rowcount == 0` 是什么

它**只**表示：当前 `id` 的行，要么已被 reclaim 改了 `started_at`/置 NULL、要么已落终态、
要么已被删（不应发生）—— 总之**「本次 attempt 已失去 commit authority」**。

→ 定义枚举（非异常）：

```python
class OutcomeCommit:
    COMMITTED = "committed"        # rowcount == 1：正常落终态
    REJECTED_STALE = "rejected"    # rowcount == 0：attempt 失去 commit authority
```

### 3.2 它是正常竞争结果，**不是 DB 错误，绝不抛**

- `REJECTED_STALE` **不代表** DB 故障（连接断开、约束冲突才是真异常，应该照常抛）。
- 它的语义是「旧 attempt 已经失去 commit authority」——一个**良性、预期的**并发结局：
  慢 Worker A 被 reclaim（PROCESSING→PENDING→B claim 再执行）后，A 晚到的 outcome
  落空是正常的，不是 bug。
- **若此处抛异常**：12.9.4.10 §C.3 描述的「A 执行 > 阈值被回收 → `record_success`
  抛错 → `process()` 把异常当失败冒泡」就会发生，反而污染或触发重跑循环。
- 因此 `record_outcome` 返回枚举；`REJECTED_STALE` 由 Worker **静默忽略**（debug 级日志）、
  `process()` 仍返回 `True`（它确实执行完了，只是 outcome 落在别处）。

### 3.3 与「row 不存在」的关系

`_require` 当前对缺失行抛 `ValueError`。改为条件 UPDATE 后，行不存在也归为 `rowcount==0`
→ 同样是 `REJECTED_STALE`（本应由 Worker 已 load 的行，不会缺失；若缺失即视为失权）。
无需再区分「row 缺失」与「被 reclaim」—— **失权即 REJECTED**。

---

## 4. 时间精度（本步最致命处）

### 4.1 三处时间语义必须完全一致

| 操作 | 时间来源 | 一致性 |
| --- | --- | --- |
| claim 写入 | `func.now()`（**DB 时钟，UTC**） | ✅ DB |
| outcome 等式 | 读回的 `processing_started_at` 原值，作为 bound param 回比 | ✅ DB 实值 |
| reclaim 比较 | `func.now() - threshold`（**DB 时钟，UTC**） | ✅ DB |

**铁律：三处全部锚定 DB `func.now()`；app 时钟不参与任何比较。**

- app **不生成** claim 时间戳（1.2）；
- outcome 等式是「列 vs 读回列」，**无时区换算、无截断**；
- reclaim **不传 app 算出的绝对 `before`**，见 4.3。

→ 只要遵守铁律，app/DB 时钟哪怕偏移几分钟，fence 内部仍自洽（只影响"多久算 stale"
的**绝对准度**，不影响 fence 正确性）。

### 4.2 列定义纪律

```python
processing_started_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True)   # 无 server_default、无 onupdate
```

与 `created_at`/`updated_at` 同 `DateTime(timezone=True)`，但**无 `onupdate`**
（D.2 已定：只被 claim 写 now、被 reclaim 写 NULL，outcome 不碰）。

### 4.3 reclaim 的 `before` 必须 DB 内计算

12.9.4.10 候选签名是 `reclaim_stale_processing(before: datetime)`。**若 `before` 是
app 端 `datetime.now() - threshold` 算出的绝对时间**，则 app/DB 时钟偏移会直接污染阈值
判定 → 错误回收或漏回收。

→ **修正签名**：

```python
reclaim_stale_processing(threshold_seconds: int, limit: int = 100) -> int
# 内部：WHERE processing_status='processing'
#           AND processing_started_at < func.now() - make_interval(threshold_seconds)
```

`threshold` 作为**间隔参数**传入，由 DB 计算 `now - threshold`。SQLite 下用
`datetime('now', :minus)`（`minus = f"-{n} seconds"`）而非 Python 减法。
这样 reclaim 与 claim 用同一 DB 时钟，绝对一致。

### 4.4 碰撞安全性的形式化钉死

SQLite `CURRENT_TIMESTAMP` = `'YYYY-MM-DD HH:MM:SS'`（**秒级**）。两个 attempt 的
`processing_started_at` 只有落在**同一秒**才可能字节相等。

```
Claim(A)  : started_at = T_A      (func.now())
Reclaim   : started_at = NULL      (只在 now - T_A > threshold 后触发)
Claim(B)  : started_at = T_B      (func.now())
```

- Reclaim **严格位于** Claim(A) 与 Claim(B) 之间，且把列置 NULL；
- 故 `|T_B - T_A| >= threshold`（reclaim 不可能在阈值前触发）；
- **若 `threshold >= 1s`，则 T_A 与 T_B 落在不同秒 → 字符串不等 → fence 安全**；
- A.3 已强制 `threshold ≫ 执行上界`（默认 ≈ 40s）⊃ `threshold >= 1s`。

→ **fence 在 SQLite 秒级精度下的碰撞安全性，由「reclaim 必置 NULL」+「A.3 阈值纪律」
双重保证**，与秒级精度的余量无关。即便 `threshold` 被错配成 <1s，reclaim 仍会先把列
置 NULL，A 的 `expected=T_A` 也绝不会等于 B 的新值（除非 B 与 A 同秒，恰是
`threshold<1s` 的反例）—— 因此 fence 正确性的**硬下限是 `threshold >= 1s`**，
已被 A.3 更强约束覆盖。

### 4.5 时区纪律

- 列用 `timezone=True`，`func.now()` 落 UTC；
- **永不**用朴素本地时 `datetime.now()` 生成任何 fence 参与值；
- 读回值原样作为 bound param 回传（SQLAlchemy 保证类型/时区保真），**不做任何手动转换**。

---

## 5. 最小落地清单（未来实现步，本步不做）

1. 加列 `interactions.processing_started_at`（`DateTime(timezone=True)`, nullable, 无 server_default/onupdate）；
2. `claim_processing` 的 UPDATE 增加 `processing_started_at = func.now()`（同语句原子）；
3. `record_outcome` 由「读-改-写」改为**单条条件 UPDATE**（`WHERE id AND processing_status='processing' AND processing_started_at=:expected`），返回 `OutcomeCommit` 枚举；
4. `record_success`/`record_failure`（及同步 `execute`）增加 `expected_processing_started_at`（或 `ClaimHandle`）参数并透传；
5. `Worker.process` 在 `claim_processing` 成功后 `get_by_id` 读回 `started_at` 并持有；
6. `reclaim_stale_processing(threshold_seconds, limit)`：DB 内算 `now - threshold`，置 `status=pending` + `started_at=NULL`；
7. `OutcomeCommit.REJECTED_STALE` 在 Worker 层**静默忽略，绝不抛**；
8. 测试：fence 命中才落终态 / fence 失配被拒 / reclaim 后旧 attempt 的 outcome 被拒 /
   合法（未超阈值）attempt 必 COMMITTED / 同步路径与异步路径 fence 行为一致。
