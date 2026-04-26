#!/usr/bin/env bash
# run_query.sh — Send a query to KubeIntellect and immediately analyze its trace.
#
# Usage:
#   ./tests/investigate/run_query.sh "list all pods"
#   ./tests/investigate/run_query.sh "show errors in kubeintellect logs"
#   KUBE_Q_URL=http://localhost:8000 ./tests/investigate/run_query.sh "list nodes"
#
# What it does:
#   1. Sends query via kq CLI (streaming, shows output live)
#   2. Waits 4s for Langfuse to ingest the trace
#   3. Runs analyze_trace.py --latest to produce a structured report
#   4. Appends findings to .claude/plans/investigation-findings.md

set -euo pipefail

QUERY="${1:-}"
if [[ -z "$QUERY" ]]; then
  echo "Usage: $0 \"your query here\""
  exit 1
fi

KUBE_Q_URL="${KUBE_Q_URL:-http://api.kubeintellect.local}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  QUERY: $QUERY"
echo "  URL:   $KUBE_Q_URL"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Run query (streaming output to terminal)
cd "$PROJECT_ROOT"
KUBE_Q_URL="$KUBE_Q_URL" uv run kq --query "$QUERY" || true

echo ""
echo "  [investigate] Waiting 4s for Langfuse trace ingestion..."
sleep 4

echo "  [investigate] Fetching and analyzing trace..."
echo ""
uv run python tests/investigate/analyze_trace.py --latest
