#!/usr/bin/env bash
#
# deploy-website.sh — static website deployment from a git repository (generic)
#
# PRODUCTION SOURCE CONTRACT (deployment-source decoupling):
#   The target host must NOT depend on a persistent git working copy.
#   An EXPLICIT source is REQUIRED — exactly one of:
#     --repo <remote-url>   (cloned into an ephemeral temp dir)
#     --src  <path>         (explicit local repo path)
#   There is NO default source. If neither is given the script FAILS CLOSED.
#
#   Pin the source with --ref <sha|tag|branch>. The resolved commit is recorded
#   in <DST>.source — a non-served provenance file next to the artifact.
#   The website repository is INDEPENDENT of this repository: pass your own
#   website repo URL or local path; nothing about it is assumed.
#
# Steps (idempotent):
#   1. resolve source+ref -> commit; git archive commit -> DST (git archive
#      excludes .git and untracked files => artifact has NO .git, NO TLS key).
#   2. mkdir DST/.well-known/acme-challenge (HTTP-01).
#   3. render _deploy/nginx/website-vhost.conf.template -> rendered dir.
#   4. isolated nginx -t gate (throwaway container, no production reload).
#   5. install rendered vhost + reload nginx (unless --check / --skip-nginx).
#
# Args: (--repo URL | --src DIR) [--ref REF] [--dst DIR] [--check]
#       [--skip-nginx] [--help]
#
# Environment (explicit — the deployment-specific ones have NO defaults):
#   WEBSITE_DST          REQUIRED. Artifact directory to publish.
#   WEBSITE_TLS_HOSTDIR  REQUIRED. Host directory holding fullchain.pem /
#                        privkey.pem for the nginx -t gate (e.g. your certbot
#                        live directory for this site).
# Optional:
#   REPO_DIR             This repository's root (default: parent of _deploy/).
#   WEBSITE_TLS_CERT     Cert path INSIDE the nginx container
#                        (default: /etc/nginx/website-tls/fullchain.pem).
#   WEBSITE_TLS_KEY      Key path INSIDE the nginx container
#                        (default: /etc/nginx/website-tls/privkey.pem).
#   WEBSITE_RENDERED_DIR Where to install the rendered vhost
#                        (default: $REPO_DIR/_deploy/nginx/runtime/rendered).
#   NGINX_CONTAINER      Container to reload. REQUIRED for the reload step
#                        (no default — the script must never guess which
#                        nginx to signal).
#   NGINX_TEST_IMAGE     Throwaway image for the isolated nginx -t gate
#                        (default: nginx:alpine).
#
# Example:
#   WEBSITE_DST=/var/www/example.com \
#   WEBSITE_TLS_HOSTDIR=/etc/letsencrypt/live/example.com \
#   NGINX_CONTAINER=my-nginx-1 \
#     ./deploy-website.sh --repo https://github.com/<you>/<website>.git \
#       --ref main
set -euo pipefail

fail() { echo "[deploy-website] FAIL: $*" >&2; exit 1; }
info() { echo "[deploy-website] $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
SRC_REPO=""                 # from --repo URL
SRC_DIR="${WEBSITE_SRC:-}"  # from --src DIR / WEBSITE_SRC (NO DEFAULT)
REF=""                      # from --ref / --tag
DST="${WEBSITE_DST:-}"      # REQUIRED (no default)
TEMPLATE="${WEBSITE_TMPL:-$REPO_DIR/_deploy/nginx/website-vhost.conf.template}"
RENDERED_DIR="${WEBSITE_RENDERED_DIR:-$REPO_DIR/_deploy/nginx/runtime/rendered}"
RENDERED="$RENDERED_DIR/website-vhost.conf"
NGINX_CONTAINER="${NGINX_CONTAINER:-}"
WEBSITE_TLS_CERT="${WEBSITE_TLS_CERT:-/etc/nginx/website-tls/fullchain.pem}"
WEBSITE_TLS_KEY="${WEBSITE_TLS_KEY:-/etc/nginx/website-tls/privkey.pem}"
WEBSITE_TLS_HOSTDIR="${WEBSITE_TLS_HOSTDIR:-}"   # REQUIRED for the -t gate
NGINX_TEST_IMAGE="${NGINX_TEST_IMAGE:-nginx:alpine}"
SKIP_NGINX=0
CHECK_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-nginx) SKIP_NGINX=1; shift ;;
    --check)      CHECK_ONLY=1; shift ;;
    --repo=*) SRC_REPO="${1#*=}"; shift ;;
    --repo)   SRC_REPO="${2:-}"; shift 2 ;;
    --src=*) SRC_DIR="${1#*=}"; shift ;;
    --src)   SRC_DIR="${2:-}"; shift 2 ;;
    --ref=*) REF="${1#*=}"; shift ;;
    --ref)   REF="${2:-}"; shift 2 ;;
    --tag=*) REF="${1#*=}"; shift ;;
    --tag)   REF="${2:-}"; shift 2 ;;
    --dst=*) DST="${1#*=}"; shift ;;
    --dst)   DST="${2:-}"; shift 2 ;;
    --help|-h) sed -n '1,60p' "$0"; exit 0 ;;
    *) fail "unknown arg: $1" ;;
  esac
done

[ -n "$SRC_REPO" ] || [ -n "$SRC_DIR" ] || fail "no source given: pass --repo <url> or --src <dir> (no implicit production working copy)"
[ -f "$TEMPLATE" ] || fail "template missing: $TEMPLATE"
[ -n "$DST" ] || fail "WEBSITE_DST is required (artifact target directory)"
[ -n "$WEBSITE_TLS_HOSTDIR" ] || fail "WEBSITE_TLS_HOSTDIR is required (host directory with fullchain.pem + privkey.pem for the nginx -t gate)"

WORKDIR=""
cleanup() { [ -n "$WORKDIR" ] && rm -rf "$WORKDIR"; return 0; }
TMPD=""
cleanup_all() { [ -n "$TMPD" ] && rm -rf "$TMPD"; cleanup; }
trap cleanup_all EXIT

# ---- resolve source ----
if [ -n "$SRC_REPO" ]; then
  WORKDIR="$(mktemp -d)"
  info "cloning $SRC_REPO -> $WORKDIR/src"
  git clone --quiet "$SRC_REPO" "$WORKDIR/src" || fail "git clone failed: $SRC_REPO"
  SRC_USED="$WORKDIR/src"
else
  SRC_USED="$SRC_DIR"
  [ -d "$SRC_USED/.git" ] || fail "SRC is not a git repo: $SRC_USED"
fi

if [ -n "$REF" ]; then
  COMMIT="$(git -C "$SRC_USED" rev-parse --verify "$REF^{commit}" 2>/dev/null)" || fail "ref not found in source: $REF"
else
  COMMIT="$(git -C "$SRC_USED" rev-parse HEAD)" || fail "cannot resolve HEAD from $SRC_USED"
fi
info "source identity: commit=$COMMIT ref=${REF:-<default>} from=${SRC_REPO:-$SRC_USED}"

# ---- 1. artifact ----
mkdir -p "$DST"
find "$DST" -mindepth 1 -delete
git -C "$SRC_USED" archive --format=tar "$COMMIT" | tar -x -C "$DST"
mkdir -p "$DST/.well-known/acme-challenge"
info "artifact deployed: $(ls -A "$DST" | tr '\n' ' ')"

# provenance (non-served sibling file, next to the artifact dir)
printf 'source=%s\nref=%s\ncommit=%s\ndeployed_at=%s\n' \
  "${SRC_REPO:-$SRC_USED}" "${REF:-default}" "$COMMIT" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  > "${DST}.source"
info "provenance written: ${DST}.source"
sed 's|^|  |' "${DST}.source"

# ---- 3. render vhost ----
mkdir -p "$RENDERED_DIR"
RENDERED_TMP="$RENDERED_DIR/.website-vhost.conf.tmp"
sed -e "s|__WEBSITE_TLS_CERT__|$WEBSITE_TLS_CERT|g" \
    -e "s|__WEBSITE_TLS_KEY__|$WEBSITE_TLS_KEY|g" \
    "$TEMPLATE" > "$RENDERED_TMP"
if grep -v '^[[:space:]]*#' "$RENDERED_TMP" | grep -q '__WEBSITE_'; then
  grep -n '__WEBSITE_' "$RENDERED_TMP" || true
  fail "rendered vhost has __WEBSITE_* placeholder residue (refusing deploy)"
fi
chmod 644 "$RENDERED_TMP"

# ---- 4. nginx -t gate (isolated; never touches a running nginx) ----
if [ ! -f "$WEBSITE_TLS_HOSTDIR/fullchain.pem" ] || [ ! -f "$WEBSITE_TLS_HOSTDIR/privkey.pem" ]; then
  fail "website TLS cert missing at $WEBSITE_TLS_HOSTDIR (run certbot first)"
fi
TMPD="$(mktemp -d)"
cp "$RENDERED_TMP" "$TMPD/website-vhost.conf"
cat > "$TMPD/test.conf" <<NGINX_EOF
events {}
http {
    include /etc/nginx/conf.d/site.conf;
}
NGINX_EOF
info "running isolated nginx -t"
docker run --rm \
  -v "$TMPD/test.conf:/etc/nginx/test.conf:ro" \
  -v "$TMPD/website-vhost.conf:/etc/nginx/conf.d/site.conf:ro" \
  -v "$WEBSITE_TLS_HOSTDIR:/etc/nginx/website-tls:ro" \
  "$NGINX_TEST_IMAGE" nginx -t -c /etc/nginx/test.conf 2>&1 | sed 's|^|  |' || fail "nginx -t failed"
info "nginx -t: PASS"

if [ "$CHECK_ONLY" -eq 1 ]; then
  rm -f "$RENDERED_TMP"
  info "--check: validation passed, not installing/reloading"
  exit 0
fi

mv "$RENDERED_TMP" "$RENDERED"
info "rendered vhost installed: $RENDERED"

if [ "$SKIP_NGINX" -eq 1 ]; then
  info "--skip-nginx: not reloading"
  exit 0
fi
[ -n "$NGINX_CONTAINER" ] || fail "NGINX_CONTAINER is required to reload nginx (or pass --skip-nginx / --check)"
info "reloading nginx container: $NGINX_CONTAINER"
docker exec "$NGINX_CONTAINER" nginx -s reload || fail "nginx reload failed"
info "done"
