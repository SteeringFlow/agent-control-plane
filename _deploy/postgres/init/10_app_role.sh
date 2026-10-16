#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Cloud V0 —— PostgreSQL runtime 角色初始化（backend 非-superuser role）
#
# 触发：官方 postgres image 的 /docker-entrypoint-initdb.d/ 机制，
#       **仅在全新 PGDATA 首次初始化时执行一次**（restart / 既有 volume 不会重跑）。
#
# 目的：把「初始化/迁移角色」（POSTGRES_USER，image 默认 superuser）与
#       「backend runtime 角色」（POSTGRES_APP_USER，非-superuser）分离。
#       PostgreSQL RLS 只对非 superuser 生效；backend 运行时必须用后者，
#       否则 RLS 第二层防线被 superuser 绕过。
#
# 授权模型（最小）：
#   - runtime 角色：LOGIN、NOSUPERUSER、NOBYPASSRLS、NOCREATEDB、NOCREATEROLE
#   - 仅 DML（SELECT/INSERT/UPDATE/DELETE）+ 序列 USAGE/SELECT
#   - 迁移在 admin（POSTGRES_USER）下执行；ALTER DEFAULT PRIVILEGES 保证
#     admin 之后创建的表/序列自动对 runtime 角色授权
#
# 实现注意：psql 的变量插值（:"x" / :'x'）**只在 stdin/文件输入时发生**，
#           `-c` 传入的字符串不做插值——故所有含变量的语句均走 heredoc(stdin)。
#
# 幂等：可安全地对既有实例手动补跑（既有 volume 未触发 init 时的逃生口）：
#   docker compose -f docker-compose.cloud.yml -f docker-compose.yml \
#     -f docker-compose.production.yml exec postgres \
#     bash /docker-entrypoint-initdb.d/10_app_role.sh
# ---------------------------------------------------------------------
set -euo pipefail

: "${POSTGRES_APP_USER:?POSTGRES_APP_USER 未设置（backend runtime role）}"
: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD 未设置（backend runtime password）}"
ADMIN_USER="${POSTGRES_USER:-postgres}"
DB_NAME="${POSTGRES_DB:-acp}"

# init 期本地连接通常为 trust；显式提供 admin 口令以兼容非 trust 场景。
export PGPASSWORD="${POSTGRES_PASSWORD:-}"

psql_admin() {
  psql -v ON_ERROR_STOP=1 --no-password \
       --username "$ADMIN_USER" --dbname "$DB_NAME" "$@"
}

# 1. 创建 / 更新 runtime 角色（口令经 psql 变量转义，绝不落 SQL 文件 / 镜像层）
exists="$(psql_admin -tA --set=app_user="$POSTGRES_APP_USER" <<'EOSQL'
SELECT 1 FROM pg_roles WHERE rolname = :'app_user';
EOSQL
)"
if [ "$exists" = "1" ]; then
  psql_admin --set=app_user="$POSTGRES_APP_USER" --set=app_password="$POSTGRES_APP_PASSWORD" <<'EOSQL'
ALTER ROLE :"app_user" WITH LOGIN PASSWORD :'app_password';
EOSQL
else
  psql_admin --set=app_user="$POSTGRES_APP_USER" --set=app_password="$POSTGRES_APP_PASSWORD" <<'EOSQL'
CREATE ROLE :"app_user" WITH LOGIN PASSWORD :'app_password';
EOSQL
fi

# 2. 钉死最小权限属性：非 superuser / 非 bypassrls / 无建库建角色权
psql_admin --set=app_user="$POSTGRES_APP_USER" <<'EOSQL'
ALTER ROLE :"app_user" NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
EOSQL

# 3. 最小运行授权：现有对象 + 未来由 admin 创建的对象（迁移在 admin 下执行）
psql_admin --set=app_user="$POSTGRES_APP_USER" --set=admin_user="$ADMIN_USER" --set=db_name="$DB_NAME" <<'EOSQL'
GRANT CONNECT ON DATABASE :"db_name" TO :"app_user";
GRANT USAGE ON SCHEMA public TO :"app_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app_user";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_user" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_user" IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO :"app_user";
EOSQL
