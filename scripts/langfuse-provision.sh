#!/usr/bin/env bash
#
# langfuse-provision.sh — auto-create ONE shared Langfuse project + API token and inject
# the keys into the root .env AND every version's .env (v2/v3/v4), so LLM cost/token
# tracing works across all versions with zero manual steps.
#
# Why one shared project (not one per version)?
#   Creating projects programmatically requires a Langfuse *Enterprise* organization API
#   key — unavailable in the free self-hosted OSS edition. Headless init (LANGFUSE_INIT_*)
#   can only seed ONE project per instance. So all versions share one project, and each
#   version stamps its traces with `version:vN` (see KI_VERSION). Per-version cost is then
#   a tag filter in Langfuse — identical data to separate projects.
#
# How it works
#   1. Generate a public key (pk-lf-<uuid>) and secret key (sk-lf-<uuid>) — once.
#   2. Write them + LANGFUSE_INIT_* seed values into the root .env (idempotent).
#   3. Fan the keys (LANGFUSE_ENABLED/HOST/PUBLIC_KEY/SECRET_KEY) into each vN/.env.
#
#   Then a fresh Langfuse (make langfuse-install, or docker compose --profile tracing)
#   seeds the project + keys on first start. All app servers authenticate with the same keys.
#
# Usage
#   bash scripts/langfuse-provision.sh                 # generate + write root + fan out
#   bash scripts/langfuse-provision.sh --force         # regenerate keys
#   bash scripts/langfuse-provision.sh --target helm   # set in-cluster LANGFUSE_HOST
#   bash scripts/langfuse-provision.sh --target compose
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
ENV_EXAMPLE="$ROOT/.env.example"

# Versions to fan keys out to (override with FANOUT="v2 v4").
FANOUT="${FANOUT:-v2 v3 v4}"

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

# Idempotent upsert of KEY=VALUE pairs into a given env file (preserves order).
upsert_env() {
  local target_file="$1"; shift
  TARGET_FILE="$target_file" python3 - "$@" <<'PY'
import os, re, sys
path = os.environ["TARGET_FILE"]
updates = {}
it = iter(sys.argv[1:])
for k in it:
    updates[k] = next(it)
try:
    lines = open(path).read().splitlines()
except FileNotFoundError:
    lines = []
seen, out = set(), []
for line in lines:
    m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=', line)
    if m and m.group(1) in updates:
        out.append(f'{m.group(1)}={updates[m.group(1)]}')
        seen.add(m.group(1))
    else:
        out.append(line)
for key, val in updates.items():
    if key not in seen:
        out.append(f'{key}={val}')
open(path, 'w').write('\n'.join(out) + ('\n' if out else ''))
PY
}

get_env() {
  local file="$1" key="$2" line val
  line="$(grep -E "^${key}=" "$file" 2>/dev/null | tail -n1 || true)"
  val="${line#*=}"; val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
  printf '%s' "$val"
}

is_placeholder() {
  case "$1" in ""|"sk-lf-change-me"|"pk-lf-change-me"|"changeme"|"your-key-here") return 0 ;; *) return 1 ;; esac
}

gen_id() { uuidgen | tr 'A-Z' 'a-z'; }

# ── Ensure root .env exists ───────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  if [ -f "$ENV_EXAMPLE" ]; then log "Creating root .env from .env.example"; cp "$ENV_EXAMPLE" "$ENV_FILE";
  else log "Creating empty root .env"; : > "$ENV_FILE"; fi
fi

CUR_PUB="$(get_env "$ENV_FILE" LANGFUSE_PUBLIC_KEY)"
CUR_SEC="$(get_env "$ENV_FILE" LANGFUSE_SECRET_KEY)"
if [ "$FORCE" -eq 1 ] || is_placeholder "$CUR_PUB" || is_placeholder "$CUR_SEC"; then
  PUBLIC_KEY="pk-lf-$(gen_id)"; SECRET_KEY="sk-lf-$(gen_id)"
  log "Generated new shared Langfuse API key pair"
else
  PUBLIC_KEY="$CUR_PUB"; SECRET_KEY="$CUR_SEC"
  ok "Reusing existing shared Langfuse keys (use --force to regenerate)"
fi

HOST="$(get_env "$ENV_FILE" LANGFUSE_HOST)"
case "$TARGET" in
  compose) HOST="$HOST_COMPOSE" ;;
  helm)    HOST="$HOST_HELM" ;;
  "")      [ -n "$HOST" ] || HOST="$HOST_HELM" ;;
  *) echo "ERROR: --target must be 'compose' or 'helm'" >&2; exit 2 ;;
esac

# ── Write root .env (full set incl. INIT_* used by make langfuse-install) ──────
log "Writing shared credentials to $ENV_FILE"
upsert_env "$ENV_FILE" \
  LANGFUSE_ENABLED "true" \
  LANGFUSE_HOST "$HOST" \
  LANGFUSE_PUBLIC_KEY "$PUBLIC_KEY" \
  LANGFUSE_SECRET_KEY "$SECRET_KEY" \
  LANGFUSE_INIT_ORG_ID "$INIT_ORG_ID" \
  LANGFUSE_INIT_ORG_NAME "$INIT_ORG_NAME" \
  LANGFUSE_INIT_PROJECT_ID "$INIT_PROJECT_ID" \
  LANGFUSE_INIT_PROJECT_NAME "$INIT_PROJECT_NAME" \
  LANGFUSE_INIT_USER_EMAIL "$INIT_USER_EMAIL" \
  LANGFUSE_INIT_USER_NAME "$INIT_USER_NAME" \
  LANGFUSE_INIT_USER_PASSWORD "$INIT_USER_PASSWORD"

# ── Fan the keys out to each version's .env (the app side) ─────────────────────
for v in $FANOUT; do
  vdir="$ROOT/$v"
  [ -d "$vdir" ] || continue
  venv="$vdir/.env"
  if [ ! -f "$venv" ] && [ -f "$vdir/.env.example" ]; then cp "$vdir/.env.example" "$venv"; fi
  # Preserve a version's own LANGFUSE_HOST if it already set a non-placeholder one.
  vhost="$(get_env "$venv" LANGFUSE_HOST)"; [ -n "$vhost" ] || vhost="$HOST"
  upsert_env "$venv" \
    LANGFUSE_ENABLED "true" \
    LANGFUSE_HOST "$vhost" \
    LANGFUSE_PUBLIC_KEY "$PUBLIC_KEY" \
    LANGFUSE_SECRET_KEY "$SECRET_KEY"
  ok "Fanned keys → $venv (host: $vhost)"
done

echo
ok "Shared Langfuse project provisioned"
printf '    project     : %s (%s)\n' "$INIT_PROJECT_NAME" "$INIT_PROJECT_ID"
printf '    public key  : %s\n' "$PUBLIC_KEY"
printf '    secret key  : %s\n' "$SECRET_KEY"
printf '    root host   : %s\n' "$HOST"
printf '    admin login : %s / %s\n' "$INIT_USER_EMAIL" "$INIT_USER_PASSWORD"
echo
log "Next:  make langfuse-install   (Kube)   |   cd vN && docker compose --profile tracing up -d  (local)"
log "Per-version cost: traces are tagged version:vN — filter by that tag in Langfuse."
