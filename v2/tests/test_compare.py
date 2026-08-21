"""Tests for evaluation.compare module."""
import json
from pathlib import Path
import pytest

# Every test below defers `from evaluation.compare import ...` into the function
# body. That kept collection alive, but only moved the failure: without the
# out-of-tree `evaluation/` harness all 12 turned into runtime errors instead.
# Skip the module outright for the same reason as the other harness-dependent
# suites here. See #155.
pytest.importorskip(
    "evaluation.compare",
    reason="needs the out-of-tree `evaluation/` scoring harness",
)


def _make_run_dir(tmp_path: Path, target: str, records: list[dict]) -> Path:
    run_dir = tmp_path / f"{target}_20260501_120000"
    run_dir.mkdir()
    (run_dir / "metadata.json").write_text(
        json.dumps({"target": target, "mode": "run", "tag": target})
    )
    for rec in records:
        (run_dir / f"{rec['query_id']}.json").write_text(json.dumps(rec))
    return run_dir


def _rec(query_id, version, category, cluster_resolved=None,
         aggregate_score=0.8, latency_ms=5000.0, tokens=500, tools=3):
    return {
        "query_id": query_id,
        "version": version,
        "category": category,
        "cluster_resolved": cluster_resolved,
        "aggregate_score": aggregate_score,
        "result": {"latency_ms": latency_ms, "tool_calls": ["t"] * tools},
        "trace": {"total_tokens": tokens},
        "trace_issues": [],
    }


def test_load_run_reads_version_from_metadata(tmp_path):
    run_dir = _make_run_dir(tmp_path, "v3", [])
    from evaluation.compare import load_run
    run = load_run(run_dir)
    assert run.version == "v3"


def test_load_run_falls_back_to_dir_name_prefix(tmp_path):
    run_dir = tmp_path / "v2_20260501_120000"
    run_dir.mkdir()
    # No metadata.json
    from evaluation.compare import load_run
    run = load_run(run_dir)
    assert run.version == "v2"


def test_load_run_parses_records(tmp_path):
    recs = [
        _rec("01-crashloop", "v2", "debugging", cluster_resolved=True),
        _rec("41-deploy-nginx", "v2", "deployment", cluster_resolved=True),
    ]
    run_dir = _make_run_dir(tmp_path, "v2", recs)
    from evaluation.compare import load_run
    run = load_run(run_dir)
    assert len(run.records) == 2
    assert run.records[0].cluster_resolved is True
    assert run.records[0].category == "debugging"


def test_load_run_skips_metadata_and_scores_json(tmp_path):
    run_dir = _make_run_dir(tmp_path, "v2", [])
    (run_dir / "scores.json").write_text('{"01": {"total": 35}}')
    from evaluation.compare import load_run
    run = load_run(run_dir)
    assert len(run.records) == 0  # no scenario JSON files


def test_load_run_includes_judge_total_from_scores_json(tmp_path):
    recs = [_rec("01-crashloop", "v2", "debugging")]
    run_dir = _make_run_dir(tmp_path, "v2", recs)
    (run_dir / "scores.json").write_text(json.dumps({"01-crashloop": {"total": 35, "result": "pass"}}))
    from evaluation.compare import load_run
    run = load_run(run_dir)
    assert run.records[0].judge_total == 35.0


# ── Task 12: Table computation tests ─────────────────────────────────────────

def _make_run_summary(version, records_data):
    from evaluation.compare import RunRecord, RunSummary
    from pathlib import Path
    records = [
        RunRecord(
            scenario_id=r["id"], version=version, category=r["cat"],
            cluster_resolved=r.get("cr"), aggregate_score=r.get("score", 0.8),
            judge_total=r.get("judge"), latency_ms=r.get("lat", 5000.0),
            total_tokens=r.get("tok", 500), tool_call_count=r.get("tools", 3),
        )
        for r in records_data
    ]
    return RunSummary(version=version, run_dir=Path("/tmp"), records=records)


def test_compute_rqa_table_resolution_rate(tmp_path):
    from evaluation.compare import compute_rqa_table
    v2 = _make_run_summary("v2", [
        {"id": "01", "cat": "debugging", "cr": True},
        {"id": "02", "cat": "debugging", "cr": True},
        {"id": "03", "cat": "debugging", "cr": False},
    ])
    v3 = _make_run_summary("v3", [
        {"id": "01", "cat": "debugging", "cr": True},
        {"id": "02", "cat": "debugging", "cr": False},
        {"id": "03", "cat": "debugging", "cr": False},
    ])
    table = compute_rqa_table([v2, v3])
    assert table[0]["version"] == "v2"
    assert table[0]["resolved"] == 2
    assert table[0]["total_verifiable"] == 3
    assert table[0]["resolution_rate_pct"] == 66.7
    assert table[1]["resolved"] == 1
    assert table[1]["resolution_rate_pct"] == 33.3


def test_compute_rqa_table_excludes_none_cluster_resolved(tmp_path):
    from evaluation.compare import compute_rqa_table
    run = _make_run_summary("v2", [
        {"id": "01", "cat": "observability", "cr": None},   # advisory, no verify.sh
        {"id": "02", "cat": "debugging", "cr": True},
    ])
    table = compute_rqa_table([run])
    assert table[0]["total_verifiable"] == 1  # None excluded


def test_compute_rqb_table_averages(tmp_path):
    from evaluation.compare import compute_rqb_table
    run = _make_run_summary("v2", [
        {"id": "01", "cat": "debugging", "score": 0.8, "judge": 32.0, "lat": 10000, "tok": 1000},
        {"id": "02", "cat": "debugging", "score": 0.6, "judge": 28.0, "lat": 20000, "tok": 2000},
    ])
    table = compute_rqb_table([run])
    assert table[0]["avg_judge_score"] == 30.0
    assert table[0]["judge_pass_rate_pct"] == 50.0   # 1 of 2 > 28 (28.0 itself does not pass)
    assert table[0]["avg_latency_s"] == 15.0
    assert table[0]["avg_tokens"] == 1500


def test_compute_rqc_table_groups_by_category(tmp_path):
    from evaluation.compare import compute_rqc_table
    run = _make_run_summary("v2", [
        {"id": "01", "cat": "debugging", "score": 0.9},
        {"id": "02", "cat": "debugging", "score": 0.5},
        {"id": "41", "cat": "deployment", "score": 0.8},
    ])
    table = compute_rqc_table([run])
    debug_row = next(r for r in table if r["category"] == "debugging")
    deploy_row = next(r for r in table if r["category"] == "deployment")
    assert debug_row["v2"] == 0.7   # (0.9 + 0.5) / 2
    assert deploy_row["v2"] == 0.8


def test_compute_rqc_winner_is_highest_version(tmp_path):
    from evaluation.compare import compute_rqc_table
    v2 = _make_run_summary("v2", [{"id": "01", "cat": "debugging", "score": 0.7}])
    v3 = _make_run_summary("v3", [{"id": "01", "cat": "debugging", "score": 0.9}])
    table = compute_rqc_table([v2, v3])
    debug_row = next(r for r in table if r["category"] == "debugging")
    assert debug_row["winner"] == "v3"


# ── Task 13: Statistical test tests ──────────────────────────────────────────

def test_chi_squared_rqa_returns_string(tmp_path):
    """chi_squared_rqa always returns a non-empty string."""
    from evaluation.compare import chi_squared_rqa
    v2 = _make_run_summary("v2", [
        {"id": "01", "cat": "debugging", "cr": True},
        {"id": "02", "cat": "debugging", "cr": False},
        {"id": "03", "cat": "debugging", "cr": True},
        {"id": "04", "cat": "debugging", "cr": True},
    ])
    v3 = _make_run_summary("v3", [
        {"id": "01", "cat": "debugging", "cr": True},
        {"id": "02", "cat": "debugging", "cr": False},
        {"id": "03", "cat": "debugging", "cr": False},
        {"id": "04", "cat": "debugging", "cr": False},
    ])
    result = chi_squared_rqa([v2, v3])
    assert isinstance(result, str)
    assert len(result) > 0


def test_wilcoxon_rqb_returns_na_with_too_few_scenarios(tmp_path):
    """Wilcoxon test returns N/A when <5 shared judged scenarios."""
    from evaluation.compare import wilcoxon_rqb
    v2 = _make_run_summary("v2", [{"id": "01", "cat": "debugging", "judge": 30.0}])
    v3 = _make_run_summary("v3", [{"id": "01", "cat": "debugging", "judge": 28.0}])
    result = wilcoxon_rqb([v2, v3])
    assert "N/A" in result
