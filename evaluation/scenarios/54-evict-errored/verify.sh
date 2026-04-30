#!/usr/bin/env bash
set -e
bad_pods=$(kubectl get pods -n scenario-test --no-headers 2>/dev/null | \
  awk '{print $3}' | grep -E "^(Error|CrashLoopBackOff)$" | wc -l)
if [ "$bad_pods" -gt 0 ]; then
  echo "FAIL: $bad_pods pods still in error state"
  exit 1
fi
echo "No Error/CrashLoopBackOff pods remain"
