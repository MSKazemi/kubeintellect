#!/usr/bin/env bash
set -e
succeeded=$(kubectl get pods -A --field-selector=status.phase=Succeeded -o name 2>/dev/null | wc -l)
if [ "$succeeded" -gt 0 ]; then
  echo "FAIL: $succeeded Succeeded pods remain"
  exit 1
fi
echo "No Succeeded pods remain"
