"""channels bootstrap RLS policy (Cloud-0.1.3.1 blocker fix)

PostgreSQL `FORCE ROW LEVEL SECURITY` 作用于 8 张核心表（含 `channels`，由 0002
建立 `tenant_isolation` + FORCE RLS）。WeCom Webhook 入站走
`bootstrap_uow_factory(tenant_id=None)`，**不设置** `app.tenant_id`，导致对
`channels` 的 PK 定位读（`_load_channel` 的 `get_by_id` / `_tenant_scope` 的
`find_tenant_id_by_id`）被 RLS 过滤为 0 行 → `InboundChannelConfigError`，
Cloud PostgreSQL 下 WeCom 入站不可用。

修复原则（保持既有 multi-tenant isolation 设计，不关闭 RLS，不放开普通 Repo）：

为 `channels` 增加 `channels_bootstrap` 策略，与 0002 既有 `tenants_bootstrap`
**同一边界**——仅当 `app.tenant_id` 未设置（bootstrap 上下文）时允许 SELECT。
这是**条件**策略（非 `USING (true)`），且入站 bootstrap 读恒为按主键定位
（`get_by_id` / `find_tenant_id_by_id`），不会因此读出「任意」渠道行；普通
tenant-scoped 读仍受 `tenant_isolation` 约束，多租户隔离不变。

策略显式 `FOR SELECT`：只服务 bootstrap 定位读，绝不经由 bootstrap 放开任何写入。
`tenants` 表的 `tenants_bootstrap` 已建立相同先例（bootstrap 未设上下文可读以解析租户）。

仅 PostgreSQL 生效；SQLite 无 RLS 概念，跳过（本地开发行为不变）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgresql():
        return
    # 与 tenants_bootstrap 同一边界：仅 bootstrap 上下文（app.tenant_id 未设）可读。
    # 入站 bootstrap 读恒为按主键定位，不会读出任意渠道行。FOR SELECT 确保该策略
    # 只服务定位读，绝不经由 bootstrap 放开写入。
    op.execute(
        "CREATE POLICY channels_bootstrap ON channels FOR SELECT "
        "USING (COALESCE(current_setting('app.tenant_id', true), '') = '')"
    )


def downgrade() -> None:
    if not _is_postgresql():
        return
    op.execute("DROP POLICY IF EXISTS channels_bootstrap ON channels")
