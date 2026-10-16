"""SQLAlchemy BootstrapArmRepository 实现（Adapter，M6.6-C3 / ADR-12）。

Session 由调用方注入；本层不 commit（事务边界属 5.5 Unit of Work）。
Cloud-0.1.1-B：可选 `tenant_id` → 显式租户过滤（第一层防线；第二层是 RLS）。

**所有时间判据一律由 DB 时钟给出**（12.9.4.11 §4 铁律）：
app 只传**间隔**，绝不传自己算出的绝对时刻。过期判定与消费写入写在**同一条 SQL**
内 ⇒ 无 TOCTOU、跨 worker / 跨实例语义一致。
"""

from datetime import timedelta

from sqlalchemy import Interval, func, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.bootstrap_arm import STATUS_ARMED, STATUS_REVOKED, BootstrapArm
from app.persistence.mappers.bootstrap_arm import to_domain, to_orm
from app.persistence.models.bootstrap_arm import BootstrapArmModel
from app.repositories.bootstrap_arm import BootstrapArmRepository


class SqlAlchemyBootstrapArmRepository:
    """实现 BootstrapArmRepository 协议。"""

    #: O-1 部分唯一索引的冲突重试次数（1 次重试：revoke → insert）。
    _ARM_ATTEMPTS = 2

    def __init__(self, session: Session, tenant_id: int | None = None) -> None:
        self._session = session
        self._tenant_id = tenant_id

    # ------------------------------------------------------------------ 写入

    def arm(self, record: BootstrapArm, *, ttl_seconds: int) -> BootstrapArm:
        """supersede：同事务内 revoke 该 scope 上既有 `armed` → 插入新 `armed`。"""
        last_error: IntegrityError | None = None
        for _ in range(self._ARM_ATTEMPTS):
            try:
                self._revoke_active(record.channel_id, record.connection_id)
                row = to_orm(record)
                if self._tenant_id is not None:
                    row.tenant_id = self._tenant_id
                # DB 时钟：app 只给间隔（与 _stale_cutoff 对称的方言表达式）。
                row.expires_at = self._expiry_expr(ttl_seconds)
                self._session.add(row)
                self._session.flush()
                return to_domain(row)
            except IntegrityError as exc:  # 并发双击撞 O-1 部分唯一索引
                last_error = exc
                # 丢弃本轮（含 revoke），回到干净事务后重试一次。
                self._session.rollback()
                continue
        assert last_error is not None
        raise last_error

    def consume(
        self,
        arm_id: int,
        *,
        interaction_id: int,
        external_identity_id: str,
        external_conversation_id: str,
    ) -> bool:
        """CAS 原子消费：`armed` 且未过期 → `consumed`（rowcount == 1 即赢家）。"""
        conditions = [
            BootstrapArmModel.id == arm_id,
            BootstrapArmModel.status == STATUS_ARMED,
            BootstrapArmModel.expires_at > self._now_expr(),
        ]
        if self._tenant_id is not None:
            conditions.append(BootstrapArmModel.tenant_id == self._tenant_id)
        result = self._session.execute(
            update(BootstrapArmModel)
            .where(*conditions)
            .values(
                status="consumed",
                consumed_at=self._now_expr(),
                consumed_by_interaction_id=interaction_id,
                consumed_external_identity_id=external_identity_id,
                consumed_external_conversation_id=external_conversation_id,
            )
        )
        return result.rowcount == 1

    # ------------------------------------------------------------------ 读取

    def find_active_by_channel(self, channel_id: int) -> BootstrapArm | None:
        conditions = [
            BootstrapArmModel.channel_id == channel_id,
            BootstrapArmModel.status == STATUS_ARMED,
            BootstrapArmModel.expires_at > self._now_expr(),
        ]
        if self._tenant_id is not None:
            conditions.append(BootstrapArmModel.tenant_id == self._tenant_id)
        row = self._session.execute(
            select(BootstrapArmModel)
            .where(*conditions)
            .order_by(BootstrapArmModel.id)
            .limit(1)
        ).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    def get_by_id(self, arm_id: int) -> BootstrapArm | None:
        if self._tenant_id is None:
            row = self._session.get(BootstrapArmModel, arm_id)
        else:
            row = self._session.execute(
                select(BootstrapArmModel).where(
                    BootstrapArmModel.id == arm_id,
                    BootstrapArmModel.tenant_id == self._tenant_id,
                )
            ).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    # ------------------------------------------------------------------ 内部

    def _revoke_active(self, channel_id: int, connection_id: int) -> int:
        """把该 scope 上全部 `armed` 行置为 `revoked`（**不过滤过期**）。

        ⚠️ 刻意**不加** `expires_at > now()`：O-1 的部分唯一索引覆盖的是
        `status='armed'`（与过期无关），若只 revoke 未过期行，一条已过期但仍为
        `armed` 的旧行会让新 Arm 的插入永久撞约束。
        """
        conditions = [
            BootstrapArmModel.channel_id == channel_id,
            BootstrapArmModel.connection_id == connection_id,
            BootstrapArmModel.status == STATUS_ARMED,
        ]
        if self._tenant_id is not None:
            conditions.append(BootstrapArmModel.tenant_id == self._tenant_id)
        result = self._session.execute(
            update(BootstrapArmModel)
            .where(*conditions)
            .values(status=STATUS_REVOKED)
        )
        return result.rowcount

    def _dialect(self) -> str:
        return self._session.get_bind().dialect.name

    def _now_expr(self):
        """当前时刻（**DB 计算**）；方言等价表达式。

        - sqlite：`datetime('now')` —— 与 `expires_at` 的写入表达式同格式；
        - 其它（PostgreSQL）：`now()`。
        """
        if self._dialect() == "sqlite":
            return func.datetime("now")
        return func.now()

    def _expiry_expr(self, ttl_seconds: int):
        """`expires_at` 写入表达式（由 **DB 时钟**计算；app 只传间隔）。

        - sqlite：`datetime('now', '+N seconds')`；
        - 其它（PostgreSQL）：`now() + interval N seconds`（绑定参数为相对间隔）。
        """
        seconds = int(ttl_seconds)
        if self._dialect() == "sqlite":
            # 显式带符号：负间隔（测试用「一出生即过期」）也必须产生合法修饰符。
            return func.datetime("now", f"{seconds:+d} seconds")
        return func.now() + literal(timedelta(seconds=seconds), type_=Interval)
