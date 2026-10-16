"""browser auth — user_credentials + browser_sessions (Cloud-0.1.1-C)

在 0002（tenant isolation）之上，为 Browser Authentication 追加两张加性表：

1. `user_credentials`：login（全局唯一）+ verifier（pbkdf2 验证器）+ user_id
   （唯一）+ tenant_id。凭据**不进** `users` 表 / User 领域实体。
2. `browser_sessions`：session_id（不透明随机令牌，唯一）+ user_id + tenant_id。

刻意**不含** RLS：这两张表在 bootstrap（登录 / 会话解析，早于 tenant 解析）时被
访问，per-tenant RLS 会挡住登录。第二层隔离的真正对象仍是 8 张 Core Table
（0002 已建立，本迁移**零改动**）。

本迁移面向 **Cloud PostgreSQL**（与 0001/0002 同定位）：本地 OSS SQLite 走
`create_all`（init_database），不经过 Alembic。

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. user_credentials（Browser Auth 凭据；login 全局唯一）
    op.create_table(
        "user_credentials",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("login", sa.String(length=255), nullable=False),
        sa.Column("verifier", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name="fk_user_credentials_user_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_user_credentials_tenant_id"),
    )
    op.create_index("ix_user_credentials_user_id", "user_credentials",
                    ["user_id"], unique=True)
    op.create_index("ix_user_credentials_login", "user_credentials",
                    ["login"], unique=True)
    op.create_index("ix_user_credentials_tenant_id", "user_credentials",
                    ["tenant_id"], unique=False)

    # 2. browser_sessions（opaque 浏览器会话）
    op.create_table(
        "browser_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name="fk_browser_sessions_user_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_browser_sessions_tenant_id"),
    )
    op.create_index("ix_browser_sessions_session_id", "browser_sessions",
                    ["session_id"], unique=True)
    op.create_index("ix_browser_sessions_user_id", "browser_sessions",
                    ["user_id"], unique=False)
    op.create_index("ix_browser_sessions_tenant_id", "browser_sessions",
                    ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_browser_sessions_tenant_id", table_name="browser_sessions")
    op.drop_index("ix_browser_sessions_user_id", table_name="browser_sessions")
    op.drop_index("ix_browser_sessions_session_id", table_name="browser_sessions")
    op.drop_table("browser_sessions")

    op.drop_index("ix_user_credentials_tenant_id", table_name="user_credentials")
    op.drop_index("ix_user_credentials_login", table_name="user_credentials")
    op.drop_index("ix_user_credentials_user_id", table_name="user_credentials")
    op.drop_table("user_credentials")
