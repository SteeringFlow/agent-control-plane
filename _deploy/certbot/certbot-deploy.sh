#!/usr/bin/env bash
#
# certbot-deploy.sh — TLS certificate deploy hook (generic)
#
# Purpose: after `certbot renew`, deploy the renewed certificate material into
# the directories your nginx (in Docker or on the host) actually reads, then
# validate and reload nginx. Designed to run as a certbot `--deploy-hook` or
# standalone after a renewal.
#
# Safety contract:
#   * never replaces a deployed certificate unless the renewed lineage is
#     actually newer;
#   * never reloads nginx unless `nginx -t` passes;
#   * fail-closed: any required configuration missing -> non-zero exit before
#     any file is touched.
#
# Configuration (environment variables — no hard-coded hosts, domains or paths):
#
#   LE_LIVE            Where certbot keeps live material
#                      (default: /etc/letsencrypt/live)
#   ACP_CERT_NAME      Certificate name (the directory under $LE_LIVE) whose
#                      fullchain/privkey should be deployed to $ACP_TLS.
#                      Empty -> that deployment target is skipped.
#   WEB_CERT_NAME      Same, for a second (e.g. website) certificate.
#                      Empty -> skipped. You can run with either, both, or
#                      neither target configured.
#   ACP_TLS            Deployment target directory for $ACP_CERT_NAME
#                      (default: /etc/nginx/acp-tls)
#   WEB_TLS            Deployment target directory for $WEB_CERT_NAME
#                      (default: /etc/nginx/website-tls)
#   NGINX_CONTAINER   Container (or host service context) used to run
#                      `nginx -t` / `nginx -s reload`. REQUIRED — there is no
#                      default, so the script can never reload the wrong
#                      nginx. On a host nginx, set NGINX_CMD="nginx".
#   NGINX_CMD          Command prefix used to reach nginx
#                      (default: docker exec "$NGINX_CONTAINER")
#
# Example (single domain, container nginx):
#   ACP_CERT_NAME=app.example.com \
#   ACP_TLS=/etc/nginx/acp-tls \
#   NGINX_CONTAINER=my-nginx-1 \
#     ./certbot-deploy.sh
#
# Example (host nginx, no docker):
#   ACP_CERT_NAME=app.example.com \
#   ACP_TLS=/etc/nginx/acp-tls \
#   NGINX_CMD="nginx" \
#     ./certbot-deploy.sh
set -euo pipefail

fail() { echo "[certbot-deploy] FAIL: $*" >&2; exit 1; }
info() { echo "[certbot-deploy] $*"; }

LE_LIVE="${LE_LIVE:-/etc/letsencrypt/live}"
ACP_CERT_NAME="${ACP_CERT_NAME:-}"
WEB_CERT_NAME="${WEB_CERT_NAME:-}"
ACP_TLS="${ACP_TLS:-/etc/nginx/acp-tls}"
WEB_TLS="${WEB_TLS:-/etc/nginx/website-tls}"
NGINX_CONTAINER="${NGINX_CONTAINER:-}"
NGINX_CMD="${NGINX_CMD:-}"

# NGINX_CMD defaults to docker exec against the (required) container name.
if [ -z "$NGINX_CMD" ]; then
  [ -n "$NGINX_CONTAINER" ] || fail "NGINX_CONTAINER is required (or set NGINX_CMD, e.g. NGINX_CMD=nginx for host nginx)"
  NGINX_CMD="docker exec $NGINX_CONTAINER"
fi

changed=0

sync_cert() {
  local cert_name="$1" dst="$2" label="$3"
  local src="$LE_LIVE/$cert_name"
  if [ ! -f "$src/fullchain.pem" ] || [ ! -f "$src/privkey.pem" ]; then
    fail "$label: source certificate missing at $src (run certbot renew first)"
  fi
  # Only act if the renewed cert is newer than the deployed one.
  if [ -f "$dst/fullchain.pem" ] && \
     [ "$src/fullchain.pem" -ot "$dst/fullchain.pem" ] && \
     [ "$src/privkey.pem"  -ot "$dst/privkey.pem" ]; then
    info "$label: deployed cert already current, skip"
    return
  fi
  mkdir -p "$dst"
  cp "$src/fullchain.pem" "$dst/fullchain.pem"; chmod 644 "$dst/fullchain.pem"
  cp "$src/privkey.pem"  "$dst/privkey.pem";  chmod 600 "$dst/privkey.pem"
  info "$label: copied fullchain(0644) + privkey(0600) -> $dst"
  changed=1
}

if [ -n "$ACP_CERT_NAME" ]; then
  sync_cert "$ACP_CERT_NAME" "$ACP_TLS" "ACP($ACP_CERT_NAME)"
else
  info "ACP_CERT_NAME not set: skipping primary certificate target"
fi

if [ -n "$WEB_CERT_NAME" ]; then
  sync_cert "$WEB_CERT_NAME" "$WEB_TLS" "Website($WEB_CERT_NAME)"
else
  info "WEB_CERT_NAME not set: skipping website certificate target"
fi

if [ "$changed" -eq 0 ]; then
  info "nothing to deploy; no nginx reload needed"
  exit 0
fi

info "running: $NGINX_CMD nginx -t"
if $NGINX_CMD nginx -t; then
  info "nginx -t PASS; reloading"
  $NGINX_CMD nginx -s reload
  info "nginx reload issued"
else
  fail "nginx -t FAILED; NOT reloading"
fi
info "done"
