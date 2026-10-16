# Step 12.9.4.10 — PROCESSING Orphan Recovery Design Audit

> **只读设计审计，不改任何代码。**
> 本步只回答：**「如果未来有一个 recovery trigger，它最小需要什么 DB primitive
> 才能安全恢复孤儿？」**
> 不回答「谁每隔 30 秒调用它」——daemon / scheduler / cron 继续冻结。

---

## 0. 先给结论

| 议题 | 裁决 |
| --- | --- |
| A. stale 定义 | `processing='processing'` **AND** `processing_started_at IS NOT NULL` **AND** `now - processing_started_at > threshold`；**禁用 `updated_at`** |
| B. 状态转换 | ✅ 采用 `PROCESSING(stale) → PENDING → find_pending → claim → process`（复用已验证 primitive），**但必须同时满足两个附加条件**（§B.2） |
| C. reclaim primitive | `reclaim_stale_processing(before, limit) -> int`：批处理、**有界**、**只碰 PROCESSING**、条件化 UPDATE（原子），**返回 count 即可** |
| C 最关键问题 | **会。** TTL 超过阈值仍存活的 Worker **会被错误回收** —— CAS 救不了，只有「阈值 > 执行上界」能救 |
| D. 写入边界 | `claim_processing` 的**同一条 UPDATE** 里与 status 一起原子写入；reclaim 置 `NULL`；**永不 `onupdate`** |
| E. 崩溃窗口 | 6 个点，其中 4 个留 `processing` 孤儿 → 全部可由同一 reclaim 覆盖 |
| F. 冻结 | scheduler / cron / daemon / queue / Redis / Celery / heartbeat / lease 全部不引入 |

---

## A. stale 的精确定义

### A.1 判定谓词

```sql
processing_status = 'processing'
AND processing_started_at IS NOT NULL
AND processing_started_at < (now - threshold)
```

三个条件缺一不可：

- `processing_status='processing'` —— 只回收在飞行的，绝不碰终态；
- `processing_started_at IS NOT NULL` —— 没有 claim 时间的行（历史遗留 / 未来其他
  写入路径）**不参与** TTL 判定（Fail Closed：不认识就不回收）；
- 时间条件 —— 唯一能区分「慢」与「死」的信号（见 A.3）。

### A.2 为什么**绝不使用 `updated_at`**

`interactions.updated_at` 的定义是 `server_default=func.now(), onupdate=func.now()`
（`models/interaction.py:72-74`）——「本行最后一次被修改」。它有四个致命问题：

1. **没有单一语义所有者**：任何一次 UPDATE 都会刷新它。它会因与执行存活
   **完全无关**的写入而更新，从而把一个已死的行伪装成存活。
2. **不可清零**：回收后需要表达「当前没有在飞行的 attempt」，`updated_at` 无法
   置 NULL；它只会越滚越新，无法表达「无」。
3. **诱导心跳**：用它做 TTL 会自然演化出「执行中定期写点什么来续命」——
   那正是本步明确冻结的 heartbeat/lease。
4. **语义会静默漂移**：一旦未来出现任何「执行途中的行写入」（例如回填
   `session_id`），`updated_at` 就**在没有任何 schema 变更的情况下**变成了存活信号。
   TTL 的判定基础被悄悄改写，而无人知晓。

→ `updated_at` 是「行维护时间戳」，不是「执行存活时间戳」。二者不可互替。

### A.3 `slow ≠ dead` 如何裁决（阈值纪律）

既然冻结了 heartbeat/lease，就**没有任何手段证明一个 Worker 还活着**。
于是唯一可用的判据是：**执行是否有界**。

已确认**有界**（真实代码，非假设）：

| 阶段 | 上界 | 证据 |
| --- | --- | --- |
| Agent dispatch | `connection.config["timeout_s"]`，默认 `30.0` | `adapters/agents/acp.py:74` `DEFAULT_TIMEOUT_S = 30.0`；`:374` 读取；`:482` `deadline = time.monotonic() + timeout_s`，超时抛 `AgentDispatchFailedError` |
| Delivery（含 token 获取） | `channel.config["timeout_s"]`，默认 `10.0` | `adapters/channels/wecom.py:119` `DEFAULT_OUTBOUND_TIMEOUT_S = 10.0`；`:598 / :674 / :701` 均 `urlopen(..., timeout=...)` |

⇒ 单次执行上界 ≈ `agent_timeout + delivery_timeout`（默认 ≈ 40s，配置可变）。

**阈值纪律（硬性）**：

```
threshold  >  max_plausible(agent_timeout) + max_plausible(delivery_timeout) + margin
```

- 必须由**实际可配置的上界**推导，不能是凭空常数；
- 因两个 timeout 都可由配置改变，阈值须按**最大合理配置**取，或干脆取数分钟级；
- 代价必须写明：**无 heartbeat ⇒ 孤儿检测延迟 = threshold 量级（分钟级）**。
  这是「不加 heartbeat」的直接代价，是显式取舍，不是疏漏。

---

## B. 状态转换

### B.1 采用候选路径

```
PROCESSING(stale)
      ↓  reclaim_stale_processing(before, limit)
   PENDING   （processing_started_at → NULL）
      ↓  find_pending()          ← 既有 primitive
      ↓  claim_processing()      ← 既有 primitive（CAS）
   PROCESSING（新的 started_at）
      ↓  Worker.process()        ← 既有 primitive
   终态
```

优点正是你说的：完全复用 `find_pending` + `claim_processing` + `Worker.process`
三个已验证 primitive，recovery 自己**不执行任何东西**。

而且因为 reclaim 只是把行「放回 pending 池」，调用方**不需要知道被回收了哪些 id** ——
下一轮 `find_pending` 自然会捡起。这直接决定了 C 里可以只返回 count。

### B.2 但这条路径有两个必须先满足的条件（**本次审计最重要的发现**）

**条件 1：阈值必须 > 执行上界（A.3）。**

否则：Worker A 仍在执行 → 被回收 → Worker B claim 成功并**再次执行 Agent**
→ **两次 Agent 执行 + 两次业务副作用**。这正是 12.9.3.x 花整章消除的双执行。

**条件 2：`record_outcome` 目前是无条件写，必须加围栏。**

```python
# persistence/repositories/interaction_record.py:64-74
row = self._require(interaction_id)        # ← 只校验「存在」
row.processing_status = processing_status  # ← 无任何状态守卫
```

`_require` 只校验行存在，**不校验当前状态**。因此：

- A 被回收后，B 已 claim（行又是 `processing`）；
- A 执行完照样调用 `record_success` → **无条件覆盖** B 的在飞行状态；
- B 再写完 → 两条 outcome 互相践踏，Evidence 被污染。

**最小围栏（不新增列）**：复用即将加入的 `processing_started_at` 本身作为
attempt 身份（fence）：

```
claim    : SET processing_status='processing', processing_started_at = now
Worker   : 记住本次 claim 到的 started_at（claim 时返回或随后读回）
reclaim  : SET processing_status='pending',    processing_started_at = NULL
outcome  : UPDATE ... WHERE id=? AND processing_status='processing'
                       AND processing_started_at = :expected
```

- A 的 `expected` 是旧值；回收后行要么是 `pending/NULL`、要么是新值 → **A 的写必然
  rowcount=0，被拒绝**；
- 碰撞安全性：两个 attempt 的 `started_at` 至少相差 `threshold`（≫1s），
  即使 SQLite 秒级精度也不会撞值；
- 代价：`record_outcome` 增加一个可选参数（Port 面**扩张一个参数**，不是新方法）——
  需单独裁决，但这是 Evidence 完整性（「Evidence = 事实」）的最低要求。

> 注意：围栏**不阻止**双执行（B 已经跑了），它只阻止僵尸 Worker 污染 Evidence。
> 阻止双执行**只能靠阈值**（条件 1）。两者都要。

---

## C. `reclaim_stale_processing` 的设计审计

### C.1 候选签名

```python
def reclaim_stale_processing(self, before: datetime, limit: int = 100) -> int:
```

返回 **受影响行数**（不是 id 列表）——因为 B.1 路径下调用方无需知道是哪些行。

### C.2 逐项裁决

| 问题 | 裁决 | 理由 |
| --- | --- | --- |
| 需要一次回收多行？ | ✅ 需要批处理 | 崩溃可能留下成批孤儿；逐行往返不可接受 |
| 必须**有界**？ | ✅ 必须带 `limit` | 与 `find_pending(limit)` 同一纪律（12.9.4.4）：写操作也必须是短事务；无界 UPDATE 会一次放洪水般的行回队列 |
| 只修改 PROCESSING？ | ✅ 绝对 | `WHERE processing_status='processing'` —— 绝不能让 `succeeded`/`failed` 复活 |
| 需要 CAS？ | ✅ 但**是条件化 UPDATE，不是读-改-写** | 单条 `UPDATE ... WHERE status='processing' AND started_at < :before`，由 DB 原子执行，与 `claim_processing` 同构；rowcount 即结果 |
| CAS 能防止活 Worker 被回收？ | ❌ **不能** —— 见 C.3 | CAS 只防「与刚写完终态的 Worker 竞争」，**不防慢** |

### C.3 「Worker 执行超过 TTL 会不会被错误回收」——**会**

这是本步最关键的问题，答案是肯定的，且必须分清两层：

**CAS 能防的**：回收语句执行时，若该行恰好刚被 Worker 写成终态（`succeeded`/
`failed`），`WHERE processing_status='processing'` 不成立 → 该行不被回收。✅

**CAS 防不了的**：Worker 仍**在飞行**（`processing`，只是慢）→ 满足全部三个 stale
条件 → **被回收** → 另一个 Worker 会 claim 并执行第二次。❌

→ **「不错误回收活 Worker」这件事，100% 由阈值承担（A.3），与 CAS 无关。**
这必须写进 Port docstring，否则后来者会误以为 CAS 已兜底。

**残余风险（即使阈值正确）**：A 在 `T + 上界-ε` 超时、正准备写 `record_failure`，
reclaim 在 `T + 上界+ε` 触发 → 仍会短暂双执行。窗口极小但非零，
由 B.2 条件 2 的围栏把危害限制在「不做 Evidence 污染」，且不产生错误终态。

---

## D. `processing_started_at` 的写入边界

### D.1 唯一正确形式

```
claim_processing()
   ↓  同一条 UPDATE（原子，不可分）
   SET processing_status = 'processing',
       processing_started_at = <now>
   ↓  COMMIT（claim 的既有短事务，不变）
```

- **必须与 status 在同一条语句里**：分成两条语句会留下「已 processing 但无
  started_at」的中间态 → 按 A.1 该行因 `IS NOT NULL` 不成立而**永远不可回收**，
  等于把孤儿又造了一遍；
- 沿用 claim 已有的独立短事务与 COMMIT（`interaction_recording.py:203-212`），
  不新增事务边界；
- `reclaim` 时与 `status='pending'` 一起置 **`NULL`**（显式「当前无在飞行 attempt」）。

### D.2 字段语义（必须写死）

> **`processing_started_at` = 本次 execution claim 的开始时间。**

- ❌ 不是「Interaction 最近更新时间」（那是 `updated_at`）；
- ❌ **不得**带 `onupdate` —— 任何后续写入都不许碰它；
- ❌ outcome（`record_success` / `record_failure`）**不写**它；
- ✅ 只被两个操作写：`claim_processing`（置 now）与 `reclaim`（置 NULL）。

理由：E 节会看到，崩溃在 outcome 事务内时行会**回退到 `processing`**，
此时 `started_at` 仍是**原始 claim 时间** → 天然满足 stale → 可被回收。
若 outcome 去刷新它，孤儿就会被续命、永远检测不到。

---

## E. 崩溃窗口全表

claim 与 outcome 是**两个独立短事务**（12.9.4.2），执行在其外。逐点：

| # | 崩溃点 | 落盘状态 | 可恢复？ | 依据 |
| --- | --- | --- | --- | --- |
| 1 | claim UPDATE 后、COMMIT 前 | `pending` | ✅ **无需恢复** | 事务回滚，`find_pending` 正常捡起 |
| 2 | claim COMMIT 后、执行尚未开始 | **`processing`（孤儿）** | ✅ reclaim | `started_at` 已随 claim 落盘 ← **12.9.4.7 §5 的主来源** |
| 3 | `runtime.execute` 期间（~16s） | **`processing`（孤儿）** | ✅ reclaim | 同上 |
| 4 | execute 抛异常后、`record_failure` 前 | `processing` | ✅ reclaim（通常 `record_failure` 会成功） | 异常路径也能崩（如写库失败） |
| 5 | outcome UPDATE 后、COMMIT 前 | **`processing`（回滚）** | ✅ reclaim | 见 D.2：`started_at` 仍为原始 claim 时间 → 必然 stale |
| 6 | outcome COMMIT 后 | 终态 | ✅ 无需恢复 | — |

**要点**：
- 真正产生永久孤儿的是 **2 / 3 / 4 / 5** 四个点，全部落在 `processing`；
- 它们都由**同一个** `reclaim_stale_processing` 覆盖，不需要按崩溃点分别处理；
- 第 5 点尤其说明：不能因为「已经走到 outcome」就认为安全 —— outcome 自己是一个
  独立事务，崩在其中会退回 `processing`。

---

## F. 继续冻结（本步与下一步都不引入）

```
❌ scheduler   ❌ cron        ❌ daemon loop   ❌ queue
❌ Redis       ❌ Celery      ❌ heartbeat     ❌ lease
```

本步产出的**只是一个 DB primitive + 阈值纪律 + 写入边界**。
「谁、以什么频率调用它」属**触发层**决策，明确后置。

因此也应接受由此产生的显式代价：**孤儿检测延迟 ≈ threshold（分钟级）**，
且**无法证明 Worker 存活**（只能靠「执行有界」反推）。

---

## G. 最小落地清单（未来实现步，本步不做）

1. 加列 `interactions.processing_started_at`（nullable DateTime，**无 `onupdate`**）；
   —— 这是 12.9.3.2 之后第二个真实 schema 演进，需触发 Alembic 条件讨论。
2. `claim_processing` 的 UPDATE 增加 `processing_started_at = now`（同语句原子）；
3. 新增 Port 方法 `reclaim_stale_processing(before, limit=100) -> int`
   （条件化、有界、只碰 processing、置 status=pending + started_at=NULL）；
4. 【需单独裁决】`record_outcome` 增加 `expected_started_at` 围栏（B.2 条件 2）；
5. 阈值常量按 A.3 从真实 timeout 上界推导，写入配置并注明「不得小于执行上界」；
6. 测试：stale 才回收 / 未 stale 不回收 / 终态绝不回收 / limit 生效 /
   回收后可被 find_pending 捡起 / 旧 Worker 的 outcome 被围栏拒绝。
