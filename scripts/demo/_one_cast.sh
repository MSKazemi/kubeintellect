#!/usr/bin/env bash
# The body of a single cast: show the cluster, then ask the agent.
# Invoked by record_all.sh inside `asciinema rec --command`.
set -uo pipefail
DEMO_DIR="$1"; SCEN="$2"; BASE_URL="$3"; CONTEXT="$4"; NS="$5"; GATES="$6"; shift 6

printf '\033[1;36m$ kubectl -n %s get pods\033[0m\n' "$NS"
kubectl --context "$CONTEXT" -n "$NS" get pods
echo
printf '\033[1;36m$ kubectl -n %s get deploy,endpoints\033[0m\n' "$NS"
kubectl --context "$CONTEXT" -n "$NS" get deploy,endpoints 2>/dev/null
echo
printf '\033[1;36m$ kq chat  # KubeIntellect\033[0m\n'
echo

exec python3 "$DEMO_DIR/auto_approve_driver.py" \
  --scenario "$DEMO_DIR/scenarios/$SCEN" \
  --base-url "$BASE_URL" \
  --json-log "$GATES" \
  --think-delay 1.4 --type-cps 16 --timeout 240 "$@"
