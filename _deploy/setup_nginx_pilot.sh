#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Step 16.1.3.3 —— ACP Pilot nginx 渲染与部署纪律脚本
#
# 职责（冻结，不扩大）：
#   1. fail-closed：任何必需参数/依赖缺失立即退出非零；
#   2. 渲染 vhost 模板占位符 → runtime/rendered/（不入 git）；
#   3. runtime 生成 htpasswd → runtime/auth/（明文密码绝不写入任何 git tracked 产物）；
#   4. 占位符残留自检：渲染结果出现 __ACP_* 即失败；
#   5. nginx -t 纪律：渲染结果必须能通过 nginx 配置测试，否则失败；
#   6. 历史脚本（setup_nginx.sh / setup_nginx_stage3.sh / deploy_acp.sh）零改动。
#
# 不做：backend 修改 / DB schema / ACP domain entities / JWT / RBAC /
#       新 deployment framework / 证书自动续期改造（沿用 certbot HTTP-01）。
# ---------------------------------------------------------------------
set -euo pipefail

fail() { echo "[setup_nginx_pilot] FAIL: $*" >&2; exit 1; }
info() { echo "[setup_nginx_pilot] $*"; }

# ---------------------------------------------------------------- 1. 依赖 fail-closed
for cmd in sed grep openssl; do
  command -v "$cmd" >/dev/null 2>&1 || fail "缺少依赖命令: $cmd"
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NGINX_DIR="$REPO_DIR/_deploy/nginx"
TEMPLATE="$NGINX_DIR/acp-pilot-vhost.conf.template"
RUNTIME_DIR="$NGINX_DIR/runtime"
RENDERED_DIR="$RUNTIME_DIR/rendered"
TLS_DIR="$RUNTIME_DIR/tls"
AUTH_DIR="$RUNTIME_DIR/auth"

[ -f "$TEMPLATE" ] || fail "vhost 模板不存在: $TEMPLATE"

# ---------------------------------------------------------------- 2. 必需环境变量（fail closed）
: "${ACP_DOMAIN:?FAIL: 缺少 ACP_DOMAIN}"
: "${ACP_TLS_CERT_PATH:?FAIL: 缺少 ACP_TLS_CERT_PATH}"
: "${ACP_TLS_KEY_PATH:?FAIL: 缺少 ACP_TLS_KEY_PATH}"
: "${ACP_BASIC_AUTH_USER:?FAIL: 缺少 ACP_BASIC_AUTH_USER}"
: "${ACP_BASIC_AUTH_PASS:?FAIL: 缺少 ACP_BASIC_AUTH_PASS}"

# 有默认值的运行时参数
ACP_UPSTREAM="${ACP_UPSTREAM:-http://backend:8000}"
ACP_ACME_WEBROOT="${ACP_ACME_WEBROOT:-/var/www/certbot}"
ACP_HTPASSWD_PATH="${ACP_HTPASSWD_PATH:-/etc/nginx/acp-auth/acp.htpasswd}"
NGINX_BIN="${NGINX_BIN:-nginx}"

# ---------------------------------------------------------------- 3. 渲染模板
mkdir -p "$RENDERED_DIR" "$AUTH_DIR"
RENDERED="$RENDERED_DIR/acp-pilot-vhost.conf"
RENDERED_TMP="$RENDERED_DIR/.acp-pilot-vhost.conf.tmp"

sed -e "s|__ACP_DOMAIN__|$ACP_DOMAIN|g" \
    -e "s|__ACP_UPSTREAM__|$ACP_UPSTREAM|g" \
    -e "s|__ACP_TLS_CERT_PATH__|$ACP_TLS_CERT_PATH|g" \
    -e "s|__ACP_TLS_KEY_PATH__|$ACP_TLS_KEY_PATH|g" \
    -e "s|__ACP_HTPASSWD_PATH__|$ACP_HTPASSWD_PATH|g" \
    -e "s|__ACP_ACME_WEBROOT__|$ACP_ACME_WEBROOT|g" \
    "$TEMPLATE" > "$RENDERED_TMP"

mv "$RENDERED_TMP" "$RENDERED"
chmod 644 "$RENDERED"
info "已渲染: $RENDERED"

# ---------------------------------------------------------------- 4. 占位符残留自检
# 只检查配置语义行（剥离注释）——注释中的占位符说明文字不算残留。
if grep -v '^[[:space:]]*#' "$RENDERED" | grep -q '__ACP_'; then
  grep -n '__ACP_' "$RENDERED" || true
  fail "渲染结果存在 __ACP_* 占位符残留（拒绝部署）"
fi
info "占位符残留自检: 通过"

# ---------------------------------------------------------------- 5. runtime 生成 htpasswd（明文密码不入 git）
HTPASSWD_OUT="$(dirname "$ACP_HTPASSWD_PATH")/$(basename "$ACP_HTPASSWD_PATH")"
mkdir -p "$(dirname "$HTPASSWD_OUT")"
if command -v htpasswd >/dev/null 2>&1; then
  htpasswd -bc "$HTPASSWD_OUT" "$ACP_BASIC_AUTH_USER" "$ACP_BASIC_AUTH_PASS"
else
  # 无 htpasswd 时用 openssl 生成 APR1（nginx 兼容）
  HASH="$(openssl passwd -apr1 "$ACP_BASIC_AUTH_PASS")" || fail "openssl passwd 生成失败"
  printf '%s:%s\n' "$ACP_BASIC_AUTH_USER" "$HASH" > "$HTPASSWD_OUT"
fi
chmod 644 "$HTPASSWD_OUT"
# 权限方案（16.1.3.6 F1 裁决）：644 而非 root:nginx+640 ——
#   * 644 与跨平台 bind mount 的 uid/gid 映射解耦，nginx:alpine 非 root
#     worker（uid 101）保证可读（640 曾导致凭据校验 500，16.1.3.5 F1）；
#   * 文件内容仅为 APR1/系统 hash，绝无明文口令；
#   * runtime/ 产物不入 git（.gitignore 守卫）；root provisioning 语义不变。
info "已生成 htpasswd: $HTPASSWD_OUT"

# ---------------------------------------------------------------- 6. nginx -t 纪律（渲染结果必须可加载）
CONF_DIR="$(dirname "$ACP_TLS_CERT_PATH")"
[ -f "$ACP_TLS_CERT_PATH" ] || fail "TLS 证书不存在: $ACP_TLS_CERT_PATH（先完成 certbot/自签 provisioning）"
[ -f "$ACP_TLS_KEY_PATH" ] || fail "TLS 私钥不存在: $ACP_TLS_KEY_PATH"

# nginx -t 职责 = 语法 / 证书 / auth file 可加载性（不含 upstream 连通性）。
# upstream 主机名（如 compose DNS 的 backend）只在 acp_net 内可解析，
# -t 环境解析不了会 emerg。因此 -t 用测试副本：upstream 替换为 127.0.0.1，
# 渲染产物本身不做任何修改。
TEST_CONF="$RUNTIME_DIR/.nginx-test.conf"
TEST_RENDERED="$RUNTIME_DIR/.nginx-test-rendered.conf"
sed "s|$ACP_UPSTREAM|http://127.0.0.1:8000|g" "$RENDERED" > "$TEST_RENDERED"

cat > "$TEST_CONF" <<EOF
events {}
http {
    server_tokens off;
    include $TEST_RENDERED;
}
EOF

"$NGINX_BIN" -t -c "$TEST_CONF" || fail "nginx -t 未通过（拒绝部署）"
rm -f "$TEST_CONF" "$TEST_RENDERED"
info "nginx -t: 通过"

info "全部检查通过。启动方式：docker compose up -d（nginx 挂载 runtime/{rendered,tls,auth}）"
