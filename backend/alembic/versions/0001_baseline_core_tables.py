"""baseline core tables (OSS V0.x RC, HEAD e6ac4e9)

Cloud-0.1.1-A：8 张核心表的 Alembic **baseline**，由 autogenerate 从
`app.persistence.models`（8 张 ORM，与运行时同一条注册路径）对**空 PostgreSQL**
生成，再逐字段核对为模型的精确镜像——只含 4.2 冻结的 8 张业务表，
**不引入**任何额外业务表 / 列 / 概念（无 tenant / RLS / 新字段）。

8 张表：users / identities / channels / agents / connections / sessions /
interactions / activities（顺序由 FK 依赖决定）。

Revision ID: 0001
Revises: (none)
Create Date: 2026-09-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("endpoint", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("credential_ref", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "channels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "connections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("credential_ref", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id", "agent_id", name="uq_connections_channel_agent_id"),
    )
    op.create_index(op.f("ix_connections_agent_id"), "connections", ["agent_id"], unique=False)
    op.create_index(op.f("ix_connections_channel_id"), "connections", ["channel_id"], unique=False)
    op.create_table(
        "identities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id", "external_id", name="uq_identities_channel_external_id"),
    )
    op.create_index(op.f("ix_identities_channel_id"), "identities", ["channel_id"], unique=False)
    op.create_index(op.f("ix_identities_user_id"), "identities", ["user_id"], unique=False)
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("identity_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=True),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("external_session_id", sa.String(length=255), nullable=True),
        sa.Column("external_conversation_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("metadata", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"]),
        sa.ForeignKeyConstraint(["identity_id"], ["identities.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sessions_agent_id"), "sessions", ["agent_id"], unique=False)
    op.create_index(op.f("ix_sessions_channel_id"), "sessions", ["channel_id"], unique=False)
    op.create_index(op.f("ix_sessions_connection_id"), "sessions", ["connection_id"], unique=False)
    op.create_index(op.f("ix_sessions_external_conversation_id"), "sessions", ["external_conversation_id"], unique=False)
    op.create_index(op.f("ix_sessions_identity_id"), "sessions", ["identity_id"], unique=False)
    op.create_table(
        "interactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("external_interaction_id", sa.String(length=255), nullable=True),
        sa.Column("request_payload", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("response_payload", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("processing_status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("delivery_status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("failure_stage", sa.String(length=32), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        # 与模型逐字段一致：sqlite 变体仅影响 SQLite 渲染；PostgreSQL 落
        # TIMESTAMP WITH TIME ZONE（既有 Attempt Fence 语义不变）。
        sa.Column(
            "processing_started_at",
            sa.DateTime(timezone=True).with_variant(
                sqlite.DATETIME(timezone=True, truncate_microseconds=True), "sqlite"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id", "external_interaction_id", name="uq_interactions_channel_external_id"),
    )
    op.create_index(op.f("ix_interactions_session_id"), "interactions", ["session_id"], unique=False)
    op.create_table(
        "activities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("interaction_id", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["interaction_id"], ["interactions.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_activities_interaction_id"), "activities", ["interaction_id"], unique=False)
    op.create_index(op.f("ix_activities_session_id"), "activities", ["session_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_activities_session_id"), table_name="activities")
    op.drop_index(op.f("ix_activities_interaction_id"), table_name="activities")
    op.drop_table("activities")
    op.drop_index(op.f("ix_interactions_session_id"), table_name="interactions")
    op.drop_table("interactions")
    op.drop_index(op.f("ix_sessions_identity_id"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_external_conversation_id"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_connection_id"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_channel_id"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_agent_id"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_index(op.f("ix_identities_user_id"), table_name="identities")
    op.drop_index(op.f("ix_identities_channel_id"), table_name="identities")
    op.drop_table("identities")
    op.drop_index(op.f("ix_connections_channel_id"), table_name="connections")
    op.drop_index(op.f("ix_connections_agent_id"), table_name="connections")
    op.drop_table("connections")
    op.drop_table("users")
    op.drop_table("channels")
    op.drop_table("agents")
