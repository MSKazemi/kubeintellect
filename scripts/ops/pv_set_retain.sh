#!/usr/bin/env bash
# pv_set_retain.sh — patch all PVs bound to the kubeintellect namespace to reclaimPolicy: Retain
#
# Usage:
#   ./scripts/ops/pv_set_retain.sh           # dry-run (default)
#   ./scripts/ops/pv_set_retain.sh --apply   # apply patches
#
# Safe to run multiple times — already-Retain PVs are skipped.

set -euo pipefail

NAMESPACE="${NAMESPACE:-kubeintellect}"
DRY_RUN=true

for arg in "$@"; do
  case "$arg" in
    --apply) DRY_RUN=false ;;
    --dry-run) DRY_RUN=true ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: $0 [--apply|--dry-run]" >&2
      exit 1
      ;;
  esac
done

if $DRY_RUN; then
  echo "[DRY RUN] Pass --apply to actually patch PVs."
fi

# Collect all PVs whose claimRef.namespace matches our target namespace
PVS=$(kubectl get pv -o json | \
  jq -r --arg ns "$NAMESPACE" \
    '.items[] | select(.spec.claimRef.namespace == $ns) | .metadata.name')

if [ -z "$PVS" ]; then
  echo "No PVs found bound to namespace '$NAMESPACE'."
  exit 0
fi

PATCHED=0
SKIPPED=0

for PV in $PVS; do
  CURRENT=$(kubectl get pv "$PV" -o jsonpath='{.spec.persistentVolumeReclaimPolicy}')
  CLAIM=$(kubectl get pv "$PV" -o jsonpath='{.spec.claimRef.name}')

  if [ "$CURRENT" = "Retain" ]; then
    echo "  SKIP  $PV  (bound to $CLAIM) — already Retain"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  echo "  PATCH $PV  (bound to $CLAIM) — $CURRENT → Retain"
  if ! $DRY_RUN; then
    kubectl patch pv "$PV" \
      --type=merge \
      -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
  fi
  PATCHED=$((PATCHED + 1))
done

echo ""
if $DRY_RUN; then
  echo "Dry run complete. $PATCHED PV(s) would be patched, $SKIPPED already Retain."
  echo "Re-run with --apply to apply changes."
else
  echo "Done. $PATCHED PV(s) patched to Retain, $SKIPPED already Retain."
fi
