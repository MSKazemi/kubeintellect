"""
KubeIntellect multi-version comparison report generator.

Usage:
    uv run python -m evaluation.compare runs/v2_<ts> runs/v3_<ts> [--output report.md] [--figures figs/]
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

try:
    from scipy import stats as scipy_stats
    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False

try:
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import numpy as np
    _PLOT_AVAILABLE = True
except ImportError:
    _PLOT_AVAILABLE = False

CATEGORIES = ["debugging", "observability", "deployment", "optimization", "maintenance", "security"]


@dataclass
class RunRecord:
    scenario_id: str
    version: str
    category: str
    cluster_resolved: bool | None
    aggregate_score: float
    judge_total: float | None
    latency_ms: float
    total_tokens: int
    tool_call_count: int


@dataclass
class RunSummary:
    version: str
    run_dir: Path
    records: list[RunRecord]


def load_run(run_dir: Path) -> RunSummary:
    """Load all EvalRecord JSONs from a run directory into a RunSummary."""
    run_dir = Path(run_dir)

    meta_path = run_dir / "metadata.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        version = meta.get("target", run_dir.name.split("_")[0])
    else:
        version = run_dir.name.split("_")[0]

    scores_path = run_dir / "scores.json"
    judge_scores: dict[str, float] = {}
    if scores_path.exists():
        raw_scores = json.loads(scores_path.read_text(encoding="utf-8"))
        judge_scores = {sid: float(s.get("total", 0)) for sid, s in raw_scores.items()}

    records: list[RunRecord] = []
    for f in sorted(run_dir.glob("*.json")):
        if f.name in ("metadata.json", "scores.json"):
            continue
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        scenario_id = raw.get("query_id", f.stem)
        category = raw.get("category", "")
        rec_version = raw.get("version", version)
        cluster_resolved = raw.get("cluster_resolved")
        aggregate_score = float(raw.get("aggregate_score", 0.0))

        result = raw.get("result") or {}
        latency_ms = float(result.get("latency_ms", 0.0))
        tool_call_count = len(result.get("tool_calls") or [])

        trace = raw.get("trace") or {}
        total_tokens = int(trace.get("total_tokens", 0))

        judge_total = judge_scores.get(scenario_id)

        records.append(RunRecord(
            scenario_id=scenario_id,
            version=rec_version,
            category=category,
            cluster_resolved=cluster_resolved,
            aggregate_score=aggregate_score,
            judge_total=judge_total,
            latency_ms=latency_ms,
            total_tokens=total_tokens,
            tool_call_count=tool_call_count,
        ))

    return RunSummary(version=version, run_dir=run_dir, records=records)


# ── Table computation ─────────────────────────────────────────────────────────

def compute_rqa_table(runs: list[RunSummary]) -> list[dict]:
    """RQ-A: Cluster remediation rate per version."""
    rows = []
    baseline_rate: float | None = None
    for run in runs:
        verifiable = [r for r in run.records if r.cluster_resolved is not None]
        resolved = sum(1 for r in verifiable if r.cluster_resolved)
        total = len(verifiable)
        rate = resolved / total if total > 0 else 0.0
        row = {
            "version": run.version,
            "resolved": resolved,
            "total_verifiable": total,
            "resolution_rate_pct": round(rate * 100, 1),
        }
        if baseline_rate is None:
            baseline_rate = rate
        delta = round(rate * 100 - baseline_rate * 100, 1)
        row["delta_vs_baseline_pp"] = f"{'+' if delta >= 0 else ''}{delta}"
        rows.append(row)
    return rows


def compute_rqb_table(runs: list[RunSummary]) -> list[dict]:
    """RQ-B: Quality vs efficiency scorecard per version."""
    rows = []
    for run in runs:
        recs = run.records
        if not recs:
            continue
        judged = [r for r in recs if r.judge_total is not None]
        avg_judge = sum(r.judge_total for r in judged) / len(judged) if judged else None
        judge_pass_rate = (
            sum(1 for r in judged if r.judge_total > 28) / len(judged)
            if judged else None
        )
        avg_latency_s = sum(r.latency_ms for r in recs) / len(recs) / 1000
        avg_tokens = sum(r.total_tokens for r in recs) / len(recs)
        avg_tools = sum(r.tool_call_count for r in recs) / len(recs)
        avg_signal = sum(r.aggregate_score for r in recs) / len(recs)
        rows.append({
            "version": run.version,
            "avg_judge_score": round(avg_judge, 1) if avg_judge is not None else "N/A",
            "judge_pass_rate_pct": round(judge_pass_rate * 100, 1) if judge_pass_rate is not None else "N/A",
            "avg_latency_s": round(avg_latency_s, 1),
            "avg_tokens": round(avg_tokens),
            "avg_tool_calls": round(avg_tools, 1),
            "avg_aggregate_score": round(avg_signal, 3),
        })
    return rows


def compute_rqc_table(runs: list[RunSummary]) -> list[dict]:
    """RQ-C: Score by category per version, with winner."""
    rows = []
    for cat in CATEGORIES:
        row: dict = {"category": cat}
        scores_by_version: dict[str, float] = {}
        for run in runs:
            cat_recs = [r for r in run.records if r.category == cat]
            if cat_recs:
                avg = sum(r.aggregate_score for r in cat_recs) / len(cat_recs)
                row[run.version] = round(avg, 3)
                scores_by_version[run.version] = avg
        row["winner"] = max(scores_by_version, key=scores_by_version.get) if scores_by_version else "—"
        rows.append(row)
    return rows


def compute_issue_table(runs: list[RunSummary]) -> list[dict]:
    """Table 4: Trace issue severity counts per version."""
    rows = []
    for run in runs:
        high = med = low = 0
        for f in sorted(run.run_dir.glob("*.json")):
            if f.name in ("metadata.json", "scores.json"):
                continue
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for issue in raw.get("trace_issues") or []:
                sev = issue.get("severity", "")
                if sev == "HIGH":
                    high += 1
                elif sev == "MEDIUM":
                    med += 1
                elif sev == "LOW":
                    low += 1
        rows.append({"version": run.version, "HIGH": high, "MEDIUM": med, "LOW": low})
    return rows


# ── Statistical tests ─────────────────────────────────────────────────────────

def chi_squared_rqa(runs: list[RunSummary]) -> str:
    """Chi-squared test on resolution counts across versions."""
    if not _SCIPY_AVAILABLE or len(runs) < 2:
        return "N/A (scipy not available or <2 runs)"
    contingency = []
    for run in runs:
        verifiable = [r for r in run.records if r.cluster_resolved is not None]
        resolved = sum(1 for r in verifiable if r.cluster_resolved)
        not_resolved = len(verifiable) - resolved
        if len(verifiable) > 0:
            contingency.append([resolved, not_resolved])
    if len(contingency) < 2:
        return "N/A (insufficient data)"
    try:
        chi2, p, dof, _ = scipy_stats.chi2_contingency(contingency)
        sig = "significant" if p < 0.05 else "not significant"
        return f"χ²={chi2:.3f}, dof={dof}, p={p:.4f} ({sig} at α=0.05)"
    except Exception as e:
        return f"Error: {e}"


def wilcoxon_rqb(runs: list[RunSummary]) -> str:
    """Paired Wilcoxon test on judge scores between first two versions."""
    if not _SCIPY_AVAILABLE or len(runs) < 2:
        return "N/A (scipy not available or <2 runs)"
    r1, r2 = runs[0], runs[1]
    scores1 = {r.scenario_id: r.judge_total for r in r1.records if r.judge_total is not None}
    scores2 = {r.scenario_id: r.judge_total for r in r2.records if r.judge_total is not None}
    shared = sorted(scores1.keys() & scores2.keys())
    if len(shared) < 5:
        return f"N/A (only {len(shared)} shared judged scenarios, need ≥5)"
    x = [scores1[sid] for sid in shared]
    y = [scores2[sid] for sid in shared]
    try:
        stat, p = scipy_stats.wilcoxon(x, y)
        sig = "significant" if p < 0.05 else "not significant"
        return (f"Wilcoxon W={stat:.1f}, p={p:.4f} ({sig} at α=0.05) "
                f"[{r1.version} vs {r2.version}, n={len(shared)}]")
    except Exception as e:
        return f"Error: {e}"


def mannwhitney_rqc(runs: list[RunSummary]) -> dict[str, str]:
    """Mann-Whitney U test per category between first two versions."""
    if not _SCIPY_AVAILABLE or len(runs) < 2:
        return {cat: "N/A (scipy not available)" for cat in CATEGORIES}
    r1, r2 = runs[0], runs[1]
    results: dict[str, str] = {}
    for cat in CATEGORIES:
        x = [r.aggregate_score for r in r1.records if r.category == cat]
        y = [r.aggregate_score for r in r2.records if r.category == cat]
        if len(x) < 3 or len(y) < 3:
            results[cat] = f"N/A (n={len(x)},{len(y)})"
            continue
        try:
            stat, p = scipy_stats.mannwhitneyu(x, y, alternative="two-sided")
            sig = "sig" if p < 0.05 else "ns"
            results[cat] = f"U={stat:.0f}, p={p:.4f} ({sig}) [n={len(x)},{len(y)}]"
        except Exception as e:
            results[cat] = f"Error: {e}"
    return results


# ── Figures ───────────────────────────────────────────────────────────────────

def _category_scores(run: RunSummary) -> list[float]:
    scores = []
    for cat in CATEGORIES:
        recs = [r for r in run.records if r.category == cat]
        scores.append(sum(r.aggregate_score for r in recs) / len(recs) if recs else 0.0)
    return scores


def plot_radar(runs: list[RunSummary], out_dir: Path) -> None:
    if not _PLOT_AVAILABLE:
        print("  ⚠ matplotlib not available — skipping radar chart")
        return
    N = len(CATEGORIES)
    angles = [n / float(N) * 2 * math.pi for n in range(N)]
    angles += angles[:1]
    fig, ax = plt.subplots(figsize=(7, 7), subplot_kw={"projection": "polar"})
    colors = ["#2196F3", "#FF5722", "#4CAF50", "#9C27B0"]
    for i, run in enumerate(runs):
        values = _category_scores(run) + [_category_scores(run)[0]]
        ax.plot(angles, values, "o-", linewidth=2, label=run.version,
                color=colors[i % len(colors)])
        ax.fill(angles, values, alpha=0.15, color=colors[i % len(colors)])
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels([c.capitalize() for c in CATEGORIES], size=11)
    ax.set_ylim(0, 1)
    ax.set_yticks([0.2, 0.4, 0.6, 0.8, 1.0])
    ax.legend(loc="upper right", bbox_to_anchor=(1.3, 1.1))
    ax.set_title("KubeIntellect — Capability Profile by Version", pad=20)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_dir / "radar.png", bbox_inches="tight", dpi=150)
    fig.savefig(out_dir / "radar.pdf", bbox_inches="tight")
    plt.close(fig)
    print(f"  → {out_dir}/radar.png")


def plot_scatter(runs: list[RunSummary], out_dir: Path) -> None:
    if not _PLOT_AVAILABLE:
        print("  ⚠ matplotlib not available — skipping scatter plot")
        return
    cat_colors = {
        "debugging": "#E53935", "observability": "#1E88E5",
        "deployment": "#43A047", "optimization": "#FB8C00",
        "maintenance": "#8E24AA", "security": "#00897B",
    }
    markers = ["o", "s", "^", "D"]
    fig, ax = plt.subplots(figsize=(10, 7))
    for i, run in enumerate(runs):
        judged = [r for r in run.records if r.judge_total is not None]
        if not judged:
            continue
        x = [r.latency_ms / 1000 for r in judged]
        y = [r.judge_total for r in judged]
        c = [cat_colors.get(r.category, "#999") for r in judged]
        ax.scatter(x, y, c=c, marker=markers[i % len(markers)],
                   label=run.version, alpha=0.7, s=80, edgecolors="white", linewidths=0.5)
    ax.axhline(y=28, color="gray", linestyle="--", alpha=0.5, label="Pass threshold (28/40)")
    ax.set_xlabel("Response Latency (s)", fontsize=12)
    ax.set_ylabel("LLM Judge Score (/40)", fontsize=12)
    ax.set_title("Quality vs Latency — All Scenarios by Version", fontsize=13)
    version_legend = ax.legend(loc="upper right")
    patches = [mpatches.Patch(color=v, label=k.capitalize()) for k, v in cat_colors.items()]
    ax.legend(handles=patches, loc="lower right", title="Category", fontsize=9)
    ax.add_artist(version_legend)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_dir / "scatter.png", bbox_inches="tight", dpi=150)
    fig.savefig(out_dir / "scatter.pdf", bbox_inches="tight")
    plt.close(fig)
    print(f"  → {out_dir}/scatter.png")


def plot_resolution_bar(runs: list[RunSummary], out_dir: Path) -> None:
    if not _PLOT_AVAILABLE:
        print("  ⚠ matplotlib not available — skipping bar chart")
        return
    verifiable_cats = ["debugging", "deployment", "maintenance"]
    n_cats = len(verifiable_cats)
    n_runs = len(runs)
    width = 0.8 / n_runs
    fig, ax = plt.subplots(figsize=(9, 5))
    colors = ["#2196F3", "#FF5722", "#4CAF50"]
    for i, run in enumerate(runs):
        rates = []
        for cat in verifiable_cats:
            v = [r for r in run.records if r.category == cat and r.cluster_resolved is not None]
            rates.append(sum(1 for r in v if r.cluster_resolved) / len(v) * 100 if v else 0)
        x = [j + i * width for j in range(n_cats)]
        ax.bar(x, rates, width, label=run.version, color=colors[i % len(colors)], alpha=0.85)
    ax.set_xticks([j + (n_runs - 1) * width / 2 for j in range(n_cats)])
    ax.set_xticklabels([c.capitalize() for c in verifiable_cats], fontsize=12)
    ax.set_ylabel("Cluster Resolution Rate (%)", fontsize=12)
    ax.set_ylim(0, 110)
    ax.legend()
    ax.set_title("Cluster Resolution Rate by Category and Version", fontsize=13)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_dir / "resolution_bar.png", bbox_inches="tight", dpi=150)
    fig.savefig(out_dir / "resolution_bar.pdf", bbox_inches="tight")
    plt.close(fig)
    print(f"  → {out_dir}/resolution_bar.png")


# ── Report generation ─────────────────────────────────────────────────────────

def _md_table(headers: list[str], rows: list[dict]) -> str:
    if not rows:
        return "_No data_\n"
    lines = [
        "| " + " | ".join(str(h) for h in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(h, "—")) for h in headers) + " |")
    return "\n".join(lines) + "\n"


def generate_comparison_report(
    runs: list[RunSummary],
    output_path: Path,
    figures_dir: Path | None = None,
) -> None:
    import datetime
    rqa    = compute_rqa_table(runs)
    rqb    = compute_rqb_table(runs)
    rqc    = compute_rqc_table(runs)
    issues = compute_issue_table(runs)
    chi2   = chi_squared_rqa(runs)
    wilc   = wilcoxon_rqb(runs)
    mw     = mannwhitney_rqc(runs)

    versions_str = " vs ".join(r.version for r in runs)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    version_cols = [r.version for r in runs]

    lines = [
        f"# KubeIntellect Multi-Version Comparison: {versions_str}",
        f"\nGenerated: {now}\n",
        "---\n",
        "## RQ-A: Cluster Remediation Rate\n",
        _md_table(
            ["version", "resolved", "total_verifiable", "resolution_rate_pct", "delta_vs_baseline_pp"],
            rqa,
        ),
        f"**Statistical test (chi-squared):** {chi2}\n",
        "---\n",
        "## RQ-B: Quality vs Efficiency Scorecard\n",
        _md_table(
            ["version", "avg_judge_score", "judge_pass_rate_pct", "avg_latency_s",
             "avg_tokens", "avg_tool_calls", "avg_aggregate_score"],
            rqb,
        ),
        f"**Statistical test (paired Wilcoxon on judge scores):** {wilc}\n",
        "---\n",
        "## RQ-C: Score by Scenario Category\n",
        _md_table(["category"] + version_cols + ["winner"], rqc),
        "\n**Mann-Whitney U tests per category:**\n",
    ]
    for cat, result in mw.items():
        lines.append(f"- **{cat.capitalize()}:** {result}")
    lines += [
        "\n---\n",
        "## Table 4: Trace Issue Summary\n",
        _md_table(["version", "HIGH", "MEDIUM", "LOW"], issues),
    ]
    if figures_dir:
        lines += [
            "\n---\n",
            "## Figures\n",
            f"![Capability Radar]({figures_dir}/radar.png)  ",
            f"![Quality vs Latency]({figures_dir}/scatter.png)  ",
            f"![Resolution Rate]({figures_dir}/resolution_bar.png)  ",
        ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Report: {output_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Compare KubeIntellect evaluation runs across versions"
    )
    parser.add_argument("runs", nargs="+", metavar="RUN_DIR",
                        help="Two or more run directories (e.g. runs/v2_<ts> runs/v3_<ts>)")
    parser.add_argument("--output", default="paper/comparison_report.md",
                        help="Output markdown report path (default: paper/comparison_report.md)")
    parser.add_argument("--figures", default=None,
                        help="Directory for figure outputs (PNG + PDF)")
    args = parser.parse_args()

    if len(args.runs) < 2:
        parser.error("Provide at least 2 run directories to compare")

    print(f"Loading {len(args.runs)} run(s)...")
    loaded = [load_run(Path(d)) for d in args.runs]
    for run in loaded:
        print(f"  {run.version}: {len(run.records)} scenarios")

    figures_dir = Path(args.figures) if args.figures else None
    if figures_dir:
        print("Generating figures...")
        plot_radar(loaded, figures_dir)
        plot_scatter(loaded, figures_dir)
        plot_resolution_bar(loaded, figures_dir)

    print("Generating report...")
    generate_comparison_report(loaded, Path(args.output), figures_dir)


if __name__ == "__main__":
    _cli()
