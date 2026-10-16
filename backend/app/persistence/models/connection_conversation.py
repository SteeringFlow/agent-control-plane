"""connection_conversations 表 ORM（Implementation Package 2 / Step 1）。

这是 V0 **唯一**的授权状态存储（Package 2-C §1.5 窄范围例外）：
「这条 Conversation 被允许使用这条 Connection」。

字段语义：
- `channel_id` + `external_conversation_id` = ADR-3 的授权作用域键
  （会话键只在 Channel Scope 内解释，因此两列共同构成主语）；
- `connection_id` = 授权目标（ADR-2 Access Path / ADR-8 eligible 集合的值域）；
- `tenant_id` = **直接**租户归属（自有列，与 8 张业务表同纪律；RLS 谓词列，
  **不**经 Connection 推导）。

约束设计（两条关键）：

1. **四元组 UNIQUE**：授权幂等（同一 conversation 对同一 connection 只有一行）。
   它的**前导列** `(tenant_id, channel_id, external_conversation_id)` 同时服务
   授权判定的主查询 —— 因此**不额外建**该组合索引（与既有的
   「复合唯一索引以前导列服务 channel 作用域访问」纪律一致）。
2. **复合 FK**（复合到 connections 的三列）：

       (tenant_id, channel_id, connection_id)
           → connections(tenant_id, channel_id, id)

   一个约束同时消灭两种错配：
   - 跨租户（grant.tenant_id 与 connection.tenant_id 不一致）；
   - **同租户跨 channel**（grant.channel_id 与 connection.channel_id 不一致）——
     后者 RLS 盖不住（同租户），单列 `connection_id` FK 也盖不住，只有复合 FK 能盖。

   代价：`connections` 需要一个 **supporting** `UNIQUE(tenant_id, channel_id, id)`
   （纯结构性，不改变 Connection 的业务唯一性 / 生命周期 / Router 语义 / API）。

Connection 删除语义：**阻断式**（无 `ondelete` 子句 = NO ACTION）——
「授权在 Connection 消失后保留」靠**阻断删除**实现，不靠隐式解绑
（与 Package 1 的 `identities.user_id` 同一裁决）。**没有 cascade。**

明确不存在的列（ADR-9 / ADR-1 / ADR-3 红线）：

    ❌ status / effect / priority / expires_at / conditions（纯 allow；deny = 缺行）
    ❌ identity_id / user_id（作用域不得下沉到 Identity / User）
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class ConnectionConversationModel(Base):
    __tablename__ = "connection_conversations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "channel_id", "external_conversation_id", "connection_id",
            name="uq_connection_conversations_tenant_channel_conv_conn",
        ),
        # 结构性一致性：一个约束同时锁住 tenant 与 channel 两处错配。
        ForeignKeyConstraint(
            ["tenant_id", "channel_id", "connection_id"],
            ["connections.tenant_id", "connections.channel_id", "connections.id"],
            name="fk_connection_conversations_tenant_channel_connection",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 列顺序对齐 Package 2-C §7.2 冻结的 DDL：授权作用域 → 目标 → 时间戳。
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True,
        default=1, server_default="1",
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id"), nullable=False
    )
    external_conversation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # 刻意**不**加单列 FK：connection_id 由上面的复合 FK 覆盖（再加会重复校验）。
    connection_id: Mapped[int] = mapped_column(nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )
