"""tenants 表 ORM（Cloud-0.1.1-B）——租户根目录（root catalog）。

`tenants` 是 root catalog：**自身不带 `tenant_id`**（它自己就是 tenant），因此
不套用「按 `tenant_id` 过滤」的普通 per-tenant RLS（见实现边界 §9 的特殊策略）。

Bootstrap 种子（实现边界 §2.1）：
- `create_all` 路径（本地 / 测试内存库）——本模块注册 `after_create` 监听，
  在 `tenants` 表创建时**自动插入 Tenant #1**，使 8 张表 `tenant_id` 的 FK
  目标恒存在（既有单租户测试零改动通过）。
- Cloud PostgreSQL——`op.create_table` 不触发 ORM 表事件，由 migration 0002
  显式 `INSERT ... (id=1)`。

本表只声明 Schema（PK/列/时间戳），不写 relationship()，与其余 8 表同纪律。
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, String, event, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class TenantModel(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


@event.listens_for(TenantModel.__table__, "after_create")
def _seed_bootstrap_tenant(target, connection, **_: object) -> None:
    """`tenants` 表创建时注入 bootstrap Tenant #1（幂等：仅在刚建表时触发）。

    只对 `create_all`（本地 SQLite / 测试内存库）生效；Cloud PG 的 schema 由
    Alembic 0002 建表并显式插入 Tenant #1（边界 §5 第 2 步）。
    """
    connection.execute(
        target.insert().values(id=1, name="default", status="active")
    )
