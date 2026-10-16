# Step 12.9.3.6 — Final Minimal Async Architecture & Migration Safety Audit

> 闭环审计（只读，不实现）。把 12.9.1 / 12.9.2 / 12.9.3.1~12.9.3.5 的发现收口为一份
> 最终架构裁决 + 迁移安全分析。
>
> 证据索引（均已逐条核实，非假设）：
> - 12.9.1 ACK 解耦 / Intake 双短事务边界
> - 12.9.2 UNIQUE 去重 / lookup-before-execute
> - 12.9.3.1 record 充分性
> - 12.9.3.2 Worker 所有权 / CAS / `processing_started_at` 最小字段
> - 12.9.3.3 所有权与持久化边界 / 单一执行路径
> - 12.9.3.4 DB-backed pickup & claim / 事务边界证据
> - 12.9.3.5 Delivery 延续 / `request_payload` 漏存 `metadata` 缺口
>
> 关键代码锚点：
> - `interaction_recording.py:162-181`（`_start` 两短事务）、`:179`（mark_processing txn②）、
>   `:143`（`inner.execute` 在事务外）、`:195-224`（`_record_*` 独立短事务）
> - `coordinator.py:85`（RuntimeCoordinator 纯编排）、`:26-27,:65`（不 import Persistence）
> - `interaction_record.py:30-33`（mark_processing 非 CAS）、无 `find_pending`
> - `wecom.py:389`（response_url→inbound.metadata）、`:234-248`（`_response_url_from`）、
>   `:551,:556`（deliver 双分支）
> - `database.py:23`（SQLite 为 MVP 真实库）、`:50-60`（FK）、`:71-72`（check_same_thread）
> - `unit_of_work.py:80-87`（每 `with uow` = 一短事务）

---

## 1. A/B/C/D 四种方案最终裁决

> 四个候选执行模型，对「ACK 与 ~16s Agent 执行如何解耦」作答。

| 方案 | 形态 | 裁决 | 理由 |
| --- | --- | --- | --- |
| **A. 同步执行（现状）** | `Inbound → Runtime.execute() → 200` | **REJECT** | ACK 与 16s 执行耦合；WeCom 5s 超时重推 → 重复 Runtime 执行（12.9.1）；违反「ACK≠执行」 |
| **B. DB-backed Worker（轮询 + CAS claim）** | `Inbound → PENDING → 200`；`Worker: pickup→claim→execute→outcome` | **ADOPT（MVP）** | 复用 `interactions` 行（本就是执行单元）、UNIQUE 去重、UoW 短事务、CAS 纯 SQL；零新基础设施（§3） |
| **C. 消息队列（Redis/RabbitMQ/Celery）** | Broker + 消费者组 | **DEFER** | 无规模证据需队列；新增 broker 违反 12.8.6「无真实需求不增基础设施」；DB 已是可靠存储（§4）；Queue 只是未来可替换的 pickup 细节 |
| **D. 进程内后台任务（asyncio/线程池）** | 进程内 fire-and-forget | **REJECT** | 进程重启/crash 丢失 in-flight 任务；无跨进程 ownership；无法 CAS claim；与「多实例 Worker」目标冲突 |

→ **最终裁决：B（DB-backed Worker）为 MVP 最小正确方案；A 回退、C 延后、D 不可行。**

---

## 2. 为什么 DB-backed Worker 是 MVP 最小方案

所需能力**已全部存在**，无需新建：

1. **执行单元已就位**：`interactions` 行本质是「一个 MsgId 的一次执行单元」（10.5.2），
   无需新 Job/Worker/Execution 实体（12.9.3.2 §8）。
2. **去重已就位**：`UNIQUE(channel_id, external_interaction_id)` 抑制重复 PENDING
   （12.9.2）。
3. **状态机已就位**：`processing_status: PENDING→PROCESSING→SUCCEEDED|FAILED` +
   `delivery_status`（4.2/5.3）已冻结。
4. **事务纪律已就位**：UoW 短事务（`unit_of_work.py:80-87`）+ 现有 `_start→execute→_record`
   三明治（12.9.3.4 §D 证据）直接复用为「claim 短事务 → 无事务执行 → outcome 短事务」。
5. **CAS claim 是纯 SQL**：`UPDATE ... WHERE id=:id AND processing_status='pending'`
   + rowcount，SQLite/PostgreSQL 均原生支持（12.9.3.2 §2、12.9.3.4 §B）。
6. **RuntimeCoordinator 零改动**：纯编排、无持久化（coordinator.py:26-27,65），天然
   可作「唯一执行引擎」（12.9.3.3 §E）。
7. **最小 schema 增量**：仅 +1~2 个 nullable 列（`processing_started_at` + 可选
   `claimed_by`），迁移零风险（NULL=未 claim=与今天一致）（12.9.3.2 §6）。

→ **B 是「在既有 8 表 + 既有短事务纪律」上做最小加法，而非新架构。**

---

## 3. 为什么 Queue 暂不需要

- **无规模证据**：MVP 单/少量渠道，突发量≈WeCom 5s 超时重推，远低于需要 broker 的吞吐；
  12.8.6 / 10.x 纪律明确「YAGNI，真实需求出现再演进」。
- **DB 已提供队列的核心保证**：持久缓冲（PENDING 行）、有序（created_at）、
  所有权（CAS claim）。Queue 不能去掉这些 DB 写（记录仍为 Evidence），只会**叠加**
  broker，复杂度净增、正确性无增益。
- **可替换性**：Queue 只是 pickup 机制的未来实现细节。若未来需队列，只需把
  `find_pending` 轮询换成「订阅 broker → 拿到 id → 同款 CAS claim → 同款
  `RuntimeCoordinator.execute`」。Worker / RuntimeCoordinator / Delivery / 持久化观察层
  **全部不动**（12.9.3.3 §E 边界隔离使然）。故 Queue 是「未来可选插件」，非现在决策。
- **与 `database.py:23` 一致**：「SQLite 就是 MVP 真实库，换 PostgreSQL 只改 URL」——
  引入 broker 会破坏这一「零额外基础设施」前提。

→ **Queue = 推迟到真实规模证据出现后的可插拔 pickup 后端，不是 MVP 决策。**

---

## 4. Intake → ACK 的精确 transaction boundary

```
HTTP POST (WeCom 回调)
  │
  ▼ ChannelInboundService.handle_message (channel_inbound.py:94)
  │   adapter.parse_message(...) → InboundInteraction        # 无事务（纯解析）
  │
  │   ┌─────────────────── 短事务①（intake 边界） ───────────────────┐
  │   │ BEGIN;                                                          │
  │   │   INSERT interactions(                                         │
  │   │     processing_status='pending',                               │
  │   │     request_payload={..., "metadata": inbound.metadata},  ← 12.9.3.5 修复
  │   │     received 快照)                                              │
  │   │   [UNIQUE(channel_id, external_interaction_id) 撞键 → 识别为已接收 → 幂等] │
  │   │ COMMIT;                                                        │
  │   └──────────────────────────────────────────────────────────────┘
  │
  ▼ return 200 ACK            # ← 仅在此之后返回；ACK = "已持久化接收 + 已入队(PENDING)"
```

- **边界定死**：200 只在 PENDING 行 durable commit 之后返回（12.9.1 解耦）。
- **intake 永不调用引擎**：`self._runtime.execute(inbound)` 从 `ChannelInboundService`
  移除（12.9.3.3 §1/§6）。intake 只注入 `IntakePort`（`create_pending`）。
- **精确事务 = PENDING 插入这一条短事务**（ms 级），不含任何 Agent/Channel I/O。
- **duplicate 吸收**：撞 UNIQUE 的第二次插入被 DB 拒绝 → intake 识别为已接收 → 仍返回
  200（幂等）（12.9.2）。

---

## 5. Worker pickup → claim → execution → outcome 完整 transaction boundary

```
Worker.process():                         # ★ RuntimeCoordinator 唯一调用点（12.9.3.3 §9）
  │
  │  pickup   : find_pending(LIMIT n)     # 只读 SELECT，无事务/不持锁（12.9.3.4 §A）
  │            （重叠无害：只读，不 claim）
  │
  │  ┌─────────────────── 短事务②（claim） ───────────────────┐
  │  │ BEGIN;                                                      │
  │  │   UPDATE interactions                                      │
  │  │     SET processing_status='processing',                   │
  │  │         processing_started_at=now(),                      │
  │  │         claimed_by=:token                                  │
  │  │     WHERE id=:id AND processing_status='pending';          │
  │  │   rowcount = executed.rowcount;                            │
  │  │ COMMIT;                          # ← 提交即释放任何锁      │
  │  └────────────────────────────────────────────────────────┘
  │  if rowcount != 1: return              # 他人已 claim，放弃
  │
  │  inbound = reconstruct_from(request_payload)   # 含 metadata（12.9.3.5 修复后）
  │
  │  result = RuntimeCoordinator.execute(inbound)   # ★ 无任何打开事务（~16s）
  │        │                                         # 证据：12.9.3.4 §D
  │        │   Identity→Routing→Session→Dispatch→【Agent ~16s】→Delivery
  │        │
  │  ┌─────────────────── 短事务③（outcome） ──────────────────┐
  │  │ BEGIN;                                                      │
  │  │   UPDATE interactions                                      │
  │  │     SET processing_status/delivery_status/                │
  │  │         failure_stage/failure_code/retryable/             │
  │  │         session_id/response_payload = ...;                │
  │  │   COMMIT;                                                  │
  │  └────────────────────────────────────────────────────────┘
```

- **三段各自独立短事务，执行在事务外**： claim(②) 提交 → 关 Session → execute(~16s,
  无 Session) → outcome(③) 新事务。这**原样复用**了现有 `_start`(①②) → `inner.execute`
  → `_record_*`(独立事务) 三明治（interaction_recording.py:162-224），不是新发明。
- **反模式否决**：`BEGIN; CAS; execute(16s); COMMIT`（把执行裹进事务）被双重否决——
  SQLite 库级锁（database.py:23,50-60）会阻塞全部写入 16s；PostgreSQL 占连接池 + 若
  用 FOR UPDATE 则行锁持 16s 阻塞孤儿回收（12.9.3.4 §D 证据 3/4）。
- **序列化点 = claim(②)**，不是 pickup。pickup 只读、可重叠；执行唯一性由 CAS 保证。

---

## 6. Duplicate semantics（去重语义）

| 层 | 机制 | 效果 |
| --- | --- | --- |
| 创建期 | `UNIQUE(channel_id, external_interaction_id)` | 同 MsgId 第二次 intake 插入撞键 → 识别为已接收 → 幂等 200（12.9.2） |
| 执行期 | UNIQUE（仅 1 个 PENDING 行）+ CAS claim（仅 1 个 Worker rowcount=1） | 同 MsgId **恰好一次执行** |
| WeCom 重推（5s 超时） | 每次重推都 intake → 创建或撞 UNIQUE → 仅 1 个 PENDING → Worker claim 1 次 | 重推**不**导致双执行 |

→ **双重保险：UNIQUE 保证单 PENDING 行，CAS 保证单执行；WeCom 重试被吸收为幂等。**

---

## 7. Concurrent Worker semantics（并发 Worker 语义）

- 多 Worker 同时扫描 PENDING {#1,#2,#3}：候选集可重叠，**重叠无害**（pickup 只读）。
- CAS claim 逐行：`A.claim(#1)→rowcount=1` 拥有；`B.claim(#1)→rowcount=0` 跳过转 #3。
  **无 A→#1 且 B→#1 的双执行窗口**（12.9.3.4 §C）。
- **不依赖 SKIP LOCKED**：SQLite（MVP 真实库）不支持 `FOR UPDATE`/`SKIP LOCKED`
  （database.py:23）；即便 PG，SKIP LOCKED 仅是竞争优化，正确性来自 CAS（12.9.3.4 §C）。
- 竞争代价：重叠仅产生廉价 rowcount=0 回退，MVP 规模可接受。

→ **并发安全性由 CAS（单序列化点）+ UNIQUE（单 PENDING 行）双重保证，DB 无关。**

---

## 8. Delivery semantics（投递语义）

Delivery 是 `RuntimeCoordinator` 第 6 段（coordinator.py:149），Worker 观测终态：

| 引擎行为 | 映射（STAGE_FAILURE_MAP, interaction_recording.py:98-108） | 异步是否成立 |
| --- | --- | --- |
| 返回 result（Delivery 成功） | `SUCCEEDED × DELIVERED` | ✓ |
| 抛 `ChannelDeliveryFailedError`（Dispatch 成功、Delivery 失败，4.2/5.3） | `SUCCEEDED × FAILED`（10.7.2 B2） | ✓ |
| 抛其它 typed error（Delivery 前 STOP） | `FAILED × PENDING` | ✓ |

- 矩阵由**异常类型**驱动，与同步/异步无关（12.9.3.5 §C）。
- `retryable`：`response_url` 一次性 → `retryable=False`（wecom.py:585），由 typed error
  实例携带，Worker 经 `getattr(exc,"retryable",...)` 传播（interaction_recording.py:240）。
- **关键缺口（§10）**：若 `inbound.metadata` 未在 intake 持久化，机器人回复会静默错投到
  `message/send`（wecom.py:350 禁止）。这是事实缺失，非矩阵语义变化。

→ **三组合全部成立；唯一风险是 metadata 缺口导致 Delivery 走错通道（见 §10）。**

---

## 9. Crash semantics（崩溃语义）

| 故障点 | 现象 | 恢复 |
| --- | --- | --- |
| Worker crash（claim 后、outcome 前） | 行卡 `PROCESSING` | 无确定性检测（12.9.3.2 §5）；TTL 扫描 `WHERE processing_status='processing' AND processing_started_at < now()-TTL` 重新 claim+执行（概率性 at-least-once，可接受） |
| intake/DB crash（PENDING commit 前） | 客户端无 200 → WeCom 重推 → 重入 intake → 创建或撞 UNIQUE | 幂等（§6） |
| DB crash | SQLite 文件持久性；已提交 PENDING 留存，未提交丢失 | 客户端重推 |
| Worker 进程在 claim 事务中被杀 | claim 原子（单 UPDATE，崩溃自动回滚） | 无部分 claim |
| 多实例 | 各 Worker 独立；CAS 跨进程防双 claim（DB 为共享真相源） | — |

→ **崩溃代价 = 延迟（TTL 重投）或幂等重入，绝不双执行**（因 CAS + UNIQUE）。**

---

## 10. Metadata persistence gap（已知缺口，必须随 Worker 一起关闭）

- **事实**：`PersistingCoordinator._start` 的 `request_payload`
  （interaction_recording.py:162-173）**未存 `inbound.metadata`**（12.9.3.5 §A）。
- **后果**：Worker 重建 `InboundInteraction.metadata=None` → `OutboundResponse.metadata`
  无 `response_url` → `WeComAdapter.deliver` 落入 `message/send` 分支 → 智能机器人回复
  **静默错投**（绕过入站 Fail Closed，不报错）（12.9.3.5 §A/B）。
- **最小修复**：`_start` 的 `request_payload` 加 `"metadata": inbound.metadata`。零 schema
  变更（JSON 列），不动引擎/契约（12.9.3.5 §D）。
- **归属**：与「构建 Worker」同属未来动作；**现在只记录、不预先实现**（12.8.6 纪律）。
  但若 Worker 先于该修复上线，机器人 Delivery 静默失效——故迁移清单须将其列为前置项。

---

## 11. 旧同步路径 → 新 Worker 路径的原子迁移（杜绝双驱动窗口）

```
OLD
  Inbound → Runtime.execute()                         # ChannelInboundService 直接驱动（同步）

NEW
  Inbound → PENDING → 200
                      ↓
                    Worker
                      ↓
                 Runtime.execute()                    # Worker 成为唯一驱动者
```

**双驱动窗口的定义**：若 intake 仍 `runtime.execute` **且** Worker 也已上线，同 MsgId 会
被同步执行一次 + 被 Worker 执行一次。注意 **UNIQUE 拦不住双执行**——因为同步路径不走
PENDING/claim，直接 execute（12.9.3.2 §3）。

**原子迁移策略（杜绝窗口）**：

1. **单一原子代码变更**：在同一 commit/部署中——
   - 从 `ChannelInboundService` **移除** `self._runtime.execute(inbound)`（改为仅
     `create_pending` + 200）；
   - **引入** `Worker`（pickup→claim→execute→outcome）。
   两者不可分拆部署。
2. **全量重启式部署（非滚动）**：MVP 单服务、少渠道，采用「全部实例同时切换」的硬 cutover，
   而非滚动发布。理由：滚动期间「旧同步实例」与「新 Worker 实例」可能共存——旧实例
   走 `_start`（创建 PENDING → 非原子 mark_processing → 同步 execute），新 Worker 也可能
   claim 同一行，而旧 `mark_processing` 非 CAS（interaction_record.py:30-33）会无视 claim
   继续执行 → **双执行窗口**（12.9.3.2 §3）。全量重启消除该共存态。
3. **组合根单一装配**：`main.py` 同时把 `ChannelInboundService` 接 `IntakePort`
   （仅 `create_pending`）、把 `Worker` 接 `RuntimeCoordinator` + `claim` + `record_outcome`。
   守卫测试断言 `ChannelInboundService` 实例触及不到 `RuntimeCoordinator`（12.9.3.3 §6）。
4. **安全属性（关键）**：因 ACK 已与执行解耦，**即便 Worker 启动延迟/不健康，intake 仍只
   入队**——最坏情况 = PENDING 堆积、处理延迟，**绝不双执行**。这把「迁移风险」从
   「数据损坏（双执行）」降为「延迟」，使 cutover 安全。
5. **（可选）双保险**：过渡期把旧路径的 `mark_processing` 也改为 CAS——即便误滚动，先
   claim 者（CAS rowcount=1）赢，另一方见 `PROCESSING` 跳过。但主保证仍是「全量重启 +
   代码原子变更」。
6. **回滚**：Worker 异常时回滚到同步（重加 `runtime.execute`、禁用 Worker）。回滚期间
   因双驱动被重新引入需同步禁用 Worker——故回滚也是原子变更。

→ **切换窗口不存在的保证 = 「intake 删除 execute 调用」与「Worker 引入」是同一原子变更 +
   全量重启部署；post-deploy intake 永不执行、Worker 唯一执行。**

---

## 12. Architecture guards / tests 应守住的 invariant

| # | Invariant（不变量） | 守护测试形态 |
| --- | --- | --- |
| G1 | **单一执行路径**：`RuntimeCoordinator.execute` 全代码恰 1 个调用点（Worker.process） | 静态/装配断言：调用点计数==1；`ChannelInboundService` 实例无 `RuntimeCoordinator` 引用（仿 `test_step12_6` 装配一致性守护，12.9.3.3 §6） |
| G2 | **intake 永不执行**：`handle_message` 不触发引擎 | 驱动 `handle_message` + spy runtime → 断言 execute **未**调用、PENDING 行已建、返回 200 |
| G3 | **CAS claim 原子性**：并发双 claim 仅 1 个 rowcount=1、仅 1 次执行 | 两线程/进程同 claim 一 PENDING → 断言恰 1 次 execution（adapter spy 计数） |
| G4 | **重复 PENDING 抑制**：同 (channel,external_id) 两次 intake → 恰 1 行 + 两次均 200 | UNIQUE 撞键断言 + 行数==1 |
| G5 | **执行不在事务内**：`RuntimeCoordinator.execute` 运行期无打开 Session/UoW | mock adapter 内尝试 DB 写 → 断言使用独立新 session（防 16s 长事务） |
| G6 | **metadata 往返**：`request_payload` 含 `metadata`；重建 InboundInteraction → 机器人走 `response_url` POST（非 `message/send`） | 构造机器人 inbound → intake → 重建 → deliver → 断言 POST 到 response_url（12.9.3.5 修复守护） |
| G7 | **状态矩阵冻结**：`SUCCEEDED×DELIVERED` / `SUCCEEDED×FAILED` / `FAILED×PENDING` 异步下仍成立 | STAGE_FAILURE_MAP 单测（经 typed-exception 驱动，与执行驱动者无关） |
| G8 | **孤儿 TTL 回收**：`PROCESSING` + 旧 `processing_started_at` 被重新 claim+执行 | 植入老行 → 跑 sweep → 断言重执行（at-least-once） |
| G9 | **导入边界**（既有 `test_import_boundaries.py`）：persistence 唯一可 import SQLAlchemy；`coordinator.py` 不 import Repository/Persistence（:26-27,:65）；api 不触 SQLAlchemy | 既有机器化守护 |

→ **G1/G2 守住「单一执行路径 + intake 不执行」（杜绝双驱动窗口）；G3/G4 守住并发与去重；
   G5 守住事务边界；G6 守住 metadata 缺口；G7/G8 守住状态与崩溃恢复；G9 守住层边界。**

---

## 13. 收口结论

1. **方案**：B（DB-backed Worker）为 MVP 最小正确方案；A 回退、C 延后、D 不可行。
2. **最小性**：复用 8 表 + UoW 短事务 + CAS + UNIQUE；schema 仅 +1~2 nullable 列；
   RuntimeCoordinator / Delivery / 契约**零改动**。
3. **事务边界**：intake = PENDING 插入短事务 → 200；Worker = claim 短事务 → 无事务执行
   (~16s) → outcome 短事务（复用现有三明治）。
4. **语义守恒**：去重（UNIQUE+CAS）、并发（CAS 单序列化点）、Delivery（三组合冻结）、
   崩溃（延迟而非双执行）均在异步下成立。
5. **已知缺口**：`request_payload` 漏存 `inbound.metadata` → 必须随 Worker 一起修复，
   否则机器人 Delivery 静默错投。
6. **迁移安全**：「intake 删 execute + 引入 Worker」为同一原子变更 + 全量重启部署 →
   **不存在双驱动窗口**；最坏情况=延迟非双执行。
7. **Guard**：G1~G9 守住单一执行路径、intake 不执行、CAS 原子、去重、事务边界、metadata
   往返、状态矩阵、孤儿回收、导入边界。

> 以上为最终架构裁决与迁移安全分析，**仅记录、不实现**（12.8.6 / 10.5.3 纪律）。
> 实施动作（Worker 构建、schema 增量、`request_payload` metadata 修复、guard 测试）归入
> 「运行模型就绪清单」，待真实需求触发。
