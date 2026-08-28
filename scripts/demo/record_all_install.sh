#!/usr/bin/env bash
# Record the installation casts. Companion to record_all_kq.sh, same geometry and renderer.
#
# ⛔ Two things here are load-bearing and both were found by running the wizard, not reading it:
#
#  1. HOME is a FRESH directory for every cast. `kubeintellect init` gates its Kind-cluster branch
#     on ~/.kube/config being absent, so recording as yourself silently records a different wizard.
#  2. The key is a THROWAWAY. `init` echoes what is typed, and `_mask` shows first-4 + last-4
#     (`sk-d***0000`) -- eight characters of a live key would be baked into the cast forever.
#     Nothing in scenario 09 sends a request, so a placeholder is honest; 10 needs a real one and
#     must therefore rely on the environment (empty answer = keep the env value) rather than typing.
#  3. The body runs with cwd = the fresh HOME, NOT the repo. `cmd_init` reads a `.env` from the
#     CURRENT DIRECTORY at priority 2 (below its own config, above the environment). Recorded from
#     the repo root, the "fresh install" silently inherited this repo's PROMETHEUS_URL and LOKI_URL
#     and the cast showed a machine that had never had a cluster reporting `172.18.0.2:30090
#     unreachable`. It did not look broken. That is the failure mode this whole corpus is about.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CASTS="${CASTS:-$REPO/scripts/demo/casts-kq}"
COLS="${COLS:-100}"
ROWS="${ROWS:-34}"
PYBIN="${PYBIN:-$REPO/v4/.venv/bin/python}"   # install_pty_driver.py needs pyte
DEMO_KEY="${DEMO_KEY:-sk-demo-0000000000000000000000000000000000000000000}"

mkdir -p "$CASTS"

SCENARIOS=(
  "09-install.txt::Installing KubeIntellect"
)

rc_all=0
for entry in "${SCENARIOS[@]}"; do
  IFS=':' read -r scen _extra title <<<"$entry"
  stem="${scen%.txt}"
  cast="$CASTS/$stem.cast"; answers="$CASTS/$stem.answers.jsonl"
  rm -f "$cast" "$answers"
  # A SHORT, clean HOME. The first recording used a mktemp path under the session scratchpad and
  # the cast was unusable: every ✓ line, the config path, the kubeconfig warning and six lines of
  # `status` output carried a 120-character machine-specific directory. A demo is a picture of a
  # user's machine, and that was a picture of mine.
  fresh="/tmp/kubeintellect-demo-home"
  rm -rf "$fresh"; mkdir -p "$fresh"
  echo "=== $stem -- $title  (HOME=$fresh) ==="
  env HOME="$fresh" ANSWERS="$answers" OPENAI_API_KEY="$DEMO_KEY" \
      PATH="$fresh/.local/bin:$PATH" \
    asciinema rec "$cast" --title "KubeIntellect -- $title" --idle-time-limit 2.5 --overwrite \
      --cols "$COLS" --rows "$ROWS" \
      --command "cd '$fresh' && bash $REPO/scripts/demo/_one_cast_install.sh '$REPO' '$scen' '$PYBIN'" \
      </dev/null
  if [ ! -s "$cast" ]; then echo "FAIL $stem: no cast"; rc_all=1; continue; fi
  cp "$REPO/scripts/demo/scenarios/$scen" "$CASTS/$stem.prompts.txt"
  echo "OK $stem bytes=$(stat -c %s "$cast") answers=$( [ -f "$answers" ] && wc -l < "$answers" || echo 0 )"
  echo "   (throwaway HOME at $fresh -- it is recreated on every run)"
done
exit "$rc_all"
