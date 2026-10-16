"""identities.user_id: NOT NULL → NULLABLE（Implementation Package 1 / Gate 3 A-1）

外部身份与 ACP User 的管理绑定解耦（ADR-1 / Gate 2.6 D1/D2）：

    Identity                = Channel-scoped External Identity
    ACP User association    = OPTIONAL

本迁移**只放宽约束**（纯加性、向后兼容）：现有写入全部提供 user_id，
不会让任何既有行为失败。它提供的是**结构容量**，不是**可达性**——
V0 不存在「创建未绑定 Identity」的写入路径（Gate 2.6 D4 未裁决：
谁有权创建未绑定 Identity / 是否需要 Discovery 触发点，均仍开放）。

FK 语义（Gate 3 A-2 冻结，本迁移**刻意不动**）：
    user_id → users.id，**无 ondelete 子句** = NO ACTION（阻断式）
    - 删除被引用的 User **会失败**（IntegrityError），不是 SET NULL、不是 CASCADE；
    - 「Identity 在 User 消失后保留」靠**阻断删除**实现，不靠隐式解绑
      （隐式解绑会绕过「改绑定 = 显式动作」的既有冻结纪律）；
    - 显式化 `ondelete="RESTRICT"` 被拒绝：与 NO ACTION 行为等同，却会触发
      约束重建与 autogenerate 差异 → 该语义改由测试守卫锁死。

不变项：
    - 索引 ix_identities_user_id 保留（PG 对 NULL 值同样建索引）；
    - 唯一约束 uq_identities_tenant_channel_external_id 不变（不含 user_id）；
    - identities 的 channel_id / external_id / tenant_id 全部不动；
    - 0001–0005 一律不回改（新变更追加为 0006）。

已知限制（诚实记录，不得当作缺陷掩盖）：
    - **仅 PostgreSQL**：SQLite 不支持 ALTER COLUMN。OSS 本地库 schema 由
      `Base.metadata.create_all` 承担（Cloud-0.1.1-A 已冻结该分工），
      因此本变更必须**同时**改 ORM（app/persistence/models/identity.py），
      否则 Cloud(PG) 与 OSS(SQLite) 两条建库路径会分叉。
    - **downgrade 有损不可逆**：若库中已存在 `user_id IS NULL` 的行，
      `nullable=False` 会直接失败。这不是缺陷，而是「结构容量一旦被使用
      就不可自动回退」的诚实表达。**禁止把 downgrade 宣传为安全回滚。**

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-18
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """identities.user_id: NOT NULL → NULLABLE（不改 FK / 索引 / 唯一键）。"""
    op.alter_column(
        "identities",
        "user_id",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    """NULLABLE → NOT NULL（**有损**：存在 user_id IS NULL 的行时本步会失败）。"""
    op.alter_column(
        "identities",
        "user_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
