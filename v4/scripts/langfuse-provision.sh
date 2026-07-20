#!/usr/bin/env bash
#
# langfuse-provision.sh — auto-create a Langfuse project + API token and inject the
# keys into KubeIntellect's .env, so LLM cost/token tracing works with zero manual steps.
#
# How it works
# ------------
# Langfuse supports "headless initialization": if the server boots with a set of
# LANGFUSE_INIT_* env vars, it creates the org / project / admin user / API keys on
# first startup (idempotent — re-running leaves existing records untouched). This script
# is the single source of truth for those credentials:
#
#   1. Generate a public key (pk-lf-<uuid>) and secret key (sk-lf-<uuid>) — once.
#   2. Write them, plus the LANGFUSE_INIT_* seed values, into .env (idempotent upsert).
#
# Both deployment paths then consume the same .env:
#   • docker compose --profile tracing  — the langfuse service reads LANGFUSE_INIT_* and
#     seeds the project; the app reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY.
#   • make langfuse-install / kind-deploy-kubeintellect — the Makefile --sets the same
#     keys into both Helm charts.
#
# Usage
# -----
#   bash scripts/langfuse-provision.sh                 # generate + write to .env (idempotent)
#   bash scripts/langfuse-provision.sh --force         # regenerate keys even if present
#   bash scripts/langfuse-provision.sh --target compose   # set LANGFUSE_HOST for docker compose
#   bash scripts/langfuse-provision.sh --target helm      # set LANGFUSE_HOST for in-cluster Helm
#   ENV_FILE=/path/to/.env bash scripts/langfuse-provision.sh
#
set -euo pipefail

# ── Resolve paths ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$PROJECT_ROOT/.env}"
ENV_EXAMPLE="$PROJECT_ROOT/.env.example"

# ── Defaults (override via env) ───────────────────────────────────────────────
INIT_ORG_ID="${LANGFUSE_INIT_ORG_ID:-kubeintellect-org}"
INIT_ORG_NAME="${LANGFUSE_INIT_ORG_NAME:-KubeIntellect}"
INIT_PROJECT_ID="${LANGFUSE_INIT_PROJECT_ID:-kubeintellect-project}"
INIT_PROJECT_NAME="${LANGFUSE_INIT_PROJECT_NAME:-KubeIntellect}"
INIT_USER_EMAIL="${LANGFUSE_INIT_USER_EMAIL:-admin@kubeintellect.local}"
INIT_USER_NAME="${LANGFUSE_INIT_USER_NAME:-Admin}"
INIT_USER_PASSWORD="${LANGFUSE_INIT_USER_PASSWORD:-langfuse-admin}"

FORCE=0
TARGET=""
HOST_COMPOSE="http://localhost:3001"
HOST_HELM="http://langfuse-web.monitoring.svc.cluster.local:3000"

# ── Parse args ────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --target) TARGET="${2:-}"; shift 2 ;;
    --target=*) TARGET="${1#*=}"; shift ;;
    -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[36m▸\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*" >&2; }

# ── Ensure .env exists ────────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  if [ -f "$ENV_EXAMPLE" ]; then
    log "No .env found — creating one from .env.example"
    cp "$ENV_EXAMPLE" "$ENV_FILE"
  else
    log "No .env found — creating an empty one"
    : > "$ENV_FILE"
  fi
fi

# ── Read a current value from .env (last assignment wins; strips quotes) ───────
get_env() {
  local key="$1" line val
  line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n1 || true)"
  val="${line#*=}"
  val="${val%\"}"; val="${val#\"}"
  val="${val%\'}"; val="${val#\'}"
  printf '%s' "$val"
}

# ── Decide whether existing keys are usable ───────────────────────────────────
is_placeholder() {
  case "$1" in
    ""|"sk-lf-change-me"|"pk-lf-change-me"|"changeme"|"your-key-here") return 0 ;;
    *) return 1 ;;
  esac
}

CUR_PUB="$(get_env LANGFUSE_PUBLIC_KEY)"
CUR_SEC="$(get_env LANGFUSE_SECRET_KEY)"

gen_id() { uuidgen | tr 'A-Z' 'a-z'; }

if [ "$FORCE" -eq 1 ] || is_placeholder "$CUR_PUB" || is_placeholder "$CUR_SEC"; then
  PUBLIC_KEY="pk-lf-$(gen_id)"
  SECRET_KEY="sk-lf-$(gen_id)"
  log "Generated new Langfuse API key pair"
else
  PUBLIC_KEY="$CUR_PUB"
  SECRET_KEY="$CUR_SEC"
  ok "Reusing existing Langfuse keys in .env (use --force to regenerate)"
fi

# ── Choose LANGFUSE_HOST ──────────────────────────────────────────────────────
HOST="$(get_env LANGFUSE_HOST)"
case "$TARGET" in
  compose) HOST="$HOST_COMPOSE" ;;
  helm)    HOST="$HOST_HELM" ;;
  "")      [ -n "$HOST" ] || HOST="$HOST_COMPOSE" ;;
  *) echo "ERROR: --target must be 'compose' or 'helm'" >&2; exit 2 ;;
esac

# ── Idempotent .env upsert (preserves order, appends new keys) ─────────────────
upsert_env() {
  ENV_FILE="$ENV_FILE" python3 - "$@" <<'PY'
import os, re, sys
path = os.environ["ENV_FILE"]
updates = {}
it = iter(sys.argv[1:])
for k in it:
    updates[k] = next(it)
try:
    lines = open(path).read().splitlines()
except FileNotFoundError:
    lines = []
seen = set()
out = []
for line in lines:
    m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=', line)
    if m and m.group(1) in updates:
        key = m.group(1)
        out.append(f'{key}={updates[key]}')
        seen.add(key)
    else:
        out.append(line)
for key, val in updates.items():
    if key not in seen:
        out.append(f'{key}={val}')
open(path, 'w').write('\n'.join(out) + '\n')
PY
}

log "Writing Langfuse credentials to $ENV_FILE"
upsert_env \
  LANGFUSE_ENABLED               "true" \
  LANGFUSE_HOST                  "$HOST" \
  LANGFUSE_PUBLIC_KEY            "$PUBLIC_KEY" \
  LANGFUSE_SECRET_KEY            "$SECRET_KEY" \
  LANGFUSE_INIT_ORG_ID          "$INIT_ORG_ID" \
  LANGFUSE_INIT_ORG_NAME        "$INIT_ORG_NAME" \
  LANGFUSE_INIT_PROJECT_ID      "$INIT_PROJECT_ID" \
  LANGFUSE_INIT_PROJECT_NAME    "$INIT_PROJECT_NAME" \
  LANGFUSE_INIT_USER_EMAIL      "$INIT_USER_EMAIL" \
  LANGFUSE_INIT_USER_NAME       "$INIT_USER_NAME" \
  LANGFUSE_INIT_USER_PASSWORD   "$INIT_USER_PASSWORD"

echo
ok "Langfuse project provisioned in .env"
printf '    project        : %s (%s)\n' "$INIT_PROJECT_NAME" "$INIT_PROJECT_ID"
printf '    public key     : %s\n' "$PUBLIC_KEY"
printf '    secret key     : %s\n' "$SECRET_KEY"
printf '    host           : %s\n' "$HOST"
printf '    admin login    : %s / %s\n' "$INIT_USER_EMAIL" "$INIT_USER_PASSWORD"
echo
log "Next:"
echo "    • docker compose --profile tracing up -d     # local: langfuse seeds the project"
echo "    • make langfuse-install                       # Kind/Helm: seeds via --set from .env"
echo "    • make kind-deploy-kubeintellect             # app picks up the keys from .env"
