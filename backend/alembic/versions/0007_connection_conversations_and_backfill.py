"""connection_conversations + 历史 Session 授权回填（Implementation Package 2 / Step 1）

本迁移落下 **V0 唯一的授权状态存储**：ADR-3 的「Conversation → Connection」
（Package 2-C §1.5 的窄范围例外；由 ADR-11 授权）。

**单事务内按序完成**（原子性是硬要求——「表存在但无授权」的中间态 = 全量 DENY
= 服务中断）：

    1. connections 加 **结构性 supporting UNIQUE**(tenant_id, channel_id, id)
    2. CREATE TABLE connection_conversations（四元组 UNIQUE + 复合 FK）
    3. 索引 ix_connection_conversations_tenant_id / _connection_id
    4. PostgreSQL：ENABLE + FORCE ROW LEVEL SECURITY + tenant_isolation 策略
    5. 历史 Session → 授权回填（DISTINCT；**不按 status 过滤**）
    6. 回填自检（期望数 vs 实得数，不一致即 raise）

RLS 表达式用 **0005 的 NULLIF 形态**（不是 0002 的原始形态）：
0005 的 CORE_TABLES 循环**不会覆盖新表**，新表必须在 0007 里直接写出正确形态。

**回填为什么按租户循环**：`sessions` 自 0002 起 ENABLE + FORCE RLS。若迁移角色
是「受 RLS 约束的表 owner / 非超级用户」，未设 `app.tenant_id` 时策略谓词为 NULL
→ 回填会**静默插入 0 行**，而 0 行 = 全量 DENY 上线（本 Package 点名的最大风险）。
因此回填显式按租户 `set_config('app.tenant_id', <tenant>, true)` 后再读再写，
使回填在「超级用户」与「受 RLS 约束的 owner」两种角色下**都正确**。
每租户回填后立即自检：`期望(DISTINCT eligible sessions) == 实得(本租户授权行数)`，
使「静默 0 行」不可能发生。

回填条件（Package 2-C §5.2 冻结）：

    connection_id IS NOT NULL
    AND external_conversation_id IS NOT NULL
    AND NULLIF(TRIM(external_conversation_id), '') IS NOT NULL

**刻意不加 `status = 'active'`**：V0 的 Session 没有 ended 生命周期
（只有 `create` / `find_active` / `bind_external_session` 三条路径，status 恒为
'active'），因此全部历史 Session 都是回填候选。该假设登记在 ADR-11——
未来引入 ended 时必须**重新裁决**，而不是被本 SQL 的隐含假设悄悄决定。

重复授权：源侧 `DISTINCT`（群聊下同一 conversation 会有多行 sessions——每个成员
一条）+ 目标侧 `ON CONFLICT DO NOTHING`（四元组 UNIQUE 兜底）。

空库 / 新部署：回填为 no-op → 全 DENY，直到显式 Create Grant。这是 ADR-6 的正确
行为（**不得**用通配授权让 rollout「看起来能跑」）。

**仅 PostgreSQL**：`op.create_unique_constraint` / `op.drop_constraint` 在 SQLite
需 batch 模式（与 0002 同一立场）。OSS SQLite 不使用 Alembic——其 schema 由
`Base.metadata.create_all` 承担，新表由 ORM 承载（两条路径不分叉）。

`downgrade` 是**有损**的（丢弃全部授权行），不得当作安全回滚。

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-18
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "connection_conversations"

# 回填资格条件（Package 2-C §5.2 冻结；**不含** status 过滤）。
ELIGIBLE_WHERE = (
    "connection_id IS NOT NULL "
    "AND external_conversation_id IS NOT NULL "
    "AND NULLIF(TRIM(external_conversation_id), '') IS NOT NULL"
)

_BACKFILL_INSERT = (
    "INSERT INTO " + TABLE + " "
    "(tenant_id, channel_id, external_conversation_id, connection_id) "
    "SELECT DISTINCT tenant_id, channel_id, external_conversation_id, connection_id "
    "FROM sessions WHERE {where} ON CONFLICT DO NOTHING"
)

_BACKFILL_EXPECTED = (
    "SELECT count(*) FROM ("
    "SELECT DISTINCT tenant_id, channel_id, external_conversation_id, connection_id "
    "FROM sessions WHERE {where}) AS eligible"
)


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _backfill() -> None:
    """历史 Session → 授权（DISTINCT）。带每租户自检，使静默 0 行不可能发生。"""
    conn = op.get_bind()

    if not _is_postgresql():
        # 无 RLS（SQLite / 本地）：一次回填即可。
        conn.execute(sa.text(_BACKFILL_INSERT.format(where=ELIGIBLE_WHERE)))
        return

    tenant_ids = [
        row[0] for row in conn.execute(sa.text("SELECT id FROM tenants ORDER BY id"))
    ]
    for tenant_id in tenant_ids:
        # 事务级租户上下文：使回填在「受 FORCE RLS 约束的角色」下同样正确。
        conn.execute(
            sa.text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        where = "tenant_id = :tid AND " + ELIGIBLE_WHERE
        expected = conn.execute(
            sa.text(_BACKFILL_EXPECTED.format(where=where)), {"tid": tenant_id}
        ).scalar_one()
        conn.execute(
            sa.text(_BACKFILL_INSERT.format(where=where)), {"tid": tenant_id}
        )
        actual = conn.execute(
            sa.text("SELECT count(*) FROM " + TABLE + " WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        ).scalar_one()
        if actual != expected:
            raise RuntimeError(
                "授权回填自检失败：tenant_id=%s 期望 %s 行、实得 %s 行——"
                "回填不得静默丢行（表刚创建，二者必须相等）"
                % (tenant_id, expected, actual)
            )


def upgrade() -> None:
    # 1. connections 的 supporting UNIQUE（纯结构性，让复合 FK 有合法 target）。
    #    id 已是 PK，因此本约束对既有数据**不可能失败**。
    op.create_unique_constraint(
        "uq_connections_tenant_channel_id", "connections",
        ["tenant_id", "channel_id", "id"],
    )

    # 2. 授权状态表
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("external_conversation_id", sa.String(length=255), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "channel_id", "external_conversation_id", "connection_id",
            name="uq_connection_conversations_tenant_channel_conv_conn"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_connection_conversations_tenant_id"),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channels.id"],
            name="fk_connection_conversations_channel_id"),
        # 复合 FK：一个约束同时锁住「跨租户」与「同租户跨 channel」两种错配。
        # Connection 删除为阻断式（无 ondelete 子句 = NO ACTION）——不 cascade。
        sa.ForeignKeyConstraint(
            ["tenant_id", "channel_id", "connection_id"],
            ["connections.tenant_id", "connections.channel_id", "connections.id"],
            name="fk_connection_conversations_tenant_channel_connection"),
    )

    # 3. 索引：tenant_id（RLS 扫描纪律）+ connection_id（List by Connection 读路径）。
    #    (tenant_id, channel_id, external_conversation_id) 由四元组 UNIQUE 的
    #    前导列覆盖，不重复建。
    op.create_index(f"ix_{TABLE}_tenant_id", TABLE, ["tenant_id"], unique=False)
    op.create_index(f"ix_{TABLE}_connection_id", TABLE, ["connection_id"], unique=False)

    # 4. 第二层防线：PostgreSQL RLS（用 0005 的 NULLIF 形态）
    if _is_postgresql():
        expr = "NULLIF(current_setting('app.tenant_id', true), '')::integer"
        op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {TABLE} "
            f"USING (tenant_id = {expr}) WITH CHECK (tenant_id = {expr})"
        )

    # 5. + 6. 回填与自检（同一事务）
    _backfill()


def downgrade() -> None:
    """**有损**：丢弃全部授权行（新部署回到「全 DENY」，需重新授权）。"""
    if _is_postgresql():
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
        op.execute(f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY")

    op.drop_index(f"ix_{TABLE}_connection_id", table_name=TABLE)
    op.drop_index(f"ix_{TABLE}_tenant_id", table_name=TABLE)
    op.drop_table(TABLE)

    op.drop_constraint("uq_connections_tenant_channel_id", "connections", type_="unique")
