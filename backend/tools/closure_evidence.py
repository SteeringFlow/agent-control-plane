"""Cloud-0.1.1-B —— Closure Evidence Run（只在 Linux + PostgreSQL 上运行）。

补齐三项 PENDING EVIDENCE（不改任何生产代码）：

A. Migration 0001 → **head**：tenants + 8×tenant_id + constraints/indexes + RLS
B. SET LOCAL：Transaction A(tenant=A) COMMIT 后，Transaction B 不继承 A 的上下文
C. RLS：Tenant A 直连 PG 看不到 Tenant B（及反向）；unset 上下文 → 访问受限

**关键前提**：PostgreSQL **superuser 绕过 RLS**。因此 RLS 证据必须用**非 superuser**
应用角色执行（本脚本自动创建 `acp_app` NOSUPERUSER 并授权）。这本身是一条证据：
「RLS 只在非 superuser 角色下生效；生产必须用非 superuser 应用角色」。

**Implementation Package 2 / Step 5 —— stale assumption 修正**（只对齐已经发生的
schema 演进，不新增检查口径）：

| 原假设 | 实际（head 0007） |
|---|---|
| `alembic head == "0002"`（硬编码） | 改为**从迁移目录动态取 head**——硬编码的 evidence 会长期静默过期（同一修法见 `tests/test_cloud_0_1_1_a_postgres_alembic_foundation.py`） |
| 表集合 = 8 业务表 + tenants | + `user_credentials` / `browser_sessions`（0003 Browser Auth）/ `connection_conversations`（0007 授权状态存储） |
| 「8 业务表 + tenants 均有 FORCE RLS」 | RLS 集合 = 8 业务表 + tenants + `connection_conversations`；**Browser Auth 两表刻意无 RLS**（auth 边界表，非租户业务数据） |

用法（容器内，WORKDIR=/app/backend）：

    CLOUD_TEST_DATABASE_URL=postgresql+psycopg://acp:acp@postgres:5432/acp \
        python tools/closure_evidence.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

APP_ROLE = "acp_app"
APP_PW = "acp_app"

CORE_TABLES = ("users", "identities", "channels", "agents",
               "connections", "sessions", "interactions", "activities")

# 0003 Browser Auth（非租户业务数据 → 刻意无 RLS）
BROWSER_AUTH_TABLES = ("user_credentials", "browser_sessions")

# 0007 授权状态存储（Package 2 / Step 1；有 RLS）
AUTHZ_TABLES = ("connection_conversations",)

# M6.6-C3 / ADR-12 / 0008：Bootstrap 窗口存储（**不是**授权状态；有 RLS）
BOOTSTRAP_TABLES = ("bootstrap_arms",)

# 应当 ENABLE + FORCE RLS 的表集合（Browser Auth 两表**不在其中**）
RLS_TABLES = CORE_TABLES + ("tenants",) + AUTHZ_TABLES + BOOTSTRAP_TABLES


def _migration_head() -> str:
    """当前迁移链 head——**动态取**，避免同类漂移复发。

    历史教训：本脚本曾硬编码 `"0002"`，而链 head 早已推进到 0007——硬编码的
    evidence 会长期静默过期，看起来"有证据"实则什么都没证明。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _pg_dsn(url: str) -> str:
    return (url.replace("postgresql+psycopg://", "postgresql://")
               .replace("postgresql+psycopg2://", "postgresql://"))


def _dsn_for_role(base_dsn: str, role: str, pw: str) -> str:
    # 在 base_dsn 里替换 user:pass@
    head, sep, tail = base_dsn.partition("://")
    creds, at, host = tail.partition("@")
    return f"{head}://{role}:{pw}{at}{host}"


def _ok(label: str, cond: bool, extra: str = "") -> bool:
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' — ' + extra) if extra else ''}")
    return cond


# ================================================================ A. Migration


def evidence_a(admin: psycopg.Connection) -> bool:
    print("\n=== Evidence A: Migration 0001 → head (tenants + tenant_id + constraints/indexes/RLS) ===")
    ok = True
    with admin.cursor() as cur:
        cur.execute("select version_num from alembic_version")
        rev = cur.fetchone()[0]
        head = _migration_head()
        ok &= _ok(f"alembic head = {head}（动态取自迁移目录）", rev == head,
                  f"head={rev}")

        cur.execute("select table_name from information_schema.tables "
                    "where table_schema='public'")
        tables = {r[0] for r in cur.fetchall()}
        expected = (set(CORE_TABLES) | {"tenants", "alembic_version"}
                    | set(BROWSER_AUTH_TABLES) | set(AUTHZ_TABLES)
                    | set(BOOTSTRAP_TABLES))
        ok &= _ok("表集合 = 8 业务表 + tenants + alembic_version "
                  "+ browser_auth(2) + connection_conversations + bootstrap_arms",
                  tables == expected, f"diff={tables ^ expected}")

        # tenant_id 列 + FK
        for t in CORE_TABLES:
            cur.execute(
                "select count(*) from information_schema.columns "
                "where table_name=%s and column_name='tenant_id'", (t,))
            has_col = cur.fetchone()[0] == 1
            cur.execute(
                "select count(*) from information_schema.table_constraints tc "
                "join information_schema.constraint_column_usage ccu "
                "  on tc.constraint_name=ccu.constraint_name "
                "where tc.table_name=%s and tc.constraint_type='FOREIGN KEY' "
                "  and ccu.table_name='tenants'", (t,))
            has_fk = cur.fetchone()[0] >= 1
            ok &= _ok(f"{t}.tenant_id 列+FK", has_col and has_fk,
                      f"col={has_col} fk={has_fk}")

        # indexes
        for t in CORE_TABLES:
            cur.execute("select count(*) from pg_indexes where tablename=%s and indexname=%s",
                        (t, f"ix_{t}_tenant_id"))
            ok &= _ok(f"index ix_{t}_tenant_id", cur.fetchone()[0] == 1)

        # unique constraints (tenant_id 前导)
        for t, cols in (
            ("identities", {"tenant_id", "channel_id", "external_id"}),
            ("connections", {"tenant_id", "channel_id", "agent_id"}),
            ("interactions", {"tenant_id", "channel_id", "external_interaction_id"}),
            ("sessions", {"tenant_id", "identity_id", "connection_id",
                          "external_conversation_id"}),
        ):
            cur.execute(
                "select c.conname, array_agg(a.attname) "
                "from pg_constraint c "
                "join pg_class t2 on t2.oid=c.conrelid "
                "join unnest(c.conkey) as k(attnum) on true "
                "join pg_attribute a on a.attrelid=c.conrelid and a.attnum=k.attnum "
                "where t2.relname=%s and c.contype='u' "
                "group by c.conname", (t,))
            found = any(set(c) == cols for _, c in cur.fetchall())
            ok &= _ok(f"{t} UNIQUE{tuple(sorted(cols))}", found)

        # RLS enabled + forced
        cur.execute(
            "select relname, relrowsecurity, relforcerowsecurity from pg_class "
            "where relname = any(%s)", (list(RLS_TABLES),))
        rls = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        ok &= _ok("8 业务表 + tenants + connection_conversations 均 ENABLE+FORCE RLS",
                  all(rls.get(t) == (True, True) for t in RLS_TABLES),
                  str(rls))
        # Browser Auth 两表**刻意无 RLS**：反向确认为设计而非遗漏
        cur.execute(
            "select relrowsecurity, relforcerowsecurity from pg_class "
            "where relname = any(%s)", (list(BROWSER_AUTH_TABLES),))
        browser = cur.fetchall()
        ok &= _ok("Browser Auth 两表刻意无 RLS（auth 边界表，非租户业务数据）",
                  len(browser) == 2 and all(r == (False, False) for r in browser),
                  str(browser))
        # 每张 RLS 表至少 1 条策略；+1 = channels 的 bootstrap 策略
        cur.execute("select count(*) from pg_policies where schemaname='public'")
        n_pol = cur.fetchone()[0]
        expected_pol = len(RLS_TABLES) + 1
        ok &= _ok(f"RLS policies 已建 = {expected_pol}（RLS 表各 1 + channels_bootstrap）",
                  n_pol == expected_pol, f"policies={n_pol}")
    return ok


# ================================================================ B. SET LOCAL


def evidence_b(base_dsn: str) -> bool:
    print("\n=== Evidence B: SET LOCAL 事务级清理（Transaction B 不继承 A）===")
    ok = True
    with psycopg.connect(base_dsn) as conn:
        # Transaction A: tenant = 1
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.tenant_id', '1', true)")
                cur.execute("SELECT current_setting('app.tenant_id', true)")
                in_a = cur.fetchone()[0]
        # Transaction B: 同一连接、同一 session
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT current_setting('app.tenant_id', true)")
                in_b = cur.fetchone()[0]
        ok &= _ok("Txn A 内上下文 = '1'", in_a == "1", f"got={in_a!r}")
        ok &= _ok("Txn B 不继承 A 的 tenant 上下文（已清理）",
                  in_b in (None, ""), f"got={in_b!r}")
    return ok


# ================================================================ C. RLS


def _setup_app_role(admin: psycopg.Connection) -> None:
    with admin.cursor() as cur:
        cur.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='%s') "
            "THEN CREATE ROLE %s LOGIN PASSWORD '%s' NOSUPERUSER; END IF; END $$;"
            % (APP_ROLE, APP_ROLE, APP_PW))
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
        cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
        cur.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")


def _tenant_names(cur, tenant_id: int) -> set[str]:
    """在指定租户上下文下**看得见**的 users.name 集合。

    用「看得见哪些行」而不是「看得见几行」：库中可能还有其它合法数据
    （历史 evidence 运行、真实用户），绝对行数是对**环境**的假设，
    不是对 RLS 的断言。
    """
    with cur.connection.transaction():
        cur.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        cur.execute("SELECT name FROM users")
        return {r[0] for r in cur.fetchall()}


def _tenant_insert_user(cur, tenant_id: int, name: str) -> None:
    with cur.connection.transaction():
        cur.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        cur.execute("INSERT INTO users (name, status, tenant_id) VALUES (%s, 'active', %s)",
                    (name, tenant_id))


def evidence_c(admin: psycopg.Connection, base_dsn: str) -> bool:
    print("\n=== Evidence C: RLS 跨租户隔离（非 superuser 应用角色）===")
    ok = True
    _setup_app_role(admin)
    # 需要存在 Tenant 1 与 Tenant 2（users.tenant_id 的 FK 目标）
    with admin.cursor() as cur:
        cur.execute("INSERT INTO tenants (id, name, status) VALUES (1,'default','active') "
                    "ON CONFLICT (id) DO NOTHING")
        cur.execute("INSERT INTO tenants (id, name, status) VALUES (2,'tenant-b','active') "
                    "ON CONFLICT (id) DO NOTHING")

    app_dsn = _dsn_for_role(base_dsn, APP_ROLE, APP_PW)
    with psycopg.connect(app_dsn) as conn:
        # 清理（用 tenant=1 上下文删除可能的历史行）
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.tenant_id', '1', true)")
                cur.execute("DELETE FROM users WHERE name LIKE 'ev_%'")
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.tenant_id', '2', true)")
                cur.execute("DELETE FROM users WHERE name LIKE 'ev_%'")

        with conn.cursor() as cur:
            _tenant_insert_user(cur, 1, "ev_tenantA")
            _tenant_insert_user(cur, 2, "ev_tenantB")

        with conn.cursor() as cur:
            names_a = _tenant_names(cur, 1)
            names_b = _tenant_names(cur, 2)
        ok &= _ok("Tenant A 直连 PG 看不到 Tenant B 的行",
                  "ev_tenantB" not in names_a and "ev_tenantA" in names_a,
                  f"A sees {sorted(names_a)}")
        ok &= _ok("Tenant B 直连 PG 看不到 Tenant A 的行（反向）",
                  "ev_tenantA" not in names_b and "ev_tenantB" in names_b,
                  f"B sees {sorted(names_b)}")

        # unset 上下文 → 访问受限（fail-closed）。
        # **Step 5 stale 修正**：0005 起 RLS 表达式为
        #   NULLIF(current_setting('app.tenant_id', true), '')::integer
        # 未设时得到 `NULLIF('', '')` = NULL → 谓词为 NULL → **0 行**；
        # 而 0002 形态的 `''::integer` 会抛 InvalidTextRepresentation(DataError)。
        # 两种形态都是 fail-closed，但**可观测不同**：旧断言要求"抛错"，
        # 属 0005 之前的假设，会在 head 上必然失败。
        with conn.cursor() as cur:
            with conn.transaction():
                cur.execute("SELECT count(*) FROM users")
                visible_wo_ctx = cur.fetchone()[0]
        ok &= _ok("unset tenant 上下文 → 读不到任何行（fail-closed：0 行）",
                  visible_wo_ctx == 0, f"rows={visible_wo_ctx}")

    # tenants bootstrap 策略：unset 可读 tenants（用于解析租户）
    with psycopg.connect(app_dsn) as conn2:
        with conn2.transaction():
            with conn2.cursor() as cur:
                cur.execute("SELECT count(*) FROM tenants")   # bootstrap 分支命中
                n_tenants = cur.fetchone()[0]
    ok &= _ok("unset 上下文可读 tenants（bootstrap 策略）", n_tenants >= 1,
              f"tenants visible={n_tenants}")
    return ok


def main() -> int:
    url = os.environ.get("CLOUD_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        print("缺少 CLOUD_TEST_DATABASE_URL / DATABASE_URL")
        return 2
    dsn = _pg_dsn(url)

    results = []
    with psycopg.connect(dsn, autocommit=True) as admin:
        results.append(("A. Migration", evidence_a(admin)))
    results.append(("B. SET LOCAL", evidence_b(dsn)))
    with psycopg.connect(dsn, autocommit=True) as admin:
        results.append(("C. RLS", evidence_c(admin, dsn)))

    print("\n==================== CLOSURE EVIDENCE SUMMARY ====================")
    for name, ok in results:
        print(f"  {name:16s}: {'PASS' if ok else 'FAIL'}")
    all_ok = all(ok for _, ok in results)
    print(f"  OVERALL         : {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
