"""InteractionRecordRepository 端口（Port，Step 10.4.2）。

只描述 Interaction 记录的生命周期能力，不含实现；实现见
app/persistence/repositories/interaction_record.py。

方法面按 10.4.2 冻结的最小生命周期设计：

    create                            → PENDING 行（Evidence 起点）
    claim_processing                  → PROCESSING（CAS 原子 claim，返回是否抢到；
                                        12.9.4.12 起同语句写入 DB 时钟的
                                        processing_started_at）
    record_outcome                    → 终态一次性落定（SUCCEEDED/FAILED ×
                                        DELIVERED/FAILED/PENDING + failure
                                        三元组 + session 回填 +
                                        response_payload（10.8.4）；
                                        12.9.4.12 起带 attempt fence，
                                        返回 OutcomeCommit）
    reclaim_stale_processing          → PROCESSING 孤儿超时回收 → PENDING
                                        （12.9.4.12；有界、单条条件化 UPDATE）
    get_by_id                         → 按主键读回（断言/诊断）
    find_by_channel_and_external_id   → 按 Interaction Identity 精确读回
                                        （10.5.2 物化）
    find_pending                      → 当前 PENDING 的 Interaction（Worker
                                        pickup：**纯读、不 claim**、有界上限）

刻意不做：update 任意字段（生命周期状态只走上述显式跃迁）、delete、
**任何「命中已存在即返回」的幂等/去重决策**——identity 的消费方式属
Execution semantics 裁决。

13.1 起：`list_recent` 是本 Port 的第一个陈列读方法（10.7.1 §Q4 记录的
触发条件达成——Observability Read Vertical Slice 出现真实消费者）；
仍不做游标 / 翻页 / 任意条件过滤——只有 bounded limit，与 find_pending 同纪律。
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.domain.interaction_record import InteractionRecord, OutcomeCommit


@runtime_checkable
class InteractionRecordRepository(Protocol):
    """Interaction 记录仓储：生命周期语义方法，不是 Generic CRUD。"""

    def create(self, record: InteractionRecord) -> InteractionRecord:
        """写入一条 PENDING 记录并返回带 id 的新实例（事务边界归 UoW）。"""
        ...

    def claim_processing(self, interaction_id: int) -> bool:
        """PENDING → PROCESSING（CAS 原子 claim）。

        条件化跃迁 `UPDATE ... WHERE id=:id AND processing_status='pending'`，
        12.9.4.12 起同一语句写入 `processing_started_at = DB now`（fence 值，
        由调用方 claim 成功后经 `get_by_id` 读回持有——app 绝不自己生成时间戳）：
        - True  = 本调用方把该行从 PENDING 抢到 PROCESSING，取得执行权；
        - False = 该行已不是 PENDING（他人已 claim / 已是终态）→ 调用方**不得**
          执行，只应停止（无条件赋值会让两个 Worker 双执行，12.9.3.2 §3）。
        """
        ...

    def record_outcome(
        self,
        interaction_id: int,
        *,
        processing_status: str,
        delivery_status: str,
        failure_stage: str | None = None,
        failure_code: str | None = None,
        retryable: bool = False,
        session_id: int | None = None,
        response_payload: dict | None = None,
        expected_started_at: datetime | None = None,
        expected_processing_status: str | None = None,
        expected_delivery_status: str | None = None,
        expected_delivery_attempts: int | None = None,
        delivery_protocol: int | None = None,
    ) -> OutcomeCommit:
        """终态一次性落定（12.9.4.12 起带 attempt fence，返回 OutcomeCommit）。

        - `session_id` 仅在已知时回填（None = 保持原值，不覆盖）——
          10.8.4 起直接来自 Runtime 执行结果（薄结果契约），不再由 wrapper
          用弱键查询推导；
        - `response_payload` 仅在成功路径给出（10.8.4：AgentResponse 的
          原样快照 `{content, external_session_id, metadata}`，10.7.2 B 裁决）；
          None = 不覆盖（失败路径没有响应，保持 NULL——不可伪造）；
        - `expected_started_at`：本 attempt 在 claim 后读回持有的
          `processing_started_at`。非 None 时以
          `WHERE processing_status='processing' AND processing_started_at=:expected`
          作围栏——命中返回 `OutcomeCommit.COMMITTED`；失配返回
          `OutcomeCommit.REJECTED`（ownership lost，正常竞争结果，**不抛异常**；
          真实 DB 错误仍照常抛）。None = 未持围栏的既有形状（同步/种子路径）。
        - **`expected_processing_status` / `expected_delivery_status`（P1 / C3）**：
          围栏的**状态侧**条件，缺省行为与既有调用**逐字不变**
          （`expected_processing_status` 缺省 = `'processing'`；
          `expected_delivery_status` 缺省 = 不加该条件）。

          它们的用途只有一个：让**投递终态**能被**带围栏**地单独落定——
          投递终态发生时，`processing_status` 已经是 `'succeeded'`（Agent 已
          成功），因此围栏的状态侧必须改为 `'succeeded'`，并且额外要求
          `delivery_status = 'pending'`（投递终态**只落一次**）。

          显式给出这两个条件，就不必为「投递终态」另开一个 Port 方法
          （Port 的**方法面**是冻结的：10.4.2 / 10.5.3）。

          **P2**：投递终态改由 `expected_delivery_attempts` 围栏——它同时
          要求 `delivery_status='delivering'` 与 `delivery_attempts=我的
          attempt 号`（**单调计数**，不用时间戳等式：SQLite 秒级精度会碰撞）。
          给出该参数时，写入同时**清空** `delivery_started_at`（本次 attempt
          已结束 ⇒ 租约释放 ⇒ 失败重投不必再等租约过期）。
        - `delivery_protocol`：协议世代标记；非 None 时写入（1 = 新协议行，
          其 `delivery_attempts=0` 才**可**被读作「从未尝试过投递」）。
        """
        ...

    def claim_delivery(
        self, interaction_id: int, *, max_attempts: int, lease_seconds: int,
        include_failed: bool = False,
    ) -> int | None:
        """**原子**取得投递权（单条条件化 UPDATE；P2 Delivery Lifecycle）。

        这是「首次投递」与「失败后重投」**共用**的唯一投递闸门：没有成功
        claim，调用方**不得**发起任何外部投递。

        条件（全部在同一条 UPDATE 内校验）：
        - `id` + `tenant_id`（若该仓储已绑定租户）；
        - `processing_status='succeeded'`（Agent 已成功）；
        - `response_payload IS NOT NULL`（结果已持久化）；
        - `delivery_protocol=1`（**协议世代**：历史行 `0` 一律不可 claim ⇒
          永不自动重投）；
        - `delivery_attempts < max_attempts`（总 attempt 上限）；
        - `include_failed=False`（首次投递）：`delivery_status='pending'`
          且 `delivery_attempts=0`（**可证明从未尝试过投递**）；
        - `include_failed=True`（失败重投）：`delivery_status='failed'` 且
          `retryable=true`（**结果已知**的未送达；`retryable` 单独**不**充分）
          且租约已释放/过期。

        同一语句写入：`delivery_status='delivering'`、
        `delivery_attempts=delivery_attempts+1`、`delivery_started_at=DB now`。

        返回：**本次 attempt 号**（= 更新后的 `delivery_attempts`，与 UPDATE
        在同一事务内读回）；`None` = 未取得投递权（rowcount != 1）——调用方
        **必须停止投递**（这是正常竞争结果，不抛异常）。
        """
        ...

    def find_undelivered(self, limit: int = 10, *,
                         max_attempts: int) -> list[InteractionRecord]:
        """恢复候选（**纯读**：不 claim、不改行、不加锁）。

        白名单（两条，**不含** `unknown` / `delivering` / `delivered`）：
        - 首次投递：`protocol=1 AND delivery_status='pending' AND attempts=0`；
        - 失败重投：`protocol=1 AND delivery_status='failed' AND retryable=true`
          且 `attempts < max_attempts`。
        """
        ...

    def reclaim_stale_delivery(self, threshold_seconds: int,
                               limit: int = 100) -> int:
        """回收**租约过期**的在途投递 → `delivery_status='unknown'`。

        ⚠️ 回收**绝不**退回 `pending`：租约过期只说明「旧 attempt 失去数据库
        写入资格」，**不**说明「消息未送达」——旧 attempt 可能仍在外部调用中、
        甚至已经送达。退回 `pending` 会让恢复驱动重发 ⇒ 重复用户消息。
        因此过期一律转 `unknown`（**永不自动重投**，转人工裁决）。
        """
        ...

    def reclaim_stale_processing(
        self, threshold_seconds: int, limit: int = 100,
    ) -> int:
        """PROCESSING 孤儿回收（12.9.4.12）：超时未落终态 → 退回 PENDING。

        单条条件化 UPDATE（子查询有界）：

            status='processing' AND processing_started_at IS NOT NULL
            AND processing_started_at < DB_NOW - threshold_seconds

        命中行：`processing_status='pending'`、`processing_started_at=NULL`
        （reclaim 必置 NULL——围栏等式不碰撞的前提，12.9.4.11 §4.4）。
        cutoff 由 **DB** 计算（threshold 作为间隔传入，app 不传自己算出的
        绝对时刻）。返回本批回收行数。只回收、不 claim、不执行。
        """
        ...

    def get_by_id(self, interaction_id: int) -> InteractionRecord | None:
        """按主键取记录；不存在返回 None。"""
        ...

    def find_by_channel_and_external_id(
        self, channel_id: int, external_interaction_id: str
    ) -> InteractionRecord | None:
        """按 Interaction Identity 精确取记录；不存在返回 None（10.5.2）。

        身份 = `(channel_id, external_interaction_id)`——与
        `IdentityRepository.find_by_channel_and_external_id` 同构，延续
        「所有外部世界产生的 ID 都必须在 Channel Scope 内解释」纪律：
        不同 Channel 的同名 external id 是两次不同交互，互不干扰。

        本方法只回答「是否已存在」，绝不内嵌「命中后怎么办」的语义。
        """
        ...

    def find_pending(self, limit: int = 10) -> list[InteractionRecord]:
        """读取**当前** PENDING 的 Interaction（Worker pickup）。

        语义严格限定（12.9.4.4）：

        - **纯读**：不修改任何行、不产生 claim、不做任何状态跃迁；调用它不会
          让任何一行「归我」；
        - **有界**：`limit` 是固定上限，不是翻页参数——本 Port 仍不提供游标 / 翻页
          或任意条件过滤；
        - **顺序**：`created_at` 升序（FIFO）。`created_at` 相同时以 `id` 升序保序
          （SQLite `CURRENT_TIMESTAMP` 是秒级精度，同秒多行必须有稳定次序）。

        **pickup ≠ 执行权**：并发 Worker 可能读到同一批 PENDING（重叠是设计允许
        的），唯一性由随后的 `claim_processing`（CAS）保证——本方法刻意不含任何
        锁定语义，不使用 `FOR UPDATE` / `SKIP LOCKED`（SQLite 不支持，且把锁持有
        到执行结束会让 ~16s 执行阻塞全表）。
        """
        ...

    def list_recent(self, limit: int = 50) -> list[InteractionRecord]:
        """最近 N 条 Interaction（13.1 Observability 读面，**纯读**）。

        语义严格限定（与 find_pending 同纪律）：

        - **纯读**：不修改任何行、不做任何状态跃迁、不加锁——读它不改变任何
          Interaction 的 processing / delivery 状态，也不触碰
          processing_started_at（attempt fence 事实）；
        - **有界**：`limit` 是固定上限，不是翻页参数——不提供游标 / 翻页 /
          任意条件过滤（10.7.1 §Q5：无多维查询用例不扩方法面）；
        - **顺序**：`created_at` 降序（最新在前，管理面陈列的自然方向）；
          `created_at` 相同时以 `id` 降序保序（SQLite `CURRENT_TIMESTAMP`
          秒级精度，同秒多行必须有稳定次序）；
        - **不重新解释状态**：返回的记录携带原生双状态（processing × delivery）、
          失败三元组与 DB 时间戳——「Agent 成功 × 投递失败」这类合法组合
          原样呈现，绝不在读路径合成单一 status。
        """
        ...
