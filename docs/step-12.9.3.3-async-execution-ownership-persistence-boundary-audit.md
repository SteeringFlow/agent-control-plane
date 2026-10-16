# Step 12.9.3.3 — Async Execution Ownership & Persistence Boundary Audit

> 只读审计（承接 12.9.1 ACK 边界、12.9.2 duplicate ACK、12.9.3.1 record 充分性、
> 12.9.3.2 Worker 所有权）。聚焦**所有权与持久化边界划分**，不再研究 crash。
>
> **重要事实校正**：本章基于实际代码，而非 12.9.1 摘要里「APIRuntimeBridge 自己
> create_request_record」的旧描述。真实链路里 `ChannelInboundService.handle_message`
> **不创建记录**，PENDING 的唯一创建点在 `PersistingCoordinator._start`。

---

## 0. 当前链路事实还原（ground truth）

```
wecom_message (channels.py:81)
  → ChannelInboundService.handle_message (channel_inbound.py:94)
       ├─ _load_channel(...)                 # 仅一次 UoW 读，定位渠道
       ├─ adapter.parse_message(...) → InboundInteraction | None
       └─ self._runtime.execute(inbound)     # channel_inbound.py:117  ← 唯一引擎调用点
            │  self._runtime == PersistingCoordinator（组合根注入）
            ▼
      PersistingCoordinator.execute (interaction_recording.py:134)
        ├─ _start (interaction_recording.py:152)
        │    ├─ 事务① uow.interactions.create(...)        # :162  ← PENDING 唯一创建点
        │    └─ 事务② uow.interactions.mark_processing()  # :179  ← PROCESSING
        ├─ self._inner.execute(inbound)                  # :143  ← RuntimeCoordinator（引擎）
        ├─ _record_success(result)  | _record_failure(exc)  # :147 / :145  ← 终态
```

事实锚点：
- **PENDING 创建**只发生一次，在 `PersistingCoordinator._start` 事务①
  （`interaction_recording.py:162`）。`ChannelInboundService` 不创建。
- **PROCESSING 标记**在 `PersistingCoordinator._start` 事务②
  （`interaction_recording.py:179`）。
- **终态（SUCCEEDED/FAILED + delivery）** 在 `PersistingCoordinator._record_*`
  （`:147` / `:145`），由 `execute` 在 `inner.execute` 返回/抛错后调用。
- **引擎 `RuntimeCoordinator`**（`coordinator.py:85`）是纯编排器，零 ORM / 零持久化
  （`:26-27, :65`），Delivery 是其第 6 段（`coordinator.py:149`）。
- **当前唯一引擎调用点**：`ChannelInboundService.handle_message:117` →
  `self._runtime.execute`，而 `self._runtime` 被组合根装配为 `PersistingCoordinator`。
  即今天同步路径**直接驱动引擎**，PENDING/PROCESSING/终态三步由同一个
  `PersistingCoordinator.execute` 在一次 webhook 请求内同步完成。

---

## 1. PENDING 创建应该由谁负责？

**目标（异步模型）：同步入站边界（intake edge）。**

理由（承接 12.9.1 结论「ACK 与执行解耦」）：
- 200 ACK 的语义应是「**已持久化接收 + 已入队（PENDING）**」，而非「已执行」。
- 因此 PENDING 必须在**入站请求内、返回 200 之前**被 durable commit——这件事天然
  属于同步 intake edge，不属于 Worker（Worker 异步拾取已存在的 PENDING）。
- 创建点应依赖现有 `UNIQUE(channel_id, external_interaction_id)` 做重复抑制
  （duplicate ACK 由「第二次 create 撞 UNIQUE → 识别为已接收」吸收，见 12.9.2）。

**当前归属调整**：今天 PENDING 创建藏在 `PersistingCoordinator._start` 事务①。异步化时
它必须**从 PersistingCoordinator 抽出**，上移到 intake edge（即新的「intake 持久化」
职责，由 `ChannelInboundService` 调用），这样同步路径在写完 PENDING 后即返回 ACK，
永不再触碰引擎。

→ **结论：PENDING 创建 = 同步入站边界（intake）的责任；Worker 绝不创建 PENDING。**

---

## 2. PENDING → PROCESSING 的唯一 owner 应该是谁？

**Worker（唯一异步执行驱动者）。**

- 只有 Worker 才允许做 `PENDING → PROCESSING` 这一步，且必须**原子 claim**
  （`UPDATE ... SET processing_status='processing' WHERE id=:id AND processing_status='pending'`
  + rowcount 检查，见 12.9.3.2 §2）。
- **同步入站边界绝不允许**把自己创建的 PENDING 直接转 PROCESSING——它只写 PENDING 就
  返回。这从结构上保证「claim 的唯一 owner = Worker」。

→ **结论：PROCESSING 的唯一 owner = Worker 的 claim 步骤。**

---

## 3. Worker 是否直接调用 RuntimeCoordinator？

**是。** Worker claim 成功后，应**直接调用与今天同步路径完全相同的
`RuntimeCoordinator.execute(inbound)`**（引擎零改动），拿到 `RuntimeExecutionResult`
或 typed exception，再持久化终态。

- Worker 是**执行驱动者（driver）**，`RuntimeCoordinator` 是**执行引擎（engine）**；
  Worker 不重新实现六段管线，只负责「claim → 调引擎 → 记终态」三段驱动逻辑。
- Worker **不应**经由任何「create-PENDING 装饰器」间接调用引擎——因为 PENDING 已在
  intake 写好。Worker 调用的是**裸引擎** + 独立的 claim/record 持久化方法。

→ **结论：Worker 直接调用 RuntimeCoordinator（裸引擎），不包任何创建装饰。**

---

## 4. PersistingCoordinator 是否应该保留？

**保留「持久化观察职责」，退役「单体 execute 装饰器形态」。**

当前 `PersistingCoordinator` 把三件所有权不同的事绑进一个 `execute(inbound)`：
(a) 创建 PENDING（→ intake）、(b) 标记 PROCESSING（→ Worker claim）、
(c) 记终态（→ Worker 执行后）。这只在「一个调用方在同一请求内同步做完三步」时成立。
异步化后三步**时间上分离、归属上分属两条路径**，单体 `execute` 不再适配任一路径。

因此：
- **职责保留**：保持「Executor/Coordinator 零持久化依赖、事实由观察层记录」这条
  9.A / 10.3.1 / 10.4.1 守卫不变——这是本项目反复冻结的核心纪律。
- **形态退役**：把单体 `execute` 拆成**显式 phase 方法**，让 intake 与 Worker 各取
  所需（见 Q5）。换言之，`PersistingCoordinator` 这个类名/形态应被一个更薄的
  「交互持久化服务」（如 `InteractionRecorder`）取代，但「观察并记录 Runtime 执行事实」
  这一层**必须保留**。

→ **结论：保留职责、拆分形态；不保留单体 execute 装饰器。**

---

## 5. 若保留，职责变成什么？

拆成三个显式、单一职责的方法（纯持久化观察，不调用引擎、不编排）：

| 方法 | 调用方 | 职责 |
| --- | --- | --- |
| `create_pending(inbound)` | intake edge | 写 PENDING + received Activity，依赖 UNIQUE 去重 |
| `claim_processing(id, token)` | Worker | 原子 CAS（`WHERE processing_status='pending'`）+ 写 `processing_started_at` / `claimed_by`（12.9.3.2 §6 最小字段）；返回 rowcount 供 Worker 判断是否 claim 成功 |
| `record_outcome(id, result|exc)` | Worker | 写 SUCCEEDED/FAILED + delivery + stage Evidence（即今天的 `_record_success/_record_failure` 内容） |

该服务**不再持有 `RuntimeCoordinator` 引用、不再 `inner.execute`**——引擎调用权完全
交给 Worker。它只负责「把观察到的执行事实落库」。

→ **结论：职责 = 三个 phase 方法的纯持久化观察器，不含编排、不含引擎调用。**

---

## 6. 如何保证 RuntimeCoordinator 不被两条路径以不同方式调用？

核心保证 = **同步路径根本不持有引擎引用 + 引擎只有唯一调用点 + 结构接线 + 守卫测试。**

1. **同步 intake 路径不调用引擎**：`ChannelInboundService` 异步化后只注入
   `create_pending`（intake 持久化），**不注入** `RuntimeCoordinator`。它写完 PENDING
   即返回 200，物理上无法触发执行。
2. **唯一引擎调用点 = `Worker.process(interaction_id)`**：claim 与 `runtime.execute`
   都封死在这一方法内，全代码仅此一处调用 `RuntimeCoordinator.execute`。
3. **不同 Protocol 隔离**：intake 依赖 `IntakePort`（仅 `create_pending`）；Worker 依赖
   `ExecutionPort`（`RuntimeCoordinator` + `claim` + `record_outcome`）。类型层面两条
   路径拿到不同能力集，无法在错误位置误调引擎。
4. **组合根单一装配 + 守卫测试**：组合根（main.py）同时装配两条路径，并用 Layer-3
   守卫测试（仿 `test_step12_6` 的「装配一致性守护」）断言：
   `ChannelInboundService` 实例**不**能触及 `RuntimeCoordinator`；`RuntimeCoordinator`
   的调用站点计数 == 1（Worker）。

→ **结论：靠「接线隔离 + 唯一调用点 + 守卫测试」三重保证，而非靠约定。**

---

## 7. Agent execution 最终 SUCCEEDED / FAILED 谁负责持久化？

**Worker**，通过 `InteractionRecorder.record_outcome(id, result|exc)`——在
`RuntimeCoordinator.execute` 返回 `RuntimeExecutionResult` 或抛出 typed exception 之后
（即今天 `PersistingCoordinator._record_success/_record_failure` 的逻辑，脱离单体
`execute` 后由 Worker 驱动）。

注意：**同步入站边界已经返回 200 ACK**（在 PENDING 提交时），所以终态持久化与 ACK
完全解耦——即使执行在数秒后失败，HTTP 侧早已 200，符合 12.9.1「执行失败 ≠ ACK 失败」
的语义（WeCom 重试由终态 retryable + 重复 PENDING 的 UNIQUE 抑制共同处理，见 12.9.2）。

→ **结论：SUCCEEDED/FAILED 终态 = Worker 的 record_outcome 责任。**

---

## 8. Delivery status 的持久化谁负责？

**同样是 Worker**，且**与 agent 执行终态在同一个 `record_outcome` 调用内**——因为
Delivery 是 `RuntimeCoordinator` 的第 6 段（`coordinator.py:149`），发生在引擎内部。
Worker 观察到的「引擎返回 / 引擎抛 typed error」已包含 delivery 事实：

- 引擎**返回** result → Delivery 成功 → `processing=SUCCEEDED, delivery=DELIVERED`；
- 引擎抛 `ChannelDeliveryFailedError` → Delivery 失败（但 Dispatch 已成功）→
  `processing=SUCCEEDED, delivery=FAILED`（4.2 / 5.3 冻结语义：Delivery 失败 ≠
  Processing 失败）；
- 引擎抛其他 typed error（Identity/Routing/Session/Dispatch）→ STOP 在 delivery 前 →
  `processing=FAILED, delivery=PENDING`。

因此 delivery 状态**不另设写入者**，它只是 `record_outcome` 根据「引擎实际观察」推导出的
一个字段，与 12.9.3.1「record 充分性」结论一致（delivery_status 已是 interaction 行上
独立字段，可独立表达）。

→ **结论：Delivery 持久化 = Worker 的 record_outcome（与执行终态同源、同调用）。**

---

## 9. 是否能够做到 一个且仅一个 Runtime execution path？

**能，且应作为架构不变量强制。** 目标形态：

```
HTTP webhook
  → ChannelInboundService.handle_message
       → adapter.parse_message → InboundInteraction
       → intake.create_pending(inbound)   # 写 PENDING + received，UNIQUE 去重
       → return 200 ACK                    # ← 同步路径到此为止，绝不调引擎

Worker.process(interaction_id):            # ★ RuntimeCoordinator 唯一调用点
  → claim = recorder.claim_processing(id, token)   # 原子 CAS
  → if not claimed: return                # 他人已 claim，放弃
  → inbound = load_from_record(id)
  → try:    result = runtime.execute(inbound)   # RuntimeCoordinator（零改动）
    except typed as exc: recorder.record_outcome(id, exc)
    else:               recorder.record_outcome(id, result)
```

满足「一个且仅一个执行路径」的三要素：
1. **一个引擎**：`RuntimeCoordinator` 不变、不感知持久化/异步；
2. **一个调用点**：`Worker.process` 是全代码唯一 `runtime.execute` 调用处；
3. **同步路径零引擎接触**：intake 只 `create_pending`，结构上无法执行。

组合保证 once-only 执行：
- `UNIQUE(channel_id, external_interaction_id)` → 每 MsgId 至多 1 个 PENDING；
- Worker 原子 claim（`WHERE processing_status='pending'` + rowcount）→ 至多 1 个
  Worker 进入执行；
- 二者叠加 → **每 MsgId 恰好一次执行**（exactly-once 语义由「去重 + 原子 claim」给出）。

**迁移不变量（诚实标注）**：从「同步路径驱动引擎」切到「仅 Worker 驱动引擎」时，必须
**原子切换**——`ChannelInboundService` 删除 `runtime.execute` 调用的同一次提交/部署中
引入 Worker。否则会出现「同一条消息既被同步执行、又被 Worker 拾取」的**双执行窗口**
（正是 12.9.3.2 §3 警告的失效模式）。该切换属于「构建 Worker」这一未来动作的组成部分，
与 12.9.1 / 12.9.3.2 结论一致：**现在只记录目标形态与守卫，不预先实现。**

→ **结论：能。靠「intake 只入队 + Worker 唯一调用引擎 + 去重&原子 claim 双保险」实现；
   但需与 Worker 的引入做原子迁移，避免切换期双执行。**

---

## 10. 结论速查表

| 问题 | 答案 |
| --- | --- |
| PENDING 创建谁负责 | **同步入站边界（intake）**；从 PersistingCoordinator 抽出上移；Worker 绝不创建 |
| PENDING→PROCESSING 唯一 owner | **Worker**（原子 claim；同步路径永不转 PROCESSING） |
| Worker 是否直接调 RuntimeCoordinator | **是**，调裸引擎，不包创建装饰 |
| PersistingCoordinator 是否保留 | **保留职责、退役单体 execute 形态** → 拆为 `InteractionRecorder` phase 方法 |
| 保留后职责 | `create_pending` / `claim_processing` / `record_outcome` 三显式方法，纯观察、不编排 |
| 如何保证引擎不被双路径误调 | intake 不持有引擎引用 + `Worker.process` 唯一调用点 + 不同 Protocol + 守卫测试 |
| SUCCEEDED/FAILED 谁持久化 | **Worker** 的 `record_outcome`（已脱离单体 execute） |
| Delivery status 谁持久化 | **Worker** 的 `record_outcome`（与执行终态同源同调用，含 DELIVERY_FAILED→processing=SUCCEEDED） |
| 能否一个且仅一个执行路径 | **能**：intake 只入队 + Worker 唯一调引擎 + UNIQUE 去重 & 原子 claim 双保险；需与 Worker 引入原子迁移 |
