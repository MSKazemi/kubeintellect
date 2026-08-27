#!/usr/bin/env bash
# Record one asciinema cast per demo scenario against a live cluster.
#
# Every cast opens with the *unedited* cluster state, so a viewer can check the
# diagnosis against the same `kubectl get` output the agent had to work from --
# a demo that shows only the answer is indistinguishable from a demo that shows
# a fixture.
#
# The operator key is read from a file into the environment, never passed on the
# command line: an argv key is visible in `ps` to every user on the box and, if
# the recorded shell ever echoes its own command, would be baked into the cast.
set -uo pipefail

DEMO_DIR="${DEMO_DIR:-$HOME/demo}"
CASTS="${CASTS:-$DEMO_DIR/casts}"
BASE_URL="${BASE_URL:-http://127.0.0.1:30082}"
CONTEXT="${CONTEXT:-kind-ki-camp1-c2}"
NS="${NS:-shop}"
KEY_FILE="${KEY_FILE:-$HOME/.demo-operator-key}"

export KI_API_KEY
KI_API_KEY="$(cat "$KEY_FILE")"

mkdir -p "$CASTS"

# scenario file : extra driver args : one-line title
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
  cast="$CASTS/$stem.cast"
  gates="$CASTS/$stem.gates.jsonl"
  rm -f "$cast" "$gates"

  echo "=== recording $stem -- $title ==="
  asciinema rec "$cast" \
    --title "KubeIntellect -- $title" \
    --idle-time-limit 2.5 \
    --overwrite \
    --command "bash $DEMO_DIR/_one_cast.sh '$DEMO_DIR' '$scen' '$BASE_URL' '$CONTEXT' '$NS' '$gates' $extra" \
    </dev/null
  rc=$?
  if [ ! -s "$cast" ]; then
    echo "FAIL $stem: no cast written"
    rc_all=1
    continue
  fi
  bytes=$(stat -c %s "$cast")
  echo "OK $stem rc=$rc bytes=$bytes gates=$( [ -f "$gates" ] && wc -l < "$gates" || echo 0 )"
done
exit "$rc_all"
