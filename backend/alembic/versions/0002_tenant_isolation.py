"""tenant isolation — tenants root + tenant_id + UNIQUE(tenant_id,...) + RLS (Cloud-0.1.1-B)

在基线 0001（8 张业务表）之上引入租户隔离：

1. `CREATE TABLE tenants` + 种子 bootstrap Tenant #1；
2. 8 张业务表各加 `tenant_id INTEGER NOT NULL REFERENCES tenants(id) DEFAULT 1`；
3. 3 个复合唯一约束演进为 `(tenant_id, ...)` 前导；`sessions` 新增复合唯一约束；
4. 各表 `ix_<t>_tenant_id`；
5. PostgreSQL：`ENABLE/FORCE ROW LEVEL SECURITY` + per-tenant policy（含 `tenants`
   特殊 bootstrap 策略）——第二层防线；
6. PostgreSQL：回填后 `DROP DEFAULT`，强制显式 tenant（防御性）。

RLS 与 `DROP DEFAULT` 仅对 PostgreSQL 生效（本地 SQLite 保留 default=1 单租户兼容，
见实现边界 §2.1/§11）。本迁移面向 **Cloud PostgreSQL**（与 0001 同定位）——UNIQUE
约束演进使用 `DROP CONSTRAINT`，SQLite 需 batch 模式方可支持；OSS SQLite 不使用
Alembic（走 `create_all`）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CORE_TABLES = (
    "users", "identities", "channels", "agents",
    "connections", "sessions", "interactions", "activities",
)

# 旧唯一约束 → 新的 (tenant_id 前导) 唯一约束
UNIQUE_CHANGES = (
    ("identities", "uq_identities_channel_external_id",
     "uq_identities_tenant_channel_external_id",
     ["tenant_id", "channel_id", "external_id"]),
    ("connections", "uq_connections_channel_agent_id",
     "uq_connections_tenant_channel_agent_id",
     ["tenant_id", "channel_id", "agent_id"]),
    ("interactions", "uq_interactions_channel_external_id",
     "uq_interactions_tenant_channel_external_id",
     ["tenant_id", "channel_id", "external_interaction_id"]),
)


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # 1. tenants root catalog
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # 2. 种子 bootstrap Tenant #1（既有 OSS 导入数据统一归到此租户）
    op.execute(
        "INSERT INTO tenants (id, name, status) VALUES (1, 'default', 'active')")

    # 2b. 推进 tenants_id_seq 越过 bootstrap Tenant #1：
    # PostgreSQL 对「显式 INSERT id」不会自动推进底层序列，否则首个 signup 的
    # provision() 取 nextval 仍得 1 → 与 tenants_pkey 冲突
    # （UniqueViolation，Cloud-0.1.2.1 Sign Up → Workspace 阻断）。
    # setval(seq, max(id)) 使下一次 nextval 返回 max(id)+1 = 2。
    op.execute(
        "SELECT setval(pg_get_serial_sequence('tenants', 'id'), "
        "(SELECT max(id) FROM tenants))")

    # 3. 8 张业务表加 tenant_id（NOT NULL DEFAULT 1 用于回填）+ FK
    for t in CORE_TABLES:
        op.add_column(t, sa.Column(
            "tenant_id", sa.Integer(), nullable=False, server_default="1"))
        op.create_foreign_key(f"fk_{t}_tenant_id", t, "tenants", ["tenant_id"], ["id"])

    # 4. 唯一约束演进：drop 旧 → create 新（tenant_id 前导）
    for table, old_name, new_name, cols in UNIQUE_CHANGES:
        op.drop_constraint(old_name, table, type_="unique")
        op.create_unique_constraint(new_name, table, cols)

    # sessions：把应用层语义键提升为租户前导的 DB 唯一约束（语义键不变）
    op.create_unique_constraint(
        "uq_sessions_tenant_identity_conn_conv", "sessions",
        ["tenant_id", "identity_id", "connection_id", "external_conversation_id"])

    # 5. 索引：RLS policy 扫描 + 租户作用域查询必需
    for t in CORE_TABLES:
        op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"], unique=False)

    # 6. 第二层防线：PostgreSQL RLS（仅 PG）
    if _is_postgresql():
        for t in CORE_TABLES:
            op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY tenant_isolation ON {t} "
                "USING (tenant_id = current_setting('app.tenant_id')::integer) "
                "WITH CHECK (tenant_id = current_setting('app.tenant_id')::integer)")
        # tenants root：不带 tenant_id——bootstrap（未设上下文）可读全部以解析租户，
        # 已设上下文仅可读自身。
        op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE tenants FORCE ROW LEVEL SECURITY")
        # current_setting(name, missing_ok boolean)：missing_ok 用 true（unset → NULL）。
        # 修复：初版误写 current_setting('app.tenant_id', '')，PG 报
        # invalid input syntax for type boolean: ""（Closure Evidence Run 捕获）。
        op.execute(
            "CREATE POLICY tenants_bootstrap ON tenants USING ("
            "COALESCE(current_setting('app.tenant_id', true), '') = '' "
            "OR id::text = current_setting('app.tenant_id', true))")
        # 说明：**保留** `tenant_id` 的 `server_default='1'`（不回退 DROP DEFAULT）——
        # 使 ORM metadata 与 PG schema 一致（无 drift），并保留 OSS/legacy 单租户
        # 兼容（实现边界 §2.1）。Cloud 安全不依赖此默认值：Repository 显式 stamp +
        # RLS 强制（未 SET LOCAL → fail-closed）。


def downgrade() -> None:
    if _is_postgresql():
        op.execute("DROP POLICY IF EXISTS tenants_bootstrap ON tenants")
        op.execute("ALTER TABLE tenants DISABLE ROW LEVEL SECURITY")
        for t in CORE_TABLES:
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
            op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")

    for t in CORE_TABLES:
        op.drop_index(f"ix_{t}_tenant_id", table_name=t)

    op.drop_constraint("uq_sessions_tenant_identity_conn_conv", "sessions", type_="unique")
    for table, old_name, new_name, cols in UNIQUE_CHANGES:
        op.drop_constraint(new_name, table, type_="unique")
        op.create_unique_constraint(old_name, table, cols[:-1] if table != "identities"
                                    else ["channel_id", "external_id"])

    for t in CORE_TABLES:
        op.drop_constraint(f"fk_{t}_tenant_id", t, type_="foreignkey")
        op.drop_column(t, "tenant_id")

    op.drop_table("tenants")
