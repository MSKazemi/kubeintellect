"""
Automated signal scorer.

Scores each evaluation query across five observable dimensions without requiring
gold answers. All scores are 0.0–1.0 (higher = better).
"""
from __future__ import annotations

from ..models import EvalResult, LangfuseTrace, LokiLogs, SignalScores

# Tools that imply write operations and legitimately trigger HITL
_WRITE_TOOLS = {"run_kubectl"}
_WRITE_VERBS = {"apply", "delete", "patch", "scale", "create", "run", "drain", "taint", "exec"}

# Latency baseline in ms — queries slower than this get a degraded score
_DEFAULT_LATENCY_BASELINE_MS = 30_000.0

# Category-specific baselines (optional, matched by query['category'])
_CATEGORY_BASELINES: dict[str, float] = {
    "cluster_overview": 20_000.0,
    "pod_health": 25_000.0,
    "rca": 60_000.0,
    "resources": 20_000.0,
    "events": 15_000.0,
    "services": 20_000.0,
    "logs": 25_000.0,
}


def score_signals(
    query: dict,
    result: EvalResult,
    trace: LangfuseTrace | None,
    logs: LokiLogs | None,
    cluster_resolved: bool | None = None,
) -> SignalScores:
    notes: list[str] = []

    # ── 1. Completion ─────────────────────────────────────────────────────────
    # 1.0 if we received a non-empty final answer without an error event
    completion = 1.0 if (result.final_text and not result.had_error) else 0.0
    if result.had_error:
        notes.append(f"Error: {result.error_message or 'stream error'}")
    elif not result.final_text:
        notes.append("No final text received — stream may have terminated early")

    # ── 2. Tool correctness ───────────────────────────────────────────────────
    expected_tools: list[str] = query.get("expected_tools", [])
    if expected_tools:
        called = set(result.tool_calls)
        expected = set(expected_tools)
        overlap = len(called & expected)
        tool_correctness = overlap / len(expected)
        missing = expected - called
        extra = called - expected
        if missing:
            notes.append(f"Missing expected tools: {sorted(missing)}")
        if extra:
            notes.append(f"Extra tools called (unexpected): {sorted(extra)}")
    else:
        tool_correctness = 1.0  # no expectation defined, can't penalise

    # ── 3. Error-free ────────────────────────────────────────────────────────
    error_signals = 0
    if result.had_error:
        error_signals += 3
    if result.tool_errors:
        error_signals += len(result.tool_errors)
        notes.append(f"Tool errors detected: {result.tool_errors}")
    if trace and trace.has_error:
        error_signals += 1
        notes.append(f"Langfuse errors: {trace.error_messages[:3]}")
    if logs and logs.error_count:
        error_signals += logs.error_count
        notes.append(f"Loki ERROR log lines: {logs.error_count}")
    error_free = max(0.0, 1.0 - error_signals * 0.15)

    # ── 4. HITL discipline ───────────────────────────────────────────────────
    # Detect whether HITL was triggered for a query that shouldn't need it.
    # A write operation is expected only when the messages mention action verbs
    # or expected_tools includes write-capable tools.
    query_text = " ".join(
        m.get("content", "") for m in query.get("messages", []) if m.get("role") == "user"
    ).lower()
    expects_writes = (
        any(tool in expected_tools for tool in _WRITE_TOOLS)
        or any(verb in query_text for verb in {"delete", "scale", "apply", "restart", "drain"})
        or query.get("expects_write", False)
    )

    if result.had_hitl and not expects_writes:
        hitl_discipline = 0.5
        notes.append(f"HITL triggered unexpectedly on likely read-only query: {result.hitl_commands}")
    else:
        hitl_discipline = 1.0
        if result.had_hitl:
            notes.append(f"HITL triggered (expected for write op): {result.hitl_commands}")

    # ── 5. Latency ────────────────────────────────────────────────────────────
    baseline = _CATEGORY_BASELINES.get(query.get("category", ""), _DEFAULT_LATENCY_BASELINE_MS)
    latency = max(0.0, 1.0 - (result.latency_ms / baseline))
    if result.latency_ms > baseline:
        notes.append(f"Slow response: {result.latency_ms:.0f}ms > baseline {baseline:.0f}ms")

    # ── 6. Cluster resolved ───────────────────────────────────────────────────
    cluster_resolved_score: float | None = None
    if cluster_resolved is True:
        cluster_resolved_score = 1.0
    elif cluster_resolved is False:
        cluster_resolved_score = 0.0
        notes.append("Cluster issue NOT resolved after agent response")

    return SignalScores(
        completion=completion,
        tool_correctness=tool_correctness,
        error_free=error_free,
        hitl_discipline=hitl_discipline,
        latency=latency,
        cluster_resolved=cluster_resolved_score,
        notes=notes,
    )
