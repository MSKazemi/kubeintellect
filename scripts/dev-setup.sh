#!/usr/bin/env bash
# One-command contributor setup for KubeIntellect.
#
#   ./scripts/dev-setup.sh
#
# Installs the v4 workspace and verifies the exact gate commands CI runs, so a
# first-time contributor knows their environment is correct before they change
# anything. No cluster, API key, or Docker required — the suites are mocked.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$PWD"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
ok()   { printf '\033[1;32m    ok\033[0m  %s\n' "$1"; }
fail() { printf '\033[1;31m    FAIL\033[0m  %s\n' "$1"; }

say "Checking prerequisites"

if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 not found. Install Python 3.12+ and re-run."
  exit 1
fi
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
ok "python3 $PYV"
if [ "$(printf '%s\n3.12\n' "$PYV" | sort -V | head -1)" != "3.12" ]; then
  fail "Python 3.12+ required (found $PYV). CI runs both 3.12 and 3.13."
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (https://docs.astral.sh/uv/)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # shellcheck disable=SC1090
  export PATH="$HOME/.local/bin:$PATH"
fi
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"

say "Installing the v4 workspace (uv sync)"
cd "$ROOT/v4"
uv sync
ok "workspace installed into v4/.venv"

# ---------------------------------------------------------------------------
# Run the eight gates CI runs. Keep these in lockstep with:
#   .github/workflows/ci.yml, CONTRIBUTING.md, AGENTS.md
#
# Gates 1-4 need the virtualenv. Gates 5-8 deliberately do not — see the header
# comments in the four scripts they call. Gates 7-8 ride inside the CI job
# named "Syntax warnings", so they add no new required check.
# ---------------------------------------------------------------------------
STATUS=0

say "Gate 1/8 — ruff check (this IS the CI lint gate)"
if uv run ruff check packages/kubeintellect-server/app/ packages/ki-protocol/; then
  ok "lint clean"
else
  fail "ruff check failed"; STATUS=1
fi

say "Gate 2/8 — mypy (the workspace sits at zero errors)"
if uv run mypy packages/kubeintellect-server/app packages/ki-protocol packages/kube-q/kube_q; then
  ok "types clean"
else
  fail "mypy failed"; STATUS=1
fi

say "Gate 3/8 — server test suite"
if uv run python -m pytest tests/ -q; then
  ok "server suite passed"
else
  fail "server suite failed"; STATUS=1
fi

say "Gate 4/8 — kq CLI test suite"
if (cd packages/kube-q && uv run python -m pytest tests/ -q); then
  ok "kq suite passed"
else
  fail "kq suite failed"; STATUS=1
fi

say "Gate 5/8 — file modes (executable iff shebang)"
if (cd "$ROOT" && ./scripts/check-file-modes.sh); then
  ok "file modes clean"
else
  fail "file-mode check failed"; STATUS=1
fi

say "Gate 6/8 — syntax warnings"
if (cd "$ROOT" && ./scripts/check-syntax-warnings.py); then
  ok "no syntax warnings"
else
  fail "syntax-warning check failed"; STATUS=1
fi

say "Gate 7/8 — text-mode calls name an encoding"
if (cd "$ROOT" && ./scripts/check-text-encoding.py); then
  ok "every text-mode call names an encoding"
else
  fail "encoding check failed"; STATUS=1
fi

say "Gate 8/8 — contributor rosters agree"
if (cd "$ROOT" && ./scripts/check-contributor-roster.py); then
  ok "both contributor rosters name the same people"
else
  fail "roster check failed"; STATUS=1
fi

echo
if [ "$STATUS" -eq 0 ]; then
  cat <<'EOF'
────────────────────────────────────────────────────────────────────────────
 You are ready to contribute. All eight locally-runnable gates pass on a
 clean checkout.

 `main` requires NINE checks. The eight gates above cover six of them —
 the encoding and roster gates ride inside the "Syntax warnings" check
 rather than adding one of their own. The other three run only in CI,
 because they need a clean machine or another interpreter, so a green run
 here does not guarantee a green PR:
   • Install smoke test
   • Tests (server · py3.13)
   • Tests (kube-q CLI · py3.13)
 If your PR is red on one of those alone, it is a real failure, not flake.

 Re-run the four workspace gates any time from v4/:
   uv run ruff check packages/kubeintellect-server/app/ packages/ki-protocol/
   uv run mypy packages/kubeintellect-server/app packages/ki-protocol packages/kube-q/kube_q
   uv run python -m pytest tests/ -q
   cd packages/kube-q && uv run python -m pytest tests/ -q

 …and the four that need no virtualenv, from the repo root:
   make check-modes
   make check-syntax
   make check-encoding
   make check-roster

 Heads-up, so you don't chase pre-existing debt that is NOT your bug:
   • `make lint` fails on a clean checkout — it runs `ruff format --check`,
     which is not a CI gate and would reformat ~108 files.
   • `ruff` is pinned <0.16 on purpose (v4/pyproject.toml says why); ~317
     findings are waiting on that upgrade (measured with ruff 0.16.3 on
     2026-08-18; re-measure rather than trusting this number, it drifts with
     the code), tracked in issue #75. Do not bump
     the pin in a PR that is about something else — and never run a bare
     `ruff check --fix`: the UP045 autofix silently disables RBAC and the
     human-in-the-loop gate. See AGENTS.md safety invariant #6.
   • `mypy` IS clean and enforced — if it complains, it is from your change.
   • CI runs the suites on Python 3.12 AND 3.13; this script uses whichever
     python3 you have. If CI fails only on one of them, that is the bug.

 Pick a first issue:
   https://github.com/MSKazemi/kubeintellect/contribute
────────────────────────────────────────────────────────────────────────────
EOF
else
  cat <<'EOF'
────────────────────────────────────────────────────────────────────────────
 Setup finished but at least one gate failed on a CLEAN checkout.
 That is a bug in the project, not in your setup — please tell us:
   https://github.com/MSKazemi/kubeintellect/issues/new/choose
────────────────────────────────────────────────────────────────────────────
EOF
fi
exit "$STATUS"
