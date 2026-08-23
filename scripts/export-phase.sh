#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────────────────────
# export-phase.sh — get a campaign phase's data off the machines that are about to be destroyed.
#
# This is the script `ship` refuses paper tags in favour of, and the difference matters:
#
#   * paper tags are PRIVATE-ONLY. They name evaluation data that is not published, so putting
#     one on the public git advertises the existence and structure of unpublished work.
#   * paper tags are IMMUTABLE. `ship` runs `git tag -f`; a provenance tag that moves is worse
#     than one that is missing, because it still resolves — just to the wrong tree.
#   * paper tags go to ALL THREE private remotes. `ship` pushes `origin` and `gitlab` and has
#     never pushed `inst`, so a tag "shipped" by it exists in two places, not three.
#
# DEFAULT IS DRY RUN. Pushing publishes to remotes and is an outward-facing action; it needs
# `--push`, given deliberately, per invocation. Everything else — collecting, hashing, tagging
# locally, and verifying — happens either way, so the expensive work is already done and checked
# when approval arrives.
#
# Usage:
#   scripts/export-phase.sh --lane opsmembench-rca-lift --harvest
#   scripts/export-phase.sh --harvest --since 2026-08-23T11:00:00Z
#   scripts/export-phase.sh --tag paper/operator-model-axis/data-2026-08-26 --push
# ─────────────────────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRIV="git --git-dir=$ROOT/.git-private --work-tree=$ROOT"
REMOTES="origin gitlab inst"

LANE="" ; TAG="" ; DO_PUSH=0 ; PULL_FROM="" ; HARVEST=0 ; HARVEST_SINCE="" ; HARVEST_STEP=30
OBS_PROM="${OBS_PROM:-http://40.114.38.168:30360}"
OBS_LANGFUSE="${OBS_LANGFUSE:-http://40.114.38.168:30932}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --lane)      LANE="$2"; shift 2 ;;
    --tag)       TAG="$2"; shift 2 ;;
    --pull-from) PULL_FROM="$2"; shift 2 ;;   # comma-separated ssh hosts to collect runs from
    --push)      DO_PUSH=1; shift ;;
    --harvest)   HARVEST=1; shift ;;              # pull Langfuse + Prometheus off the observatory
    --since)     HARVEST_SINCE="$2"; shift 2 ;;   # ISO8601 window start for the harvest
    --step)      HARVEST_STEP="$2"; shift 2 ;;
    -h|--help)   sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "export-phase: unknown argument $1" >&2; exit 2 ;;
  esac
done

if [[ -n "$TAG" && "$TAG" != paper/* ]]; then
  echo "export-phase: --tag must be a paper/* tag (got '$TAG')" >&2; exit 2
fi

cd "$ROOT"

# ── 0. Harvest the VOLATILE telemetry stores ──────────────────────────────────────────────────
# These run FIRST, before anything else, because they are the only campaign data that no rerun
# can recreate. Run artifacts sit in files and survive a copy; Langfuse and Prometheus live in
# databases inside kind clusters on VMs with a deletion deadline, and the models that produced
# those traces are non-deterministic and will have moved on. Once the observatory is gone, every
# prompt, response, latency, per-call cost and resource series is gone with it — and with them
# every question a reviewer might ask that the summary tables did not anticipate.
#
# Deliberately NOT skipped quietly when credentials are missing: a silent skip here produces an
# archive that looks complete and is missing the half that cannot be regenerated.
if [[ "$HARVEST" == "1" ]]; then
  echo "=== harvesting telemetry (the half that cannot be regenerated) ==="
  TELEMETRY="$ROOT/evaluation/runs/telemetry"

  if [[ -z "${LANGFUSE_PUBLIC_KEY:-}" || -z "${LANGFUSE_SECRET_KEY:-}" ]]; then
    echo "ABORT: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set — the trace archive would be" >&2
    echo "       silently absent from an export that otherwise looks complete." >&2
    echo "       They are in ~/ki/.env on any lane VM: ssh ki-camp1 'grep LANGFUSE_ ~/ki/.env'" >&2
    exit 1
  fi

  # Array, not `${VAR:+--flag "$VAR"}`: quoting inside a `:+` expansion is subtle enough that it
  # is not worth relying on for a value that reaches an archival exporter.
  LF_ARGS=( --out "$TELEMETRY/langfuse" )
  [[ -n "$HARVEST_SINCE" ]] && LF_ARGS+=( --from-timestamp "$HARVEST_SINCE" )

  LANGFUSE_HOST="$OBS_LANGFUSE" \
  uv run --project v4 python -m evaluation.langfuse_export "${LF_ARGS[@]}"

  uv run --project v4 python -m evaluation.prometheus_export \
      --prom "$OBS_PROM" --out "$TELEMETRY/prometheus" \
      --start "${HARVEST_SINCE:-$(date -u -d '25 hours ago' +%Y-%m-%dT%H:%M:%SZ)}" \
      --end "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --step "${HARVEST_STEP:-30}"
fi

# ── 1. Collect run artifacts from the lane VMs ────────────────────────────────────────────────
# The VMs are the other volatile half. Pulling before hashing means a tag can never name data
# that only exists on a box scheduled for deletion.
if [[ -n "$PULL_FROM" ]]; then
  echo "=== collecting runs ==="
  IFS=',' read -ra HOSTS <<< "$PULL_FROM"
  for h in "${HOSTS[@]}"; do
    if rsync -az --info=stats1 "$h:~/ki/evaluation/runs/" "$ROOT/evaluation/runs/" 2>&1 | tail -2; then
      echo "  pulled $h"
    else
      # A lane that cannot be reached is REPORTED, never skipped silently: an unexported lane at
      # credit expiry is unrecoverable data, and the only defence is knowing it happened now.
      echo "  ⚠ FAILED to pull $h — this lane's data is NOT in the export" >&2
    fi
  done
fi

# ── 2. Hash every artifact ────────────────────────────────────────────────────────────────────
# Per-file hashes, plus one root hash over the sorted manifest. The root hash is what goes in the
# tag message, so a single 64-char string in `git show <tag>` fixes the entire data set.
echo "=== hashing ==="
RUNS_DIR="$ROOT/evaluation/runs"
if [[ -d "$RUNS_DIR" ]]; then
  ( cd "$RUNS_DIR" && find . -type f ! -name SHA256SUMS.txt -print0 \
      | sort -z | xargs -0 sha256sum > SHA256SUMS.txt )
  ROOT_HASH="$(sha256sum "$RUNS_DIR/SHA256SUMS.txt" | cut -d' ' -f1)"
  FILE_COUNT="$(wc -l < "$RUNS_DIR/SHA256SUMS.txt")"
  echo "  $FILE_COUNT files, root hash $ROOT_HASH"
else
  echo "  no evaluation/runs/ yet — nothing to hash"
  ROOT_HASH="" ; FILE_COUNT=0
fi

# ── 3. Commit to the PRIVATE git only ─────────────────────────────────────────────────────────
$PRIV add -A
if $PRIV diff --cached --quiet; then
  echo "=== nothing new to commit ==="
else
  $PRIV commit -q -m "campaign: export${LANE:+ — lane $LANE}${ROOT_HASH:+ (root ${ROOT_HASH:0:12}, $FILE_COUNT files)}"
  echo "=== committed $($PRIV rev-parse --short HEAD) ==="
fi

# ── 4. Tag, refusing to move an existing one ──────────────────────────────────────────────────
if [[ -n "$TAG" ]]; then
  if $PRIV rev-parse -q --verify "refs/tags/$TAG" >/dev/null 2>&1; then
    echo "ABORT: tag '$TAG' already exists. A provenance tag that moves still resolves — to the" >&2
    echo "       wrong tree — which is worse than one that is missing. Choose a new tag." >&2
    exit 1
  fi
  $PRIV tag -a "$TAG" -m "$(cat <<EOF
$TAG

Campaign: azure-2026-08-23${LANE:+ · lane $LANE}
Pre-registration: papers/paper2/PREREGISTRATION.md
Registry:         papers/paper2/campaign.yaml

Data root hash (sha256 of evaluation/runs/SHA256SUMS.txt):
  ${ROOT_HASH:-<no data in this export>}
Files covered: $FILE_COUNT

REDUCED DESIGN — 2 repeats, not 4; 27 scenarios on the model axis, not 62. See
PREREGISTRATION.md section 6. These runs must NOT be pooled with the 4x62 campaign
of record in _archives/campaign-2026-07-15.
EOF
)"
  echo "=== tagged $TAG ==="
fi

# ── 5. Push, only when explicitly asked ───────────────────────────────────────────────────────
if [[ "$DO_PUSH" != "1" ]]; then
  echo
  echo "DRY RUN — nothing pushed. Everything above is committed and tagged LOCALLY only."
  echo "To publish to the three private remotes: re-run with --push"
  exit 0
fi

echo "=== pushing to: $REMOTES ==="
PUSH_FAILED=0
for r in $REMOTES; do
  # No `|| true`. A swallowed push failure is how a phase gets reported as exported when it is
  # sitting on one laptop that is also the only copy.
  if $PRIV push "$r" HEAD 2>&1 | sed "s/^/  [$r] /"; then :; else
    echo "  ⚠ push to $r FAILED" >&2; PUSH_FAILED=1
  fi
  if [[ -n "$TAG" ]]; then
    if $PRIV push "$r" "$TAG" 2>&1 | sed "s/^/  [$r] /"; then :; else
      echo "  ⚠ tag push to $r FAILED" >&2; PUSH_FAILED=1
    fi
  fi
done

# ── 6. Verify the push actually landed ────────────────────────────────────────────────────────
# A push you did not verify is not a backup. `git push` exiting 0 means the transport succeeded,
# not that the ref is where you think it is on the far side.
if [[ -n "$TAG" ]]; then
  echo "=== verifying the tag on every remote ==="
  LOCAL_SHA="$($PRIV rev-parse "refs/tags/$TAG^{commit}")"
  for r in $REMOTES; do
    REMOTE_SHA="$($PRIV ls-remote --tags "$r" "refs/tags/$TAG^{}" 2>/dev/null | cut -f1)"
    [[ -z "$REMOTE_SHA" ]] && REMOTE_SHA="$($PRIV ls-remote --tags "$r" "refs/tags/$TAG" 2>/dev/null | cut -f1)"
    if [[ "$REMOTE_SHA" == "$LOCAL_SHA" ]]; then
      echo "  ✓ $r → ${REMOTE_SHA:0:12}"
    else
      echo "  ✗ $r → '${REMOTE_SHA:-absent}' (local ${LOCAL_SHA:0:12}) — NOT BACKED UP" >&2
      PUSH_FAILED=1
    fi
  done
fi

if [[ "$PUSH_FAILED" == "1" ]]; then
  echo; echo "EXPORT INCOMPLETE — at least one remote does not have this phase." >&2
  exit 1
fi
echo; echo "export complete${TAG:+ — $TAG verified on all three remotes}"
