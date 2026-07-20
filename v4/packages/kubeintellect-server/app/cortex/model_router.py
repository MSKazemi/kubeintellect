"""Heterogeneous model routing + air-gap floor (v5 P4, ADR-103).

Route each request to the cheapest capable tier: a small/local model for triage and tool-call
formatting, the frontier model for RCA synthesis. When the frontier is unreachable (air-gapped /
disconnected operation — C8), degrade gracefully to a **read-only triage floor** on the small
model rather than failing: the edge/production persona (CH-15) keeps a working diagnostic path.

Unsupervised edge *writes* while disconnected are an explicit NON-GOAL (roadmap §9) — only P5's
signed action capsules handle disconnected writes. So the air-gap floor is read-only by contract.

Pure functions — no LLM, no network — fully unit-testable.
"""

from __future__ import annotations

SMALL = "small"        # local/small model: triage, tool-call formatting, disconnected floor
FRONTIER = "frontier"  # large model: RCA synthesis, final answer

# Task kinds and their preferred tier.
TRIAGE = "triage"
TOOL_FORMAT = "tool_format"
RCA_SYNTHESIS = "rca_synthesis"

_PREFERRED = {
    TRIAGE: SMALL,
    TOOL_FORMAT: SMALL,
    RCA_SYNTHESIS: FRONTIER,
}


def route_tier(task_kind: str, *, connected: bool = True, frontier_reachable: bool | None = None) -> str:
    """Pick the model tier for a task. Falls back to the small tier when the frontier a task wants
    is unreachable (air-gap degradation) — never fails to route."""
    if frontier_reachable is None:
        frontier_reachable = connected
    preferred = _PREFERRED.get(task_kind, FRONTIER)
    if preferred == FRONTIER and not frontier_reachable:
        return SMALL          # degrade to the small-model floor rather than fail
    return preferred


def edge_write_allowed(connected: bool) -> bool:
    """Disconnected ⇒ no unsupervised writes (air-gap read-only floor; ADR-103 / roadmap §9)."""
    return connected


def degraded(task_kind: str, *, connected: bool = True, frontier_reachable: bool | None = None) -> bool:
    """True iff this task is running below its preferred tier (surfaced so the answer can disclose it)."""
    if frontier_reachable is None:
        frontier_reachable = connected
    return _PREFERRED.get(task_kind, FRONTIER) == FRONTIER and not frontier_reachable
