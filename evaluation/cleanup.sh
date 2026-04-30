#!/usr/bin/env bash
# Cleanup all scenario-test resources between runs.
# Run this before re-running any scenario to avoid resource conflicts.
#
# Usage:
#   bash tests/scenarios/cleanup.sh             # clean scenario-test only
#   bash tests/scenarios/cleanup.sh --all       # also clean loop-test (scenario 09)

set -euo pipefail

echo "==> Cleaning up scenario-test namespace..."
kubectl delete namespace scenario-test --ignore-not-found --timeout=60s
echo "    Done."

if [[ "${1:-}" == "--all" ]]; then
  echo "==> Cleaning up loop-test namespace (scenario 09)..."
  kubectl delete namespace loop-test --ignore-not-found --timeout=60s
  echo "    Done."
fi

echo ""
echo "==> Cluster state after cleanup:"
kubectl get namespaces | grep -E "scenario-test|loop-test" || echo "    (none — clean)"
