"""
Lessons learned analyzer.

Reads all EvalRecords from a run and surfaces cross-query patterns:
unexpected HITL, wrong tools, errors, latency outliers. Output feeds
the improvement backlog.
"""
from __future__ import annotations

from .models import EvalRecord


def analyze(records: list[EvalRecord]) -> str:
    if not records:
        return "No records to analyze.\n"

    n = len(records)
    completed = [r for r in records if r.scores.completion == 1.0]
    errors = [r for r in records if r.result.had_error or r.scores.error_free < 0.8]
    unexpected_hitl = [r for r in records if r.result.had_hitl and r.scores.hitl_discipline < 1.0]
    wrong_tools = [r for r in records if 0 < r.scores.tool_correctness < 0.8]
    slow = [r for r in records if r.scores.latency < 0.5]
    avg_score = sum(r.aggregate_score for r in records) / n
    avg_latency = sum(r.result.latency_ms for r in records) / n

    verified = [r for r in records if r.cluster_resolved is not None]
    resolved = [r for r in verified if r.cluster_resolved is True]
    unresolved = [r for r in verified if r.cluster_resolved is False]

    lines = ["# Lessons Learned\n"]

    # ── Summary ──────────────────────────────────────────────────────────────
    lines.append("## Run Summary\n")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total queries | {n} |")
    lines.append(f"| Completed | {len(completed)} ({100*len(completed)//n}%) |")
    lines.append(f"| Average score | {avg_score:.2f} |")
    lines.append(f"| Average latency | {avg_latency:.0f}ms |")
    if verified:
        lines.append(f"| **Cluster resolved** | **{len(resolved)}/{len(verified)} ({100*len(resolved)//len(verified)}%)** |")
    lines.append(f"| Error queries | {len(errors)} |")
    lines.append(f"| Unexpected HITL | {len(unexpected_hitl)} |")
    lines.append(f"| Wrong tools | {len(wrong_tools)} |")
    lines.append(f"| Slow (latency<0.5) | {len(slow)} |")
    lines.append("")

    # ── Unresolved cluster issues ─────────────────────────────────────────────
    if unresolved:
        lines.append("## Unresolved Cluster Issues\n")
        lines.append("These scenarios ran but verify.sh reported the cluster issue was NOT fixed. "
                     "Agent either only suggested a fix or applied one that didn't work.\n")
        for r in unresolved:
            lines.append(f"### {r.query_id}: {r.title}")
            lines.append(f"- Score: {r.aggregate_score:.2f}")
            lines.append(f"- Tools called: {r.result.tool_calls or '(none)'}")
            if r.result.final_text:
                preview = r.result.final_text[:300].replace("\n", " ")
                lines.append(f"- Response preview: {preview}…")
            for note in r.scores.notes:
                lines.append(f"- Note: {note}")
            lines.append("")

    # ── Unexpected HITL ───────────────────────────────────────────────────────
    if unexpected_hitl:
        lines.append("## Unexpected HITL Triggers\n")
        lines.append("These read-only queries triggered HITL approval. "
                     "Check if the agent is using unnecessarily risky kubectl verbs.\n")
        for r in unexpected_hitl:
            lines.append(f"### {r.query_id}: {r.title}")
            for cmd in r.result.hitl_commands:
                lines.append(f"- Command: `{cmd}`")
            lines.append("")

    # ── Wrong tools ────────────────────────────────────────────────────────────
    if wrong_tools:
        lines.append("## Tool Usage Mismatches\n")
        lines.append("Queries where the agent called different tools than expected.\n")
        for r in wrong_tools:
            lines.append(f"### {r.query_id}: {r.title}")
            lines.append(f"- Tool correctness score: {r.scores.tool_correctness:.2f}")
            lines.append(f"- Called: {r.result.tool_calls}")
            for note in r.scores.notes:
                if "tool" in note.lower():
                    lines.append(f"- {note}")
            lines.append("")

    # ── Errors ────────────────────────────────────────────────────────────────
    if errors:
        lines.append("## Error Patterns\n")
        for r in errors:
            if not (r.result.had_error or r.result.tool_errors
                    or (r.trace and r.trace.has_error)
                    or (r.logs and r.logs.error_count)):
                continue
            lines.append(f"### {r.query_id}: {r.title}")
            if r.result.had_error:
                lines.append(f"- API error: {r.result.error_message}")
            if r.result.tool_errors:
                lines.append(f"- Tool errors: {r.result.tool_errors}")
            if r.trace and r.trace.has_error:
                for msg in r.trace.error_messages[:3]:
                    lines.append(f"- Langfuse: {msg}")
            if r.logs and r.logs.error_count:
                lines.append(f"- Loki ERROR lines: {r.logs.error_count}")
                err_lines = [l.message for l in r.logs.lines if l.level == "ERROR"][:3]
                for el in err_lines:
                    lines.append(f"  > {el[:200]}")
            lines.append("")

    # ── Slow queries ───────────────────────────────────────────────────────────
    if slow:
        lines.append("## Latency Outliers\n")
        for r in sorted(slow, key=lambda x: x.result.latency_ms, reverse=True):
            lines.append(f"### {r.query_id}: {r.title}")
            lines.append(f"- Total latency: {r.result.latency_ms:.0f}ms")
            lines.append(f"- Status phases: {r.result.status_phases}")
            if r.trace and r.trace.available and r.trace.generations:
                slowest = max(r.trace.generations, key=lambda g: g.latency_ms)
                lines.append(f"- Slowest LLM call: `{slowest.name}` — {slowest.latency_ms:.0f}ms "
                              f"({slowest.prompt_tokens}+{slowest.completion_tokens} tokens)")
            lines.append("")

    # ── All signal notes ───────────────────────────────────────────────────────
    if any(r.scores.notes for r in records):
        lines.append("## All Signal Notes\n")
        for r in records:
            if r.scores.notes:
                lines.append(f"**{r.query_id}** — {r.title}:")
                for note in r.scores.notes:
                    lines.append(f"- {note}")
                lines.append("")

    # ── Trace issues (automated diagnostics) ──────────────────────────────────
    all_issues = [(r, iss) for r in records for iss in r.trace_issues]
    if all_issues:
        SEV = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵"}
        lines.append("## Trace-Level Issues\n")
        lines.append("Auto-detected by issue_detector after each scenario. "
                     "HIGH issues require immediate investigation.\n")
        by_id: dict[str, list[tuple]] = {}
        for r, iss in all_issues:
            by_id.setdefault(iss.id, []).append((r, iss))
        for issue_id, hits in sorted(by_id.items(), key=lambda x: x[0]):
            first_iss = hits[0][1]
            icon = SEV.get(first_iss.severity, "⚪")
            lines.append(f"### {icon} {issue_id} ({first_iss.severity}) — {len(hits)} occurrence(s)\n")
            lines.append(f"{first_iss.detail}\n")
            lines.append("Affected scenarios:")
            for r, _ in hits:
                lines.append(f"- `{r.query_id}` — {r.title}")
            lines.append("")

    return "\n".join(lines)
