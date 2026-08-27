#!/usr/bin/env bash
# Record one cast per scenario through the real `kq` REPL.
#
# Supersedes record_all.sh, which drove the HTTP API directly and therefore recorded the raw
# SSE stream -- literal `###` and `**bold**` where a user sees rendered markdown. This drives
# the client a person actually runs.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CASTS="${CASTS:-$REPO/scripts/demo/casts-kq}"
URL="${URL:-http://127.0.0.1:30080}"
CONTEXT="${CONTEXT:-kind-ki-demo}"
NS="${NS:-shop}"
KEY_FILE="${KEY_FILE:-$HOME/.ki-demo-operator-key}"
COLS="${COLS:-100}"
ROWS="${ROWS:-34}"

export PATH="$REPO/v4/.venv/bin:$PATH"
export KUBE_Q_API_KEY; KUBE_Q_API_KEY="$(cat "$KEY_FILE")"
mkdir -p "$CASTS"

SCENARIOS=(
  "01-crashloop.txt::A pod that will not stay up"
  "02-stuck-rollout.txt::A rollout that never finished"
  "03-oomkill.txt::A worker the kernel keeps killing"
  "04-silent-service.txt::A Service with no endpoints"
  "05-pending-pod.txt::A pod that will never schedule"
  "06-approval-gate.txt::Human-in-the-loop: approve"
  "07-approval-denied.txt:--deny-nth 1:Human-in-the-loop: deny"
  "08-complex-triage.txt::Triage a whole broken namespace"
)

rc_all=0
for entry in "${SCENARIOS[@]}"; do
  IFS=':' read -r scen extra title <<<"$entry"
  stem="${scen%.txt}"
  cast="$CASTS/$stem.cast"; gates="$CASTS/$stem.gates.jsonl"
  rm -f "$cast" "$gates"
  echo "=== $stem -- $title ==="
  # --cols/--rows pin the recorded geometry instead of inheriting whatever terminal
  # happened to launch this. The driver sizes the REPL's pty from the same terminal,
  # so the layout kq renders for and the geometry the cast header declares are the
  # same number by construction, not by coincidence.
  asciinema rec "$cast" --title "KubeIntellect -- $title" --idle-time-limit 2.5 --overwrite \
    --cols "$COLS" --rows "$ROWS" \
    --command "bash $REPO/scripts/demo/_one_cast_kq.sh '$REPO' '$scen' '$URL' '$CONTEXT' '$NS' '$gates' $extra" \
    </dev/null
  if [ ! -s "$cast" ]; then echo "FAIL $stem: no cast"; rc_all=1; continue; fi
  # Freeze the prompts beside the cast. A recording has to be checkable against what was sent
  # to it; scenario files get edited, and then a test comparing a frozen cast to the live file
  # fails for a reason that has nothing to do with the recording.
  cp "$REPO/scripts/demo/scenarios/$scen" "$CASTS/$stem.prompts.txt"
  echo "OK $stem bytes=$(stat -c %s "$cast") gates=$( [ -f "$gates" ] && wc -l < "$gates" || echo 0 )"
done
exit "$rc_all"
