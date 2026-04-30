"""Weighted aggregate scorer."""
from __future__ import annotations

from ..models import SignalScores, TraceIssue

# Weights without cluster verification (read-only or no verify.sh) — must sum to 1.0
WEIGHTS: dict[str, float] = {
    "completion": 0.35,
    "tool_correctness": 0.25,
    "error_free": 0.20,
    "hitl_discipline": 0.10,
    "latency": 0.10,
}

# Weights when cluster_resolved is present — cluster outcome dominates
WEIGHTS_WITH_VERIFY: dict[str, float] = {
    "cluster_resolved": 0.40,
    "completion": 0.20,
    "tool_correctness": 0.15,
    "error_free": 0.12,
    "hitl_discipline": 0.08,
    "latency": 0.05,
}


def aggregate(
    scores: SignalScores,
    issues: list[TraceIssue] | None = None,
) -> float:
    if scores.cluster_resolved is not None:
        w = WEIGHTS_WITH_VERIFY
        base = (
            scores.cluster_resolved * w["cluster_resolved"]
            + scores.completion * w["completion"]
            + scores.tool_correctness * w["tool_correctness"]
            + scores.error_free * w["error_free"]
            + scores.hitl_discipline * w["hitl_discipline"]
            + scores.latency * w["latency"]
        )
    else:
        w = WEIGHTS
        base = (
            scores.completion * w["completion"]
            + scores.tool_correctness * w["tool_correctness"]
            + scores.error_free * w["error_free"]
            + scores.hitl_discipline * w["hitl_discipline"]
            + scores.latency * w["latency"]
        )

    # Soft penalty for TOKEN_EXPLOSION: ~0.0025 nudge so a wasteful-but-correct
    # run sorts below a clean one without flipping any verdict.
    if issues and any(i.id == "TOKEN_EXPLOSION" for i in issues):
        base = max(0.0, base - 0.05 * w["latency"])

    return round(base, 4)
