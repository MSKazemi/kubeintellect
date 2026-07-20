#!/usr/bin/env bash
# test-watch.sh — Re-run pytest on every Python file change in app/ or tests/.
#
# Usage:
#   ./scripts/dev/test-watch.sh              # run all tests on change
#   ./scripts/dev/test-watch.sh tests/test_foo.py  # run specific file on change
#
# Requires: uv (watchfiles is already a project dependency via uvicorn)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_TARGET="${1:-tests/}"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  test-watch: watching app/ and tests/ for changes"
echo "  running: uv run pytest ${TEST_TARGET} -x --tb=short -q"
echo "  press Ctrl+C to stop"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

cd "$REPO_ROOT"

# Run once immediately so you see the current state before any change
uv run pytest "${TEST_TARGET}" -x --tb=short -q || true

# Then re-run on every Python file change
uv run watchfiles \
  --filter python \
  "uv run pytest ${TEST_TARGET} -x --tb=short -q" \
  app/ tests/
