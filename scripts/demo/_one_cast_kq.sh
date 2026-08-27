#!/usr/bin/env bash
# Body of one kq cast: show the cluster, then hand over to the REPL.
set -uo pipefail
REPO="$1"; SCEN="$2"; URL="$3"; CONTEXT="$4"; NS="$5"; GATES="$6"; shift 6
printf '\033[1;36m$ kubectl -n %s get pods\033[0m\n' "$NS"
kubectl --context "$CONTEXT" -n "$NS" get pods
echo
printf '\033[1;36m$ kubectl -n %s get deploy,endpoints\033[0m\n' "$NS"
kubectl --context "$CONTEXT" -n "$NS" get deploy,endpoints 2>/dev/null
echo
printf '\033[1;36m$ kq\033[0m\n'
sleep 1
exec python3 "$REPO/scripts/demo/kq_pty_driver.py" \
  --scenario "$REPO/scripts/demo/scenarios/$SCEN" --url "$URL" --json-log "$GATES" \
  --quiet 3.0 --timeout 300 --type-cps 18 --read-pause 2.5 "$@"
