# Step 12.9.4.7 — Async Execution Reliability / Recovery Audit

> 只读审计。**不加任何新设施**（不引入 queue / Redis / Celery / lease / heartbeat /
> `processing_started_at` / scheduler / cron / daemon loop）。
> 目标：验证现有四个 primitive 在「BackgroundTasks 丢失 / 未执行」时，是否已足以
> 形成可靠的恢复路径；若不足，定位**具体缺口**供后续最小机制填补，而非现在就上重武器。

---

## 0. 被审计的四个 primitive（当前真实代码）

| primitive | 位置 | 关键行为（精确） |
| --- | --- | --- |
| **DB durability** | `create_pending`（`interaction_recording.py`） | 落 `processing_status='pending'` 的独立短事务，**COMMIT 后才返回** → ACK 前 PENDING 已持久化 |
| **find_pending** | `interaction_record.py:101-107` | `SELECT ... WHERE processing_status='pending' ORDER BY created_at, id LIMIT N` —— **纯读、不 claim、不加锁**；**只返回 PENDING** |
| **CAS claim** | `interaction_record.py:42-50` | 条件化 `UPDATE ... SET processing_status='processing' WHERE id=:id AND processing_status='pending'`；`rowcount==1` 才 True，否则 False |
| **Worker.process** | `worker.py:59-80` | `load → claim(若 False 立即 return，0 次执行) → reconstruct → runtime.execute → record_*` |

`claim_processing` 是**独立短事务且自 commit**（12.9.4.2）：`processing_status` 由
`'pending'` 跃迁到 `'processing'` 后**立即落盘**，之后才跑 `runtime.execute`（~16s）。

---

## 1. 场景 A — 正常路径（预期行为，确认基线）

```
ACK → BackgroundTasks.add_task(worker.process, id)        channels.py:110-113
    → claim（pending→processing，COMMIT）
    → runtime.execute（~16s，事务之外）
    → record_success → 终态 SUCCEEDED×DELIVERED
```

`process()` 返回 `True`，行进入终态。**可靠。** 此路径与同步架构执行结果完全一致
（G3：状态语义仅在 phase API 一处）。

---

## 2. 场景 B — ACK 后 BackgroundTasks 丢失 / 从未执行（**本次核心问题**）

触发条件（例）：
- 进程在「ACK 已返回、但 BackgroundTasks 尚未被调度」之间被 kill；
- 或 BackgroundTasks 内部在 `claim` **之前**崩溃（异常未到 claim）；
- 或进程正常退出、后台任务被丢弃。

此时状态：行**仍是 `pending`**（claim 从未发生）。

恢复路径（由现有四个 primitive 完整构成）：

```
Recovery Worker（未来 daemon loop，复用同一套 primitive）
   find_pending()        → 返回该 pending 行        （interaction_record.py:101）
   → claim_processing(id) → CAS：pending→processing，本调用方=True
   → runtime.execute
   → record_success / record_failure → 终态
```

**结论：对「BackgroundTasks 丢失 / 未执行」这一类故障，四个 primitive 足以形成
可靠的恢复路径。** 依据：

1. **数据不丢**：PENDING 在 ACK 前已 COMMIT（DB durability），崩溃后行仍在库。
2. **可被重新发现**：`find_pending` 只看 `pending`，故未执行行必然被 pickup 看到。
3. **重新执行不致双执行**：`claim_processing` 是 CAS —— 即使 BackgroundTasks 后来
   又"复活"并也来 claim 同一行，也只有一个调用方拿到 `True`，另一个 `False → STOP`
   （见场景 C）。

> ✅ **核心问题答案：是。** 这四个 primitive 在 BackgroundTasks 丢失场景下，已经
> 构成「Durable PENDING + 可重 pickup + CAS 防双执行 + 单次 process」的完整恢复环。

唯一的附带前提：**目前没有 autonomous driver**（daemon loop 在 12.9.4.5 被刻意后置）。
即 primitive *能力*足够，但"谁来周期性调用 find_pending→process"尚不存在。这是
**触发/部署层**缺口，不是正确性缺口；未来 daemon loop 只是这几个 primitive 的循环
调用者，不引入新的执行语义，因此不会带来新的双执行风险。

---

## 3. 场景 C — BackgroundTasks 与 Recovery Worker 同时发现（并发 pickup）

```
BackgroundTasks ─┐
                 ├─ find_pending（都可能读到同一 pending id，重叠读是设计允许）
Recovery Worker ─┘
   ↓ 两者都调用 claim_processing(id)
   ↓ 单条条件化 UPDATE，由 DB 原子串行化
   → 一个 rowcount==1（True，取得执行权）
   → 一个 rowcount==0（False，立即 return，**0 次 runtime.execute**）
```

**只有一个 `True`、另一个 `False`，因此不会双执行。** 这是整个异步架构的
最重要安全阀（G1 在并发下的推广），由 `claim_processing` 的 SQL 原子性保证，
与语言/锁/进程数无关。

要点：
- `find_pending` **允许**重叠返回同一行（它不 claim、不加锁）——这是故意的，唯一性
  交给下游 CAS，而非上游读锁（SQLite 不支持 `FOR UPDATE` / `SKIP LOCKED`）。
- BackgroundTasks 在 ACK 时**已固定 id**（不是"重新发现"），与 Recovery Worker 的
  pickup 走的是同一条 CAS 路径，故二者本质上等价竞争，**CAS 天然去重**。
- 重复 `process(id)` 本身也是安全的：`_load` 拿到行 → `claim_processing` 因行已非
  `pending` 返回 `False` → 不执行（`worker.py:71-72`）。**重入幂等。**

---

## 4. 可靠性保证矩阵（四个 primitive 能 / 不能保证什么）

| 性质 | 是否保证 | 依据 |
| --- | --- | --- |
| ACK 不丢数据（PENDING 已持久化） | ✅ | create_pending 独立 COMMIT |
| 失败重 pickup（pending 必被 find_pending 看到） | ✅ | `WHERE processing_status='pending'` |
| 并发 pickup 不双执行 | ✅ | `claim_processing` CAS，单 rowcount=1 |
| 重入幂等（重复 process 同 id） | ✅ | 非 pending 行 claim=False→STOP |
| 崩溃于 claim **之前** → 可恢复 | ✅ | 行仍是 pending，回到场景 B |
| **崩溃于 claim 之后、outcome 之前 → 可恢复** | ❌ | **见 §5 残留缺口** |
| 自主周期性重 pickup（无需人工） | ❌（需 daemon loop，已后置） | 能力在，触发器不在 |

---

## 5. 残留缺口（四个 primitive 覆盖不到的**唯一**失败模式）

### 5.1 现象：PROCESSING 孤儿

`claim_processing` 成功后，`processing_status` 已被**持久化为 `processing`**
（`interaction_record.py:42-50`，自 commit）。此后：

- 进程在 `runtime.execute`（~16s）期间被 SIGKILL / OOM；或
- `record_success` / `record_failure` 自身抛异常（如 outcome 写库时连接丢失）

→ 行**永久停在 `processing`**。

而 `find_pending`（`interaction_record.py:103`）**只选 `pending`** → PROCESSING
孤儿**永远不会被任何 primitive 重新发现**。直至人工干预或未来的 stale reclaimer。

### 5.2 为什么四个 primitive 内在无法补这个洞

要 CAS 互斥，**必须**先写下一个"我正在处理"的状态（否则并发两者都通过 WHERE
`pending` 的校验）。一旦写了该状态又在中途崩溃，这个状态就成了孤儿。而"识别孤儿"
需要"状态已持续多久"——即一个时间戳，而 `processing_started_at` 正是被本步明确
**推迟**引入的列。

可排查的替代方案（均被否决，确认非 primitive 层面可解）：
- **把 claim+execute+outcome 包进一个事务**：被 12.9.3.4 §D 否决——执行 ~16s 不能
  裹在事务里（SQLite 库级锁 + PG 连接池/行锁双重否决）。
- **claim 不落 `processing`，仅落 outcome 终态**：CAS 必须写点什么来防并发双执行，
  不落非 pending 状态就退化为"先读后判"的非原子双执行来源（12.9.3.2 §3）。

→ **结论：PROCESSING 孤儿是 CAS-claim 设计的固有边界，必须由"带时间戳的 stale
reclaimer"在外部闭合，不在本四个 primitive 之内。**

### 5.3 严重程度与处置

- 每交互暴露窗口 = claim 成功到 outcome 落定的 ~16s 执行期；崩溃恰好落在此窗口的概率
  低，但**累积性**——每次崩溃留下一条永不答复的用户消息，长期会堆积。
- 本步**不修**（遵循"无证据不增设施"）。标志为**生产上线前必须闭合的单一前置项**
  （见 §7 最小机制草图）。

---

## 6. 次要风险（非本次核心问题，但审计顺带指出）

1. **WeCom 5s 超时重推 vs `create_pending` 去重**：若 BackgroundTasks 丢失、行留
   `pending`，WeCom 会在 5s 超时后**重推同 MsgId 的消息**。重推经 `handle_message`
   → `create_pending` 再次 `create`。UNIQUE(channel_id, external_interaction_id)
   的吸收逻辑（10.5.2/10.5.3）是否在 `create_pending` 内部被兜住、还是会把
   IntegrityError 冒泡成 500，**需单独验证**（建议作为下一步小审计）。这不影响"恢复
   路径"正确性，但影响"重推期间 HTTP 应答语义"。
2. **BackgroundTasks 是单进程内机制**：它与主进程同生共死。本步用它作"过渡性
   invocation"是合理妥协（12.9.4.6 已声明），但场景 B 的"可靠恢复"真正成立，依赖
   未来的 daemon loop / 独立进程，而非 BackgroundTasks 本身。
3. **当前无任何 autonomous driver**：如 §2 末所述，primitive 够用但无人周期性触发
   重 pickup。在 daemon loop 落地前，BackgroundTasks 丢失 = 该消息**只能等下次手动
   或未来 daemon 触发**才执行——这是可接受的"暂时状态"（12.9.4.6 已承认 PENDING
   堆积是允许的暂时态），但需写入运维认知。

---

## 7. 最小未来机制草图（**仅规格、不在本步实现**）

用于闭合 §5 的 PROCESSING 孤儿，且**不引入 queue / Redis / lease / heartbeat**：

```
新增列：interactions.processing_started_at  (nullable, default null)
        在 claim_processing 成功时一并写入 now()

新增 Port 方法（最小、显式、非通用读面）：
    reclaim_stale_processing(before: datetime) -> list[int]
        UPDATE ... SET processing_status='pending'
        WHERE processing_status='processing'
          AND processing_started_at < :before
        RETURNING id
        （条件化、有界、单语句；不提供任意字段 update）

daemon loop（未来步）每 N 秒：
    ids = find_pending(limit)           # 正常 pickup
    ids += reclaim_stale_processing(now()-N)   # 孤儿回收
    for id in ids: process(id)          # 每一条仍走 CAS，天然防双执行
```

该机制**完全建立在既有四个 primitive 之上**（reclaim 只是把孤儿"退回 pending 池"，
后续仍由 claim+CAS+process 处理），不新增执行语义、不引入中间件，符合 12.9.3.6
"A 最小方案"精神。

---

## 8. 裁决

**对本次核心问题**——"BackgroundTasks 丢失 / 未执行时，四个 primitive 是否足以形成
可靠的恢复路径"——**答案：是**。Durable PENDING + find_pending（纯读 pickup）+
CAS claim（防双执行）+ 单次 process，构成一个完整、exactly-once 的恢复环；并发 pickup
由 CAS 原子性去重（场景 C）。

**唯一残留缺口**：崩溃发生在 `claim` 成功之后、`outcome` 落定之前的 ~16s 窗口，会留下
PROCESSING 孤儿，而 `find_pending` 对其不可见。该缺口**内在于 CAS-claim 设计**，须由
未来的 stale reclaimer（带 `processing_started_at`）闭合，**不在本四个 primitive 之内**，
且**本步不实现**（遵循无证据不增设施）。

**不建议现在加入** queue / Redis / Celery / lease / heartbeat / scheduler / cron /
daemon loop：它们解决的是"规模 / 多实例 ownership / 自主触发"，而非"恢复正确性"——
恢复正确性已被四个 primitive 证明成立。

**上线前置**：在把 BackgroundTasks 真正替换为 daemon loop、或让生产依赖"异步执行"
之前，必须先闭合 §5 的 PROCESSING 孤儿回收（§7 草图），否则崩溃窗口内会有用户消息
永久无应答。
