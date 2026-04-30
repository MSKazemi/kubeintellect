"""Detect when the agent self-gated and the runner should follow up.

Pure functions, no I/O. Used by `runner.py` to decide whether to send a
"yes, please apply the fix" reply on the same `X-Session-ID` after the
first turn returns.
"""
from __future__ import annotations

import re

DEFAULT_FOLLOW_UP = "yes, please apply the fix"

# Tool names that mean the agent already wrote.
# Note: `run_kubectl` is also used for read-only `get`/`describe`. Disambiguating
# by command would require trace.tool_spans inspection, which the runner has
# access to but `agent_self_gated` does not. Keep this conservative: if a write
# tool fired, we only follow up when a gate pattern *also* matches — that means
# the agent acted then asked a follow-up question (rare).
_WRITE_TOOLS: frozenset[str] = frozenset({"run_kubectl"})

# Patterns scanned in the last 400 chars of `final_text` only, so we don't
# false-positive on phrases like "would you like me to..." appearing
# mid-explanation.
_GATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"would you like me to\s+(apply|fix|create|update|delete|patch|run|proceed)", re.I),
    re.compile(r"shall i\s+(apply|proceed|continue|fix|run|patch)", re.I),
    re.compile(r"should i\s+(apply|fix|run|create|patch|proceed)", re.I),
    re.compile(r"do you want me to\s+(apply|fix|patch|proceed|run)", re.I),
    re.compile(r"let me know (if|whether) you('?d| would) like", re.I),
    re.compile(r"(if you('?d| would) like|if you want), i can\s+(apply|patch|fix|update)", re.I),
)


def agent_self_gated(final_text: str, tool_calls: list[str]) -> bool:
    """True iff the agent asked for confirmation AND did not already write.

    Args:
        final_text: The agent's last user-facing response.
        tool_calls: Tool names called during the turn, in order.
    """
    if not final_text:
        return False

    tail = final_text[-400:]
    pattern_match = any(p.search(tail) for p in _GATE_PATTERNS)

    # If a write-capable tool fired, only treat it as self-gated when a gate
    # pattern *also* matches (agent acted, then asked a follow-up question).
    if any(t in _WRITE_TOOLS for t in tool_calls):
        return pattern_match

    if pattern_match:
        return True

    # Fallback: last sentence is a short question. Real confirmation prompts
    # are abrupt ("Apply now?", "Proceed?"); long sentences ending in "?" are
    # almost always rhetorical.
    last = re.split(r"(?<=[.!?])\s+", tail.strip())[-1] if tail.strip() else ""
    return last.endswith("?") and len(last) < 120
