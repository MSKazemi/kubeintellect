#!/usr/bin/env bash
# log-watcher.sh — Dev-phase log monitor for KubeIntellect.
#
# Start this once at the beginning of a dev session:
#   bash scripts/dev/log-watcher.sh
#
# It tails kubeintellect-core logs, detects when a request cycle ends
# (SILENCE_TIMEOUT seconds of silence), analyzes the logs with Claude,
# appends findings to company/inputs/ideas/backlog.md, and prints them to your terminal.
#
# Rules, patterns, thresholds, and LLM prompts are controlled by:
#   scripts/dev/log-pipeline.conf
#
# Requires: kubectl (pointing at the right cluster), claude CLI

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

CONF="$REPO_ROOT/scripts/dev/log-pipeline.conf"
if [[ ! -f "$CONF" ]]; then
    echo "ERROR: $CONF not found" >&2; exit 1
fi
# shellcheck source=scripts/dev/log-pipeline.conf
source "$CONF"

NAMESPACE="$LOG_NAMESPACE"
DEPLOYMENT="$LOG_DEPLOYMENT"
BACKLOG="$REPO_ROOT/company/inputs/ideas/backlog.md"

# ── Startup checks ────────────────────────────────────────────────────────────
if ! command -v kubectl &>/dev/null; then
    echo "ERROR: kubectl not found in PATH" >&2; exit 1
fi
if ! command -v claude &>/dev/null; then
    echo "ERROR: claude CLI not found in PATH" >&2; exit 1
fi
if ! kubectl cluster-info --request-timeout=3s &>/dev/null 2>&1; then
    echo "ERROR: Kubernetes cluster not reachable" >&2; exit 1
fi
if ! kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" &>/dev/null 2>&1; then
    echo "ERROR: deployment $DEPLOYMENT not found in namespace $NAMESPACE" >&2; exit 1
fi


# ── Helpers ───────────────────────────────────────────────────────────────────
separator() { printf '\n%s\n' "────────────────────────────────────────────────────"; }

analyze_and_record() {
    local buffer="$1"
    local line_count
    line_count=$(echo "$buffer" | wc -l)

    if (( line_count < MIN_LINES_FOR_ANALYSIS )); then
        return
    fi

    # Build prompt via Python to safely handle multi-line buffers with special chars
    local PROMPT
    PROMPT=$(python3 - "$buffer" "$PROMPT_WATCHER_TEMPLATE" \
        "$PROMPT_SYSTEM_CONTEXT" "$ROADMAP_ITEM_FORMAT" "$SEMANTIC_PATTERN" << 'PYEOF'
import sys
buffer, template, system_context, roadmap_fmt, semantic_pattern = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
prompt = template.replace("{BUFFER}", buffer)
prompt = prompt.replace("{SYSTEM_CONTEXT}", system_context)
prompt = prompt.replace("{ROADMAP_ITEM_FORMAT}", roadmap_fmt)
prompt = prompt.replace("{SEMANTIC_PATTERN}", semantic_pattern)
print(prompt)
PYEOF
)

    local ANALYSIS
    ANALYSIS=$(claude --dangerously-skip-permissions -p "$PROMPT" 2>/dev/null || true)

    if [[ -z "$ANALYSIS" ]] || [[ "$ANALYSIS" == "NO_ISSUES" ]]; then
        return
    fi

    local TIMESTAMP
    TIMESTAMP=$(date -u +"%Y-%m-%d %H:%M UTC")

    separator
    echo "  KubeIntellect request analyzed — $TIMESTAMP"
    separator
    echo ""
    echo "$ANALYSIS"
    echo ""

    # Insert log-watcher findings into backlog.md (deduped by title)
    python3 - "$BACKLOG" "$ANALYSIS" "$TIMESTAMP" << 'PYEOF'
import sys, re

path, analysis, ts = sys.argv[1], sys.argv[2], sys.argv[3]

with open(path) as f:
    content = f.read()

# Deduplicate: collect only items whose bold title does not already exist in the file
new_items = []
for line in analysis.splitlines():
    m = re.search(r'\*\*([^*]+)\*\*', line)
    if line.startswith("- [ ] **") and m:
        title = m.group(1)
        if title not in content:
            new_items.append(line)

if not new_items:
    print("(Skipped: no new items — all titles already in backlog.md)")
    sys.exit(0)

# Build a compact entry block with only the new deduplicated items
entry_lines = [
    f"\n<!-- log-watcher: {ts} -->",
    f"### Log Watcher — {ts}",
    "",
] + new_items + ["", "---", ""]
entry = "\n".join(entry_lines)

# Insert before the "## Future / Backlog" section so findings stay grouped with backlog items.
# Falls back to appending at end of file if the marker is not found.
marker = re.search(r'\n## Future / Backlog', content)
insert_pos = marker.start() if marker else len(content)
with open(path, 'w') as f:
    f.write(content[:insert_pos] + entry + content[insert_pos:])
print(f"(Added {len(new_items)} new item(s) to backlog.md — {ts})")
PYEOF
}

# ── Main loop ─────────────────────────────────────────────────────────────────
echo ""
echo "KubeIntellect log watcher started"
echo "  Deployment : $NAMESPACE/$DEPLOYMENT"
echo "  Backlog    : $BACKLOG"
echo "  Silence gap: ${SILENCE_TIMEOUT}s = fallback end-of-request (pattern-based flush is primary)"
echo "  Config     : $CONF"
echo ""
echo "Waiting for KubeIntellect interactions... (Ctrl+C to stop)"
separator

BUFFER=""
REQUEST_COUNT=0
# Pattern that marks the definitive end of a request cycle in KubeIntellect logs.
# When matched we flush immediately without waiting for SILENCE_TIMEOUT.
# Covers streaming success (stream_complete), streaming error (stream_error),
# non-streaming success, and non-streaming error paths.
REQUEST_END_PATTERN="stream_complete|stream_error|Successfully formatted non-streaming response|Workflow error for non-streaming"

while true; do
    # Stream logs; inner read has SILENCE_TIMEOUT as a fallback timeout.
    # We also check each line for REQUEST_END_PATTERN and flush early when found —
    # this captures the full request cycle even when LLM calls take longer than
    # SILENCE_TIMEOUT seconds between log lines.
    while IFS= read -r -t "$SILENCE_TIMEOUT" line 2>/dev/null; do
        BUFFER+="$line"$'\n'
        if echo "$line" | grep -qE "$REQUEST_END_PATTERN"; then
            break  # Request complete — flush now, don't wait for silence
        fi
    done < <(kubectl logs -n "$NAMESPACE" "deployments/$DEPLOYMENT" -f --tail=0 2>/dev/null)

    # read timed out OR kubectl exited — process buffered lines if any
    if [[ -n "$BUFFER" ]]; then
        (( REQUEST_COUNT++ )) || true
        echo ""
        echo ">>> Request #$REQUEST_COUNT detected ($(echo "$BUFFER" | wc -l) log lines)"
        analyze_and_record "$BUFFER"
        BUFFER=""
    else
        # kubectl exited (pod restarted or cluster issue) — reconnect after a pause
        echo "[$(date -u +%H:%M:%S)] Connection lost — reconnecting in 5s..."
        sleep 5
    fi
done
