#!/usr/bin/env bash
# Body of one installation cast: install the published package, run the wizard, show the result.
#
# Deliberately NOT _one_cast_kq.sh with different arguments. That script shows a cluster and hands
# over to the REPL; an installation has no cluster to show yet -- showing one would be the demo
# lying about its own starting state. The two share the recorder and the geometry, nothing else.
#
# Runs entirely inside $HOME, which the caller sets to a fresh directory. That is a correctness
# requirement, not hygiene: `kubeintellect init` only offers to create a Kind cluster when
# ~/.kube/config is MISSING, so recording as a user who has ever touched a cluster records a
# different wizard -- one that never mentions Kind, convincingly.
set -uo pipefail
REPO="$1"; SCEN="$2"; PYBIN="$3"

say() { printf '\033[1;36m$ %s\033[0m\n' "$1"; }

say "uv tool install kubeintellect"
# `tail -n` opens the cast mid-dependency-list ("+ websockets==16.1.1"), which reads as a glitch.
# Keep the lines that say what happened, drop the hundred that say which wheels were unpacked.
uv tool install kubeintellect 2>&1 | grep -E "^(Resolved|Prepared|Installed|Installing|Using)" 
export PATH="$HOME/.local/bin:$PATH"
echo

say "kubeintellect --version"
kubeintellect --version
echo

say "kubeintellect init"
sleep 1
# The wizard is plain input(); the driver answers it through a pty and fails non-zero if a prompt
# never arrives. See install_pty_driver.py for why prompt-text matching is right here and silence
# (which the kq driver uses) is not.
"$PYBIN" "$REPO/scripts/demo/install_pty_driver.py" \
  --scenario "$REPO/scripts/demo/scenarios/$SCEN" \
  --command 'kubeintellect init' \
  --json-log "${ANSWERS:-/dev/null}" --timeout 180 --read-pause 1.2 --type-cps 18
rc=$?
echo

say "kubeintellect status"
kubeintellect status
exit "$rc"
