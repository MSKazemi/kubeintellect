"""
Automated issue detectors that work on typed EvalResult + LangfuseTrace models.

Results are stored in EvalRecord.trace_issues and surfaced in the lessons report.
analyze_trace.py uses the deeper raw-API detectors; this module covers what we can
derive from the typed models collected automatically during every run.
"""
from __future__ import annotations

from ..models import EvalResult, LangfuseTrace, LokiLogs, TraceIssue


def detect(
    result: EvalResult,
    trace: LangfuseTrace | None,
    logs: LokiLogs | None,
    *,
    agent_asked_first: bool = False,
    cluster_resolved: bool | None = None,
) -> list[TraceIssue]:
    issues: list[TraceIssue] = []
    query = " ".join(
        m.get("content", "") for m in result.messages if m.get("role") == "user"
    ).lower()

    if trace and trace.available:
        gens = trace.generations

        # Token explosion between LLM calls
        if len(gens) >= 2:
            tokens = sorted(g.prompt_tokens for g in gens)
            delta = tokens[-1] - tokens[0]
            if delta > 400:
                issues.append(TraceIssue(
                    id="TOKEN_EXPLOSION",
                    severity="HIGH",
                    detail=(
                        f"Prompt grew by {delta} tokens across LLM calls "
                        f"({tokens[0]} → {tokens[-1]}). "
                        "Tool output fed raw into next prompt without summarization."
                    ),
                ))

        # Excessive LLM round-trips
        if len(gens) > 3:
            issues.append(TraceIssue(
                id="MANY_LLM_CALLS",
                severity="MEDIUM",
                detail=f"{len(gens)} LLM calls in one query. Coordinator may be looping.",
            ))

        # Truncated kubectl output in a tool span
        for span in trace.tool_spans:
            if span.output and ("[truncated" in span.output or "chars omitted" in span.output):
                issues.append(TraceIssue(
                    id="OUTPUT_TRUNCATED",
                    severity="MEDIUM",
                    detail=f"kubectl output truncated in span '{span.name}'. LLM may be missing data.",
                ))
                break

        # Tool errors not surfaced in the final answer
        if trace.has_error and not result.had_error:
            issues.append(TraceIssue(
                id="SILENT_TOOL_ERROR",
                severity="LOW",
                detail="Tool spans show errors but final answer appears clean — errors may have been silently swallowed.",
            ))

    # High end-to-end latency
    if result.latency_ms > 30_000:
        issues.append(TraceIssue(
            id="HIGH_LATENCY",
            severity="MEDIUM",
            detail=f"{result.latency_ms:.0f}ms total latency.",
        ))

    # Log tool skipped for log-related queries
    log_keywords = ("log", "error", "crash", "exception", "stderr")
    if any(k in query for k in log_keywords) and "query_loki" not in result.tool_calls:
        issues.append(TraceIssue(
            id="LOKI_NOT_USED",
            severity="MEDIUM",
            detail="Query mentions logs but query_loki was not called. kubectl logs misses historical data.",
        ))

    # Prometheus tool skipped for metric queries
    metric_keywords = ("cpu", "memory", "metric", "usage", "trend", "latency")
    if any(k in query for k in metric_keywords) and "query_prometheus" not in result.tool_calls:
        issues.append(TraceIssue(
            id="PROMETHEUS_NOT_USED",
            severity="MEDIUM",
            detail="Query mentions metrics but query_prometheus was not called. kubectl top gives only a current snapshot.",
        ))

    # Agent self-gated despite a correct diagnosis. The harness confirmed
    # and the cluster ended up healthy — a signal to tighten the agent
    # prompt so it acts when auto_approve=true.
    if agent_asked_first and cluster_resolved is True:
        issues.append(TraceIssue(
            id="AGENT_SELF_GATED_BUT_DIAGNOSED",
            severity="LOW",
            detail=(
                "Agent diagnosed correctly but stopped to ask before acting. "
                "Harness confirmed and the fix succeeded — consider tightening "
                "the agent prompt to act when auto_approve=true."
            ),
        ))

    # Wrong tool for "events"/"warnings" intent: query asks about events but
    # the agent didn't invoke kubectl with `events`. Inspect tool_spans.
    event_keywords = ("event", "warning", "warnings")
    if any(k in query for k in event_keywords):
        used_events = False
        if trace and trace.available:
            for span in trace.tool_spans:
                args = span.input_args
                if isinstance(args, dict):
                    blob = " ".join(str(v) for v in args.values()).lower()
                elif isinstance(args, str):
                    blob = args.lower()
                else:
                    blob = str(args or "").lower()
                if "events" in blob:
                    used_events = True
                    break
        if not used_events and "run_kubectl" in result.tool_calls:
            issues.append(TraceIssue(
                id="WRONG_TOOL_FOR_INTENT",
                severity="MEDIUM",
                detail=(
                    "Query mentions events/warnings but kubectl was not invoked "
                    "with `get events`. Prometheus/Loki may have been used where "
                    "kubectl events would be authoritative."
                ),
            ))

    return issues
