"""bootstrap_arms（M6.6-C3 / ADR-12 —— Bootstrap Arm 持久化）

本迁移落下 **Bootstrap Arm 的窗口存储**：已认证 Tenant Admin 显式 Verify Connection
产生的「有界、一次性、精准作用域、可归属、可审计」的授权建立许可（ADR-12）。

它不是授权状态存储（授权仍只有 `connection_conversations` 一张表，ADR-11 §4 的
窄豁免范围**不扩张**）；本表**不参与** `find_granted_connection_ids`。

**单事务内按序完成**（原子性是硬要求）：

    1. CREATE TABLE bootstrap_arms
    2. 部分唯一索引 UNIQUE(tenant_id, channel_id, connection_id) WHERE status='armed'
       ← **O-1 的权威层**：同一 scope 至多一条 ARMED 由 **DB** 保证
    3. 索引 ix_bootstrap_arms_tenant_id（RLS 扫描纪律）/ _connection_id
    4. PostgreSQL：ENABLE + FORCE ROW LEVEL SECURITY + tenant_isolation 策略

RLS 表达式用 **0005 的 NULLIF 形态**（不是 0002 的原始形态）：0005 的 CORE_TABLES
循环**不会覆盖新表**，新表必须在 0008 里直接写出正确形态。

**无 backfill**：历史数据里不存在「已 arm」事实——回填等于凭空创造授权窗口。

**仅 PostgreSQL**：`op.create_index(..., postgresql_where=...)` 在 SQLite 需 batch
模式；且 OSS SQLite **不使用 Alembic**——其 schema 由 `Base.metadata.create_all`
承担（ORM 侧已带 `sqlite_where` 的同一条部分唯一索引，两条路径不分叉，
见 `0007:44-46` 与 `app/persistence/models/bootstrap_arm.py`）。

`downgrade` 是**有损**的（丢弃全部 Arm 行），不得当作安全回滚。

Revision ID: 0008
Revises: 0007
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "bootstrap_arms"
PARTIAL_UNIQUE_INDEX = "uq_bootstrap_arms_scope_armed"


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # 1. 窗口表
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False,
                  server_default="armed"),
        # NOT NULL：Arm 的合法性组成部分（O-4）——无 anonymous / guest / system。
        sa.Column("initiator_user_id", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_interaction_id", sa.Integer(), nullable=True),
        sa.Column("consumed_external_identity_id", sa.String(length=255),
                  nullable=True),
        sa.Column("consumed_external_conversation_id", sa.String(length=255),
                  nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_bootstrap_arms_tenant_id"),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"],
                                name="fk_bootstrap_arms_channel_id"),
        sa.ForeignKeyConstraint(["initiator_user_id"], ["users.id"],
                                name="fk_bootstrap_arms_initiator_user_id"),
        # 复合 FK：一个约束同时锁住「跨租户」与「同租户跨 channel」两种 scope 错配
        # （与 connection_conversations 同形；target 是 connections 的 supporting
        # UNIQUE(tenant_id, channel_id, id)，由 0007 建立）。
        sa.ForeignKeyConstraint(
            ["tenant_id", "channel_id", "connection_id"],
            ["connections.tenant_id", "connections.channel_id", "connections.id"],
            name="fk_bootstrap_arms_tenant_channel_connection",
        ),
    )

    # 2. O-1：部分唯一索引 —— 同一 scope 至多一条 ARMED（**数据库为权威**）
    op.create_index(
        PARTIAL_UNIQUE_INDEX, TABLE,
        ["tenant_id", "channel_id", "connection_id"],
        unique=True,
        postgresql_where=sa.text("status = 'armed'"),
        sqlite_where=sa.text("status = 'armed'"),
    )

    # 3. 索引：tenant_id（RLS 扫描纪律）+ connection_id（读路径）
    op.create_index(f"ix_{TABLE}_tenant_id", TABLE, ["tenant_id"], unique=False)
    op.create_index(f"ix_{TABLE}_connection_id", TABLE, ["connection_id"],
                    unique=False)

    # 4. 第二层防线：PostgreSQL RLS（用 0005 的 NULLIF 形态）
    if _is_postgresql():
        expr = "NULLIF(current_setting('app.tenant_id', true), '')::integer"
        op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {TABLE} "
            f"USING (tenant_id = {expr}) WITH CHECK (tenant_id = {expr})"
        )


def downgrade() -> None:
    """**有损**：丢弃全部 Arm 行（回到「无 Arm ⇒ Fail Closed」，可重新验证）。"""
    if _is_postgresql():
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
        op.execute(f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY")

    op.drop_index(f"ix_{TABLE}_connection_id", table_name=TABLE)
    op.drop_index(f"ix_{TABLE}_tenant_id", table_name=TABLE)
    op.drop_index(PARTIAL_UNIQUE_INDEX, table_name=TABLE)
    op.drop_table(TABLE)
