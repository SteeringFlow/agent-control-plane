"""tenant_isolation RLS robustness — NULLIF(current_setting(..., true), '') (Cloud V0 rehearsal fix)

0002 的 `tenant_isolation` 策略表达式为：

    tenant_id = current_setting('app.tenant_id')::integer

它在 **bootstrap 定位读**（该连接未设置 `app.tenant_id`）下会**报错**而非返回 0 行：

  1. 全新连接（GUC 未定义）→ `current_setting('app.tenant_id')` 抛
     `unrecognized configuration parameter "app.tenant_id"`；
  2. 复用连接（此前事务级 set_config 过）→ 事务结束后 GUC 回落为空串 '' →
     `''::integer` 抛 `invalid input syntax for type integer: ""`。

RLS 会对同一命令的所有 permissive 策略求值（OR，且不保证短路），因此即便 0004
新增了 `channels_bootstrap`，`tenant_isolation` 仍会在 bootstrap 读上抛错——
Webhook 入站（`_load_channel` / `find_tenant_id_by_id`）在 Cloud PostgreSQL 下
依旧不可用（本地 fresh PostgreSQL rehearsal 实证）。

修复（**只改写策略表达式，隔离语义逐字节不变**）：

    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::integer

  - 未设置 / 空串 → NULLIF → NULL → 谓词为 NULL（不匹配、且**不报错**）
    → 仅 `channels_bootstrap` / `tenants_bootstrap` 在 bootstrap 上下文放行；
  - 已设置有效值 → 与原表达式完全一致（仍严格按租户过滤）。

不关闭 RLS、不改任何业务列 / 既有策略名、不放开写入、不改冻结产品边界。

仅 PostgreSQL 生效（SQLite 无 RLS）。

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CORE_TABLES = (
    "users", "identities", "channels", "agents",
    "connections", "sessions", "interactions", "activities",
)

# 健壮表达式：unset / '' 均 → NULL（不匹配且不报错）；有效值 → 原语义。
_ROBUST = "NULLIF(current_setting('app.tenant_id', true), '')::integer"
# 原（0002）表达式，仅用于 downgrade 精确回退。
_LEGACY = "current_setting('app.tenant_id')::integer"


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _alter_tenant_isolation(expr: str) -> None:
    for t in CORE_TABLES:
        op.execute(
            f"ALTER POLICY tenant_isolation ON {t} "
            f"USING (tenant_id = {expr}) "
            f"WITH CHECK (tenant_id = {expr})"
        )


def upgrade() -> None:
    if not _is_postgresql():
        return
    _alter_tenant_isolation(_ROBUST)


def downgrade() -> None:
    if not _is_postgresql():
        return
    _alter_tenant_isolation(_LEGACY)
