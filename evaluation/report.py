"""
Markdown report generator for an evaluation run.

Produces a single report_<run_id>.md in the run directory.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .lessons import analyze
from .models import EvalRecord

# Curated set of scenarios that don't depend on cluster timing — read-only or
# trivially deterministic. Used for a "stable subset" pass-rate metric in the
# report. The set lives here (not in the scenarios) because stability is a
# property of how the metric is reported, not of the scenario itself.
STABLE_SCENARIO_IDS: frozenset[str] = frozenset({
    "01-crashloop", "03-service-mismatch", "07-rbac-denied",
    "26-cluster-overview", "27-cluster-health", "29-pod-restarts",
    "30-unhealthy-pods", "33-resource-quotas", "36-services-no-endpoints",
    "38-app-log-errors", "39-prometheus-error-rate",
})


def _serialise(obj) -> object:
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _serialise(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_serialise(i) for i in obj]
    return obj


def save_record_json(record: EvalRecord, path: Path) -> None:
    path.write_text(json.dumps(_serialise(record), indent=2, default=str))


def generate_report(records: list[EvalRecord], run_id: str, run_dir: Path) -> Path:
    lines = [f"# KubeIntellect Evaluation — {run_id}\n"]

    # ── Score table ───────────────────────────────────────────────────────────
    lines.append("## Results\n")
    lines.append(
        "| ID | Title | Score | Resolved | Asked? | ✓ | Tools | Errors | HITL | Latency (ms) | Verdict |"
    )
    lines.append(
        "|----|-------|-------|----------|--------|---|-------|--------|------|-------------|---------|"
    )
    for r in records:
        verdict = r.review_verdict or "_(pending)_"
        hitl_cell = (
            "⚠️ unexpected" if (r.result.had_hitl and r.scores.hitl_discipline < 1.0)
            else ("🔒 expected" if r.result.had_hitl else "—")
        )
        resolved_cell = (
            "✅" if r.cluster_resolved is True
            else ("❌" if r.cluster_resolved is False else "—")
        )
        asked_cell = "🙋" if r.agent_asked_first else "—"
        lines.append(
            f"| {r.query_id} | {r.title} | {r.aggregate_score:.2f} | "
            f"{resolved_cell} | {asked_cell} | "
            f"{'✅' if r.scores.completion == 1.0 else '❌'} | "
            f"{r.scores.tool_correctness:.2f} | "
            f"{'❌' if r.result.had_error else '✅'} | "
            f"{hitl_cell} | "
            f"{r.result.latency_ms:.0f} | "
            f"{verdict} |"
        )
    lines.append("")

    # ── Aggregate stats ───────────────────────────────────────────────────────
    if records:
        avg_score = sum(r.aggregate_score for r in records) / len(records)
        avg_latency = sum(r.result.latency_ms for r in records) / len(records)
        completion_rate = sum(1 for r in records if r.scores.completion == 1.0) / len(records)

        lines.append("## Aggregate\n")
        lines.append(f"- **Average score:** {avg_score:.2f}")
        lines.append(f"- **Completion rate:** {completion_rate:.0%}")
        lines.append(f"- **Average latency:** {avg_latency:.0f}ms")
        lines.append(f"- **Queries with HITL:** {sum(1 for r in records if r.result.had_hitl)}")
        lines.append(f"- **Queries with errors:** {sum(1 for r in records if r.result.had_error)}")
        verified = [r for r in records if r.cluster_resolved is not None]
        if verified:
            resolved_count = sum(1 for r in verified if r.cluster_resolved)
            lines.append(f"- **Cluster resolved:** {resolved_count}/{len(verified)} scenarios with verify.sh")
        lines.append(f"- **Langfuse available:** {sum(1 for r in records if r.trace and r.trace.available)}/{len(records)}")
        lines.append(f"- **Loki available:** {sum(1 for r in records if r.logs and r.logs.available)}/{len(records)}")

        # Stable subset — pass-rate over scenarios that don't depend on cluster timing.
        # Useful as a low-variance trend metric alongside the noisy whole-run average.
        stable = [r for r in records if r.query_id in STABLE_SCENARIO_IDS]
        if stable:
            stable_pass = sum(1 for r in stable if r.aggregate_score >= 0.9)
            lines.append(
                f"- **Stable subset (≥0.90):** {stable_pass}/{len(stable)} "
                f"({stable_pass/len(stable):.0%})"
            )
        lines.append("")

    # ── Multi-turn outcomes ──────────────────────────────────────────────────
    asked = [r for r in records if r.agent_asked_first]
    if asked:
        fixed = [r for r in asked if r.cluster_resolved is True]
        lines.append("## Multi-Turn (agent asked, harness confirmed)\n")
        lines.append(f"- Agent self-gated: {len(asked)}")
        lines.append(f"- Fixed after confirmation: {len(fixed)}/{len(asked)}")
        for r in asked:
            status = (
                "fixed" if r.cluster_resolved is True
                else ("still broken" if r.cluster_resolved is False else "n/a")
            )
            lines.append(
                f"  - **{r.query_id}** — sent `{r.follow_up_sent}` ({status})"
            )
        lines.append("")

    # ── Lessons learned ───────────────────────────────────────────────────────
    lines.append(analyze(records))

    # ── Per-query detail ──────────────────────────────────────────────────────
    lines.append("## Per-Query Detail\n")
    for r in records:
        lines.append(f"### {r.query_id}: {r.title}\n")
        lines.append(f"- **Session:** `{r.session_id}`")
        lines.append(f"- **Latency:** {r.result.latency_ms:.0f}ms")
        lines.append(f"- **Tools called:** {r.result.tool_calls or '(none)'}")
        lines.append(f"- **Status phases:** {r.result.status_phases or '(none)'}")

        if r.result.had_hitl:
            lines.append(f"- **HITL commands:** {r.result.hitl_commands}")

        if r.trace and r.trace.available:
            total_llm_ms = sum(g.latency_ms for g in r.trace.generations)
            lines.append(f"- **Tokens:** {r.trace.total_tokens} | LLM calls: {len(r.trace.generations)} | LLM time: {total_llm_ms:.0f}ms")

        if r.logs and r.logs.available:
            lines.append(f"- **Logs:** {len(r.logs.lines)} lines | errors: {r.logs.error_count} | warnings: {r.logs.warning_count}")
            if r.logs.kubectl_commands:
                lines.append(f"- **kubectl commands in logs:**")
                for cmd in r.logs.kubectl_commands[:5]:
                    lines.append(f"  - `{cmd}`")

        if r.scores.notes:
            lines.append("- **Score notes:**")
            for note in r.scores.notes:
                lines.append(f"  - {note}")

        if r.cluster_resolved is not None:
            lines.append(f"- **Cluster resolved:** {'✅ yes' if r.cluster_resolved else '❌ no'}")
        lines.append(f"- **Verdict:** {r.review_verdict or '_(pending manual review)_'}")

        if r.result.final_text:
            preview = r.result.final_text[:500]
            if len(r.result.final_text) > 500:
                preview += " ..."
            lines.append(f"\n<details><summary>Answer preview</summary>\n\n{preview}\n\n</details>")

        lines.append("")

    report_path = run_dir / f"report_{run_id}.md"
    report_path.write_text("\n".join(lines))
    return report_path
