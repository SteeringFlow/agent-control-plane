"""delivery_recovery：P2 Delivery Lifecycle 的**附加式**三列迁移。

只**加列**，不改任何既有列的语义、类型或约束：

    delivery_attempts    INTEGER  NOT NULL DEFAULT 0   —— 投递 attempt 单调计数
                                                       （= 投递围栏身份）
    delivery_started_at  TIMESTAMPTZ NULL             —— 投递 attempt 租约起点
    delivery_protocol    SMALLINT NOT NULL DEFAULT 0  —— 协议世代
                                                       （0 = 历史/未知协议 ⇒
                                                        禁止自动重投；1 = 新协议）

为什么需要 `delivery_protocol`：`delivery_attempts` 对**历史行**恒为 0，
而「0」对历史行**不能**证明「从未发送」（P2.1 设计修订 R2/R3）——协议世代标记
让「是否新协议」成为**行内可判定**的事实，而不是运维约定。

向后兼容：新增列均带默认值 ⇒ 旧代码（不引用这三列）照常读写；
⚠️ 但「旧代码照常读写」**不等于**「混版安全」——旧代码的 `record_outcome` 会
无条件写 `delivery_status`（可能覆盖新状态）；精确边界与部署顺序见
docs/step-p2-delivery-recovery-implementation.md 的 runbook。

回滚：downgrade 只删这三列 ⇒ **丢失的仅是恢复元数据**；`response_payload` 与
Agent 结果**不受影响**。⚠️ 但当库中已存在 `delivery_protocol=1` 的行时，
**禁止**回滚迁移（会丢失协议世代标记 ⇒ 历史歧义行会被误读为可重投）。

**PostgreSQL 专有**：生产 schema 归 Alembic 管理；OSS SQLite 不使用 Alembic
（其 schema 由 `Base.metadata.create_all` 承担）⇒ **不需要** SQLite 迁移脚本。

Revision ID: 0009
Revises: 0008
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "interactions"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("delivery_attempts", sa.Integer(), nullable=False,
                  server_default="0"),
    )
    op.add_column(
        TABLE,
        sa.Column("delivery_started_at", sa.DateTime(timezone=True),
                  nullable=True),
    )
    op.add_column(
        TABLE,
        sa.Column("delivery_protocol", sa.SmallInteger(), nullable=False,
                  server_default="0"),
    )


def downgrade() -> None:
    op.drop_column(TABLE, "delivery_protocol")
    op.drop_column(TABLE, "delivery_started_at")
    op.drop_column(TABLE, "delivery_attempts")
