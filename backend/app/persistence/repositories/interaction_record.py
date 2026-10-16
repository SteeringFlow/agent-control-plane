"""SQLAlchemy InteractionRecordRepository 实现（Adapter，Step 10.4.2）。

Session 由调用方注入；本层不 commit（事务边界属 UoW）。

生命周期只走 Port 冻结的显式跃迁（create / claim_processing /
record_outcome / reclaim_stale_processing——12.9.4.12 起增加孤儿回收）——
不提供任意字段 update；读路径 = get_by_id +
find_by_channel_and_external_id（10.5.2 Interaction Identity）。
"""

from datetime import datetime, timedelta

from sqlalchemy import Interval, func, literal, select, update

from app.domain.interaction_record import (
    PROCESSING_PENDING,
    PROCESSING_PROCESSING,
    InteractionRecord,
    OutcomeCommit,
)
from app.persistence.mappers.interaction_record import to_domain, to_orm
from app.persistence.models.interaction import InteractionModel
from app.repositories.interaction_record import InteractionRecordRepository


class SqlAlchemyInteractionRecordRepository:
    """实现 InteractionRecordRepository 协议。"""

    def __init__(self, session, tenant_id: int | None = None) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def create(self, record: InteractionRecord) -> InteractionRecord:
        row = to_orm(record)
        if self._tenant_id is not None:
            row.tenant_id = self._tenant_id
        self._session.add(row)
        self._session.flush()
        return to_domain(row)

    def claim_processing(self, interaction_id: int) -> bool:
        """CAS 原子 claim：仅当该行仍是 PENDING 时才跃迁到 PROCESSING。

        单条条件化 UPDATE 同时完成「判断 + 写入」，并发下由数据库保证只有一个调用方
        拿到 rowcount=1。刻意不用「先 `_require` 读回再赋值」的非原子读-改-写
        （那正是 12.9.3.2 §3 的双执行来源），也不用 `SELECT ... FOR UPDATE`
        （SQLite 不支持，且会把锁持有到执行结束）。

        12.9.4.12 Attempt Fence：同一语句内写入 `processing_started_at =
        func.now()`（**DB 时钟**，12.9.4.11 §4 铁律——app 不生成 claim 时间戳）。
        该值随后被 Worker 读回持有，成为 outcome 围栏的等式值。
        """
        conditions = [
            InteractionModel.id == interaction_id,
            InteractionModel.processing_status == PROCESSING_PENDING,
        ]
        if self._tenant_id is not None:
            conditions.append(InteractionModel.tenant_id == self._tenant_id)
        result = self._session.execute(
            update(InteractionModel)
            .where(*conditions)
            .values(
                processing_status=PROCESSING_PROCESSING,
                processing_started_at=func.now(),
            )
        )
        return result.rowcount == 1

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
    ) -> OutcomeCommit:
        """终态一次性落定（12.9.4.12 起为条件化 UPDATE，返回 OutcomeCommit）。

        - `session_id` 仅在已知时回填（None = 保持原值，不覆盖）——
          10.8.4 起直接来自 Runtime 执行结果（薄结果契约），不再由 wrapper
          用弱键查询推导；
        - `response_payload` 仅在成功路径给出（10.8.4：AgentResponse 的
          原样快照 `{content, external_session_id, metadata}`，10.7.2 B 裁决）；
          None = 不覆盖（失败路径没有响应，保持 NULL——不可伪造）。

        **Attempt Fence（12.9.4.11 §2）**：`expected_started_at` 非 None 时，
        追加条件 `processing_status='processing' AND processing_started_at =
        :expected`（等式两侧同为 DB 值：列 vs claim 后读回值）——

        - rowcount == 1 → `OutcomeCommit.COMMITTED`（仍持有 ownership）；
        - rowcount == 0 → `OutcomeCommit.REJECTED`（ownership lost：旧 attempt
          被 reclaim 后新 attempt 已重新 claim，或行已落终态）。**正常竞争结果，
          不是 DB 错误，不抛异常**（真实 DB 故障仍照常抛）。

        `expected_started_at = None` = 未持围栏的既有调用形状（同步/种子路径
        直接落终态），退化为仅按 id 的条件更新——异步执行路径（Worker）必须
        传 fence 值，绝不走 None。
        """
        values: dict = {
            "processing_status": processing_status,
            "delivery_status": delivery_status,
            "failure_stage": failure_stage,
            "failure_code": failure_code,
            "retryable": retryable,
        }
        if session_id is not None:  # 仅回填真正观察到的事实，None = 保持原值
            values["session_id"] = session_id
        if response_payload is not None:   # 仅成功路径写入；失败路径保持 NULL
            values["response_payload"] = response_payload
        outcome_conditions = [InteractionModel.id == interaction_id]
        if self._tenant_id is not None:
            outcome_conditions.append(
                InteractionModel.tenant_id == self._tenant_id)
        stmt = update(InteractionModel).where(*outcome_conditions)
        if expected_started_at is not None:
            stmt = stmt.where(
                InteractionModel.processing_status == PROCESSING_PROCESSING,
                InteractionModel.processing_started_at == expected_started_at,
            )
        result = self._session.execute(stmt.values(**values))
        return (OutcomeCommit.COMMITTED if result.rowcount == 1
                else OutcomeCommit.REJECTED)

    def reclaim_stale_processing(
        self, threshold_seconds: int, limit: int = 100,
    ) -> int:
        """PROCESSING 孤儿回收（12.9.4.12）：超时未落终态的行退回 PENDING。

        单条条件化 UPDATE（子查询有界），条件：

            status = 'processing'
            AND processing_started_at IS NOT NULL
            AND processing_started_at < datetime('now', '-threshold seconds')

        更新：`processing_status = 'pending'`、`processing_started_at = NULL`
        （12.9.4.11 D.2：reclaim 必置 NULL——这是围栏等式在新旧 attempt 之间
        不碰撞的前提）。时间语义与 claim 同源：cutoff 由 **DB** 计算
        （`datetime('now', ...)`），app 只传 threshold 间隔，绝不传自己算出的
        绝对时刻（12.9.4.11 §4.3）。返回本批回收的行数。

        注意：本方法**只回收**，不 claim、不执行——回收后的行重新进入
        `find_pending` 池，由既有 claim（CAS）+ Worker 流程接管。

        方言（Cloud-0.1.1-A）：cutoff 仍由 **DB 时钟**计算（12.9.4.11 §4 铁律，
        app 只传阈值间隔、绝不传自己算出的绝对时刻）。此处仅把 SQLite 专有的
        `datetime('now', '-N seconds')` 换成按 dialect 选择的**等价**表达式——
        不改变既有语义（只修方言兼容性）。
        """
        cutoff = self._stale_cutoff(threshold_seconds)
        stale_conditions = [
            InteractionModel.processing_status == PROCESSING_PROCESSING,
            InteractionModel.processing_started_at.is_not(None),
            InteractionModel.processing_started_at < cutoff,
        ]
        if self._tenant_id is not None:
            stale_conditions.append(InteractionModel.tenant_id == self._tenant_id)
        stale_ids = (
            select(InteractionModel.id)
            .where(*stale_conditions)
            .limit(limit)
        )
        result = self._session.execute(
            update(InteractionModel)
            .where(InteractionModel.id.in_(stale_ids))
            .values(
                processing_status=PROCESSING_PENDING,
                processing_started_at=None,
            )
        )
        return result.rowcount

    def _stale_cutoff(self, threshold_seconds: int):
        """stale 判定 cutoff（由 DB 时钟计算；按方言给出等价表达式）。

        - sqlite：`datetime('now', '-N seconds')`——**原样保留**（OSS/local
          profile 行为逐字节不变）；
        - 其它（PostgreSQL）：`now() - :interval`——绑定参数为**相对间隔**
          （`timedelta` → PG interval），类型稳定（timestamptz − interval →
          timestamptz），不使用任何裸 SQL。

        两者都满足「cutoff 由 DB 计算、app 只传阈值间隔」的冻结语义；本方法
        不引入任何 app 侧绝对时刻。
        """
        seconds = int(threshold_seconds)
        dialect = self._session.get_bind().dialect.name
        if dialect == "sqlite":
            return func.datetime("now", f"-{seconds} seconds")
        return func.now() - literal(timedelta(seconds=seconds), type_=Interval)

    def get_by_id(self, interaction_id: int) -> InteractionRecord | None:
        stmt = select(InteractionModel).where(
            InteractionModel.id == interaction_id)
        if self._tenant_id is not None:
            stmt = stmt.where(InteractionModel.tenant_id == self._tenant_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    def find_by_channel_and_external_id(
        self, channel_id: int, external_interaction_id: str
    ) -> InteractionRecord | None:
        """Interaction Identity 精确查询（10.5.2 物化的读路径）。"""
        stmt = select(InteractionModel).where(
            InteractionModel.channel_id == channel_id,
            InteractionModel.external_interaction_id == external_interaction_id,
        )
        if self._tenant_id is not None:
            stmt = stmt.where(InteractionModel.tenant_id == self._tenant_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    def find_pending(self, limit: int = 10) -> list[InteractionRecord]:
        """当前 PENDING 的 Interaction（纯读，不 claim、不改状态、不加锁）。

        FIFO：`created_at` 升序；`created_at` 相同时以 `id` 升序保序——SQLite 的
        `CURRENT_TIMESTAMP` 是秒级精度，同秒创建的多行若不定序，pickup 顺序就会
        在两次轮询之间漂移（对重试/孤儿回收都是不可预期的行为）。
        """
        stmt = select(InteractionModel).where(
            InteractionModel.processing_status == PROCESSING_PENDING)
        if self._tenant_id is not None:
            stmt = stmt.where(InteractionModel.tenant_id == self._tenant_id)
        rows = self._session.execute(
            stmt.order_by(InteractionModel.created_at, InteractionModel.id).limit(limit)
        ).scalars().all()
        return [to_domain(row) for row in rows]

    def list_recent(self, limit: int = 50) -> list[InteractionRecord]:
        """最近 N 条 Interaction（13.1 Observability，纯读：单一 select，零写入）。

        最新在前：`created_at` 降序、同秒以 `id` 降序保序（秒级精度下的稳定
        次序）。返回记录携带原生双状态 / 失败三元组 / DB 时间戳——不在读路径
        合成任何派生状态。
        """
        stmt = select(InteractionModel)
        if self._tenant_id is not None:
            stmt = stmt.where(InteractionModel.tenant_id == self._tenant_id)
        rows = self._session.execute(
            stmt.order_by(
                InteractionModel.created_at.desc(), InteractionModel.id.desc()
            ).limit(limit)
        ).scalars().all()
        return [to_domain(row) for row in rows]


# 协议一致性自检（Port 形状；不做运行时开销）
_ = InteractionRecordRepository
