# Step 12.9.4.13 — Async Execution End-to-End Closure Audit

> **只读闭环验证，不改任何代码。**
> 目标不是再设计新东西，而是确认 12.9.4.12 落地后的全部 primitive 是否真的能
> 拼成 10 个端到端场景的完整闭环。每个场景都给出：参与 primitive、状态迁移链、
> 代码落点、对应测试证据、以及是否闭合。

---

## 0. 结论速览

| # | 场景 | 闭合？ | 依赖的 primitive |
| --- | --- | --- | --- |
| 1 | Normal | ✅ | P1 create → P2 claim → P6 execute → P3 outcome |
| 2 | Agent failure | ✅ | P2 → P6(execute 抛 AgentDispatchFailedError) → P3(record_failure) |
| 3 | Delivery failure | ✅ | P2 → P6(execute 抛 ChannelDeliveryFailedError) → P3(record_failure) |
| 4 | Duplicate | ✅ | P7 回查命中 → 返回既有 id（不 create）；P2 的 CAS 兜底恰好一次执行 |
| 5 | BackgroundTask lost | ✅* | P1 COMMIT 的 PENDING 留在库 →（driver）P5 find_pending → P2 → P6 → P3 |
| 6 | Worker competition | ✅ | 两个 P6 都走 P2，DB 条件化 UPDATE 串行化 → 一 True 一 False |
| 7 | Worker crash | ✅ | P4 reclaim(T1 孤儿) → P5 → P2(T2) → P6 → P3 |
| 8 | Old attempt completion | ✅ | P4 夺权 → P3(T1) fence 失配 → REJECTED；P3(T2) → COMMITTED |
| 9 | Duplicate + PROCESSING | ✅ | P7 命中 → 返回既有 id；P2 对 PROCESSING 行 claim=False → STOP |
| 10 | Duplicate + orphan PROCESSING | ✅* | P7 命中 → 200/STOP（不重触发）；孤儿由 P4 回收 → P5 → P2 → P6 |

`✅*` = primitive 层面**已闭合**；其**自动触发**依赖 12.9.4.10 §F 冻结、尚未接线的
autonomous driver（daemon loop）。这是触发/部署层缺口，不是正确性缺口（见 §3）。

---

## 1. 参与闭环的 primitive 清单（12.9.4.12 真实落地，已读代码确认）

| 代号 | 方法 | 落点 | 关键语义 |
| --- | --- | --- | --- |
| **P1** | `create_pending` | `interaction_recording.py:188-212`；repo `create` `persistence/repositories/interaction_record.py:32-36` | 落 `processing_status='pending'` 的**独立短事务，COMMIT 后才返回** → ACK 前 PENDING 已持久化 |
| **P2** | `claim_processing` | `persistence/repositories/interaction_record.py:38-61` | 条件化 `UPDATE ... SET status='processing', processing_started_at=func.now() WHERE id=? AND status='pending'`；**单语句原子**写入 DB 时钟；`rowcount==1` 才 True |
| **P3** | `record_outcome` | `persistence/repositories/interaction_record.py:63-118` | 条件化 UPDATE + **attempt fence**（`WHERE status='processing' AND processing_started_at=:expected`）；返回 `OutcomeCommit.COMMITTED` / `REJECTED`；REJECTED = ownership lost，**正常竞争结果，不抛** |
| **P4** | `reclaim_stale_processing(threshold_seconds, limit)` | `persistence/repositories/interaction_record.py:120-158` | `WHERE status='processing' AND started_at IS NOT NULL AND started_at < datetime('now','-threshold s')` → `status='pending', started_at=NULL`；有界、只碰 PROCESSING、只回收不执行 |
| **P5** | `find_pending(limit)` | `persistence/repositories/interaction_record.py:178-191` | 纯读 `WHERE status='pending' ORDER BY created_at,id LIMIT N`；不 claim、不加锁；只返回 PENDING |
| **P6** | `Worker.process` | `worker.py:62-103` | `_load → claim(P2，False→STOP 绝不执行) → _read_started_at(读回 DB 值) → runtime.execute → record_success/failure(fence)` |
| **P7** | `ChannelInboundService.handle_message` | `channel_inbound.py:123-161`；回查 `_existing_row_id:163-179` | 状态感知接纳：先 `find_by_channel_and_external_id` 回查既有行；**命中 → 直接返回既有 id（不 create）**；不解释状态 |
| **P8** | HTTP `BackgroundTasks.add_task(worker.process, id)` | `api/runtime/channels.py:110-113` | 响应发出后才异步驱动 Worker；`ACK ≠ 执行` |

**关键不变量（12.9.4.11/12 钉死）**：
- app 不生成任何时间戳：`worker.py` 源码无 `datetime.now/utcnow/time.time/func.now`（test_a3）；
- fence 等式两侧都是 DB 值（列 vs claim 后读回值）；
- reclaim 必置 `started_at=NULL` → 新旧 attempt 的 fence 值绝不碰撞（阈值 ≫ 1s 双保险）；
- `record_outcome` 失败路径 REJECTED 静默忽略、不写 Evidence（`interaction_recording.py:259-265, 326-330`）。

---

## 2. 逐场景闭环验证

### 场景 1 — Normal
```
P1 create_pending → PENDING（COMMIT）
  ↓（P8：响应后）BackgroundTasks.add_task(worker.process, id)
P6 process：_load → P2 claim=True（pending→processing, started_at=T）
  → _read_started_at → runtime.execute → P3 record_success(fence=T)
  → WHERE status='processing' AND started_at=T 命中 → rowcount=1
  → COMMITTED → SUCCEEDED × DELIVERED
```
**闭合。** 证据：`test_step12_9_4_5_worker_skeleton::test_a1_process_claims_executes_and_records`。

### 场景 2 — Agent failure
```
P6 process：P2=True → runtime.execute 抛 AgentDispatchFailedError
  → P3 record_failure(fence=T) → STAGE_FAILURE_MAP 映射
    stage=agent_dispatch → processing=FAILED, delivery=PENDING
  → fence 命中 → COMMITTED → FAILED × PENDING
```
**闭合。** 证据：`worker_skeleton::test_d3_other_failure_maps_to_failed_pending`。

### 场景 3 — Delivery failure
```
runtime.execute 抛 ChannelDeliveryFailedError
  → record_failure → stage=delivery → processing=SUCCEEDED, delivery=FAILED
  → fence 命中 → COMMITTED → SUCCEEDED × FAILED
```
**闭合。** 证据：`worker_skeleton::test_d2_delivery_failure_maps_to_succeeded_failed`。

### 场景 4 — Duplicate
```
same (channel_id, external_interaction_id) 再次入站
P7 handle_message → _existing_row_id 命中 → return 既有 id（intake.calls==0，不 create）
  ↓ P8：BackgroundTasks.add_task(worker.process, 既有id)
P6 process(既有id)：
  - 若既有行 = 终态/PROCESSING → P2 claim=False → STOP，Runtime 0 次
  - 若既有行 = PENDING → P2 与「原始那条」的任务竞争，CAS 保证恰好一个 True
  ⇒ 绝不产生第二条行；至多执行一次
```
**闭合。** 证据：`12_9_4_9::test_b_five_states_duplicate_returns_existing_and_stops`
（含 retryable=True 仍 STOP）、`test_d1_duplicate_inbound_returns_200_not_500`
（两次 POST 都 200、行数=1、Runtime 仅 1 次）、
`test_c3_pending_duplicate_still_executes_exactly_once`（首达+重推两任务 → `(True,False)`、`calls==1`）。

### 场景 5 — BackgroundTask lost
```
P1 create_pending COMMIT → 行是 PENDING
进程在 ACK 返回后、BackgroundTasks 被调度前崩溃 → 行仍是 PENDING
（driver）P5 find_pending() → 返回该 PENDING 行
  → P2 claim=True → P6 → P3 → 终态
```
**primitive 层面闭合 ✅**；其自动发生依赖 driver（见 §3）。行不丢（P1 已 COMMIT）、
必被 `find_pending` 看见（P5 只看 pending）、重 pickup 不双执行（P2 的 CAS，场景 6）。
证据等价于场景 1 + P5 纯读语义（`test_12_9_4_4::*`）。

### 场景 6 — Worker competition
```
两个 Worker 对同一 id 调 process：
  W1: _load → P2 claim → rowcount=1 → True → execute（1 次）
  W2: _load → P2 claim → 同一行已非 pending → rowcount=0 → False → 立即 return
Runtime.execute 恰好 1 次
```
**闭合。** 原子性来自 `claim_processing` 的单条条件化 UPDATE（`interaction_record.py:50-61`），
与语言/锁/进程数无关。证据：`worker_skeleton::test_b4_two_workers_race_only_one_executes`。

### 场景 7 — Worker crash
```
P2 claim（T1, started_at=T_A）COMMIT → 进程在 runtime.execute 期间 SIGKILL
  → 行停在 PROCESSING（started_at=T_A），这是 12.9.4.7 §E 点 2/3/4 的孤儿
（driver）P4 reclaim(threshold)：started_at < now-threshold 命中
  → status='pending', started_at=NULL
P5 find_pending → 捡起 → P2 claim（T2, started_at=T_B） → P6 → P3
  → T1 的任何晚到 outcome 因 expected=T_A ≠ T_B 被拒（见场景 8）
```
**闭合。** 证据：`12_9_4_12::test_c1_stale_processing_row_is_reclaimed_to_pending`
（`status=pending, started_at=NULL`）、`test_e1_worker_completes_a_reclaimed_orphan`
（崩溃孤儿 → reclaim → 新 Worker `process() is True`、`calls==1`、落终态）。

### 场景 8 — Old attempt completion
```
T1 慢：claim(T1, started_at=T_A) → 超阈值 → P4 夺权 → P2 claim(T2, started_at=T_B)
T1 晚到：P3 record_success/failure(expected=T_A)
  → WHERE status='processing' AND started_at=T_A —— 行已是 T_B → rowcount=0
  → REJECTED（静默忽略；不写 Evidence、不改 B ownership）
T2 晚到：P3 record_*(expected=T_B) → 命中 → COMMITTED → 终态由 T2 落定
```
**闭合。** 这是整套孤儿恢复设计最核心的回归。证据：`12_9_4_12::test_d1_old_attempt_cannot_write_through_new_attempt`
（A 晚到 REJECTED、B ownership 不动、B outcome COMMITTED）、
`test_d2_old_attempt_success_is_equally_rejected`（对称性：旧 attempt 的成功也写不穿）。

### 场景 9 — Duplicate + PROCESSING
```
入站时行已是 PROCESSING（正在被某 Worker 执行）
P7 _existing_row_id 命中 → return 既有 id（不 create）
P8 add_task(worker.process, 既有id)
P6 process(既有id)：P2 claim → 行是 processing（非 pending）→ False → STOP
Runtime 0 次
```
**闭合。** 不重触发在飞的执行（重推≠重试，12.9.4.9 冻结）。证据：
`12_9_4_9::test_c2_other_states_also_do_not_reenroll_worker`（PROCESSING 用例）。

### 场景 10 — Duplicate + orphan PROCESSING
```
行是「孤儿 PROCESSING」：崩溃留下、尚未被 reclaim（started_at=T_A 仍非 NULL）
P7 _existing_row_id 命中 → return 既有 id、HTTP 200、不 create
P8 add_task(worker.process, 既有id) → P2 claim=False（PROCESSING）→ STOP（不重触发）
孤儿回收改由 P4：reclaim(threshold) → status=pending, started_at=NULL
  → P5 find_pending 捡起 → P2(T2) → P6 → P3   （即场景 7 的回收闭环）
```
**闭合。** Duplicate 本身不替代 reclaimer：它只保证「重推不会让孤儿被错误重执行/造第二行」，
真正的恢复交给 P4→P5→P2→P6 这一条既有路径。证据：场景 9 的 STOP 行为 +
`12_9_4_12::test_c1/c2/c3`（reclaim 只碰 PROCESSING、不碰终态/PENDING）。

---

## 3. 唯一残留缺口（非正确性缺口，已多次确认、显式后置）

**autonomous driver（daemon loop / scheduler）尚未接线**（12.9.4.10 §F 冻结；12.9.4.7 §2）。

- 10 个场景里，**场景 5 / 7 / 10 的「自动」闭合**需要周期性调用方：
  ```
  loop:
      ids = find_pending(limit)               # P5
      ids += reclaim_stale_processing(thr)    # P4
      for id in ids: worker.process(id)       # P6（仍走 P2 CAS）
  ```
- **primitive 能力已经齐备**：P5/P4/P2/P6 全部就位且经测试证明可拼成闭环；
  driver 只是这几条 primitive 的循环调用者，不引入新的执行语义，因此不带来新的双执行风险。
- 在 driver 落地前：BackgroundTasks 丢失的 PENDING（场景 5）与崩溃留下的 PROCESSING 孤儿
  （场景 7/10）**不会被自动回收**，只能等手动触发或未来的 daemon —— 这是「不加 heartbeat/lease」
  的显式代价（孤儿检测延迟 ≈ threshold，分钟级），与既有审计结论一致，**不是本次新发现的缺陷**。
- **仍需遵守的硬纪律**（12.9.4.10 §A.3）：`threshold > 执行上界`
  （默认 agent 30s + delivery 10s ≈ 40s，加 margin），否则慢 Worker 会被误回收 → 双执行。

---

## 4. 实证证据汇总（本次运行）

```
test_step12_9_4_12_attempt_fence.py          ✓ 全过（Schema/Claim/B.fence/C.reclaim/D.核心回归/E.Worker 端到端）
test_step12_9_4_9_state_aware_duplicate_ack.py ✓ 全过（五状态 STOP / retryable 仍 STOP / 两次 200）
test_step12_9_4_5_worker_skeleton.py         ✓ 全过（G1 不执行 / G2 唯一 owner / G3 不重解释）
test_step12_9_4_2_persistence_phase_api.py   ✓ 全过
test_step12_9_4_4_find_pending_pickup.py     ✓ 全过
test_step12_9_4_6_intake_ack_migration.py    ✓ 全过（ACK≠执行 / BackgroundTasks 驱动）
test_step12_9_4_3_interaction_snapshot_reconstruction.py ✓ 全过
```
本轮共 **99 个相关用例全绿**。除 §3 的 driver 接线外，10 个端到端场景均已被代码与上述测试
证明可组成完整闭环。

---

## 5. 裁决

> **对 Step 12.9.4.13 的提问——「已实现的所有 primitive 能否组成完整闭环？」——答案：能。**
>
> 1–4、6、8、9 在**当前架构内即自动闭合**（无需任何新设施）；
> 5、7、10 的闭环**primitive 已齐备**，只差一个被刻意冻结、明确后置的 autonomous driver 去
> 周期性调用 `find_pending` + `reclaim_stale_processing`。
>
> 不建议为闭合这些场景引入 queue / Redis / Celery / lease / heartbeat / scheduler / cron：
> 它们解决的是「规模 / 多实例 ownership / 自主触发」，而非「恢复正确性」——正确性已被
> P1–P8 证明成立。上线前置项仍是：**在让生产依赖异步执行前，先接好 daemon loop（§3）**。
