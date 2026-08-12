#!/usr/bin/env bash
# verify-brew.sh — reproduce #113's two unverified commands
# Usage:
#   bash v4/packages/kube-q/scripts/verify-brew.sh              # audit only (no build)
#   bash v4/packages/kube-q/scripts/verify-brew.sh --install    # also brew install --build-from-source
# In CI without brew, exits 0 after static checks and prints what a macOS runner would do.

set -euo pipefail
# Resolve repo root (handles running from any cwd, and WSL path with spaces)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# scripts is at v4/packages/kube-q/scripts → repo root is 4 levels up
ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
# Fallback: if that doesn't contain v4/packages, try git toplevel
if [ ! -f "$ROOT/v4/packages/kube-q/Formula/kube-q.rb" ] && command -v git >/dev/null 2>&1; then
  if GIT_ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null); then
    ROOT="$GIT_ROOT"
  fi
fi
FORMULA="$ROOT/v4/packages/kube-q/Formula/kube-q.rb"
# Issue #113 references Formula/kube-q.rb at repo root pre-v4 layout; real path is v4/packages/...
# Support both for audit invocation.
ALT_FORMULA="$ROOT/Formula/kube-q.rb"

echo "=== KubeIntellect brew verification — #113 ==="
echo "Formula: $FORMULA"
ls -lh "$FORMULA"
echo

# ── Static checks that brew audit --strict would enforce (no brew needed) ──
echo "--- Static audit (no brew required) ---"
DESC=$(grep -E '^\s*desc ' "$FORMULA" | sed -E 's/.*desc "(.*)".*/\1/')
DESC_LEN=${#DESC}
echo "desc: \"$DESC\" ($DESC_LEN chars)"
if [ "$DESC_LEN" -gt 80 ]; then
  echo "FAIL: desc >80 chars — brew audit would flag"
  exit 1
else
  echo "PASS: desc length <=80"
fi

# Resource ordering
RESOURCES=$(grep -E '^  resource "' "$FORMULA" | sed -E 's/.*resource "(.*)".*/\1/')
SORTED=$(echo "$RESOURCES" | sort)
if [ "$RESOURCES" != "$SORTED" ]; then
  echo "FAIL: resources not sorted"
  echo "Current:"
  echo "$RESOURCES"
  echo "Sorted:"
  echo "$SORTED"
  exit 1
else
  echo "PASS: resources sorted alphabetically"
fi

# depends_on ordering
DEPS=$(grep -E '^\s*depends_on ' "$FORMULA" | sed -E 's/.*depends_on "(.*)".*/\1/' | cut -d'"' -f1)
SORTED_DEPS=$(echo "$DEPS" | sort)
if [ "$DEPS" != "$SORTED_DEPS" ]; then
  echo "FAIL: depends_on not sorted"
  echo "$DEPS"
  exit 1
else
  echo "PASS: depends_on sorted"
fi

# Basic Ruby syntax check if ruby available
if command -v ruby >/dev/null 2>&1; then
  ruby -c "$FORMULA" && echo "PASS: ruby -c syntax ok" || { echo "FAIL: ruby -c"; exit 1; }
else
  echo "SKIP: ruby not installed — syntax check deferred to CI"
fi

# sha256 format check (64 hex chars)
echo "Checking sha256 fields..."
BAD_SHA=$(grep -E 'sha256' "$FORMULA" | grep -vE '"[a-f0-9]{64}"' || true)
if [ -n "$BAD_SHA" ]; then
  echo "FAIL: malformed sha256:"
  echo "$BAD_SHA"
  exit 1
else
  echo "PASS: all sha256 are 64 hex"
fi

echo
if ! command -v brew >/dev/null 2>&1; then
  echo "--- brew not found — skipping brew audit/build (expected in Linux CI) ---"
  echo "To fully verify #113 on macOS or Linuxbrew:"
  echo "  brew audit --strict --online \"$FORMULA\""
  echo "  brew install --build-from-source \"$FORMULA\" && kq --version  # expect 1.5.0"
  echo
  echo "Static checks PASSED. Real brew steps require a runner with brew + rust/maturin + libyaml."
  echo " pydantic-core builds from sdist needing maturin/rust (declared as :build)"
  echo " PyYAML builds against Homebrew libyaml (declared)"
  echo " Issue #113 can be closed as 'static-pass, install pending runner' or kept open until a macOS job runs."
  exit 0
fi

echo "--- brew found: $(brew --version) ---"
echo "Running: brew audit --strict --online \"$FORMULA\""
set +e
brew audit --strict --online "$FORMULA"
AUDIT_EXIT=$?
set -e
if [ $AUDIT_EXIT -ne 0 ]; then
  echo "brew audit exited $AUDIT_EXIT — see output above"
  if [ "${1:-}" != "--install" ]; then
    exit $AUDIT_EXIT
  fi
else
  echo "PASS: brew audit --strict"
fi

if [ "${1:-}" = "--install" ]; then
  echo
  echo "Running: brew install --build-from-source \"$FORMULA\""
  brew install --build-from-source "$FORMULA"
  echo "Checking: kq --version"
  kq --version || kube-q --version || echo "kq not in PATH, try $(brew --prefix)/bin/kq --version"
  "$(brew --prefix)/bin/kq" --version 2>&1 | head -n 5
  echo "PASS: brew install + kq --version"
fi

echo "=== Done ==="
