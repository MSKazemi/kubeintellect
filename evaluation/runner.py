"""
KubeIntellect evaluation runner — single entry point.

Three subcommands:

  run      Run scenarios from evaluation/scenarios/.
           Each scenario dir has query.md (required), setup.yaml (optional fault
           injection), and expected.md (optional — enables LLM judge scoring).

  csv      Bulk query testing from a CSV file. No fault injection; measures
           latency and completion rate.

  stress   Concurrent load test with a fixed query.

Usage
-----
  # Run all scenarios (signal scoring only)
  uv run python -m evaluation.runner run

  # Run specific scenarios with LLM judge (8-dim /40 rubric)
  uv run python -m evaluation.runner run --scenario 01 --scenario 06 --judge

  # Run all scenarios that have expected.md (scored ones only)
  uv run python -m evaluation.runner run --scored --judge

  # Bulk CSV testing (pass any CSV file with a 'query' column)
  uv run python -m evaluation.runner csv my_queries.csv

  # Stress test (10 workers, 50 requests)
  uv run python -m evaluation.runner stress --concurrency 10 --requests 50

Output: evaluation/runs/<tag>_<timestamp>/
  metadata.json   git commit, date, tag, api_url
  results.csv     per-scenario: status, latency_ms, hitl, scores
  <ID>.json       full EvalRecord per scenario
  scores.json     LLM judge results (--judge only)
  report_*.md     signal scoring report + lessons learned

Environment
-----------
  KUBEINTELLECT_URL      API base (default: http://api.kubeintellect.local)
  KUBEINTELLECT_API_KEY  Bearer token
  LANGFUSE_PUBLIC_KEY    for trace collection
  LANGFUSE_SECRET_KEY
  LANGFUSE_HOST
  LOKI_URL               for log collection
  PROMETHEUS_URL         for metrics
  ANTHROPIC_API_KEY      for --judge (preferred)
  AZURE_OPENAI_API_KEY   for --judge (fallback)
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # picks up .env from cwd or any parent directory

from .client import KubeIntellectClient
from .collectors.langfuse_collector import LangfuseCollector
from .collectors.loki_collector import LokiCollector
from .collectors.prometheus_collector import PrometheusCollector
from .follow_up import DEFAULT_FOLLOW_UP, agent_self_gated
from .models import EvalRecord, EvalResult, TurnResult
from .report import generate_report, save_record_json
from .scorers.aggregate import aggregate
from .scorers.issue_detector import detect as detect_issues
from .scorers.signal_scorer import score_signals
from . import judge as _judge

SCENARIOS_DIR = Path(__file__).parent / "scenarios"
RUNS_DIR      = Path(__file__).parent / "runs"
DEFAULT_URL   = os.environ.get("KUBEINTELLECT_URL", "http://api.kubeintellect.local")
DEFAULT_KEY   = (
    os.environ.get("KUBEINTELLECT_API_KEY")
    or os.environ.get("KUBEINTELLECT_SUPERADMIN_KEYS", "").split(",")[0]
    or os.environ.get("KUBEINTELLECT_ADMIN_KEYS", "").split(",")[0]
    or ""
)
TARGET_URLS: dict[str, str] = {
    "v1": "http://api-v1.kubeintellect.local",
    "v2": "http://api.kubeintellect.local",
    "v3": "http://api-v3.kubeintellect.local",
}
K8S_NAMESPACE = "scenario-test"


def _read_category(scenario_dir: Path) -> str:
    """Read scenario category from metadata.yaml. Returns '' if not present."""
    meta = scenario_dir / "metadata.yaml"
    if not meta.exists():
        return ""
    for line in meta.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("category:"):
            return stripped.split(":", 1)[1].strip().split("#")[0].strip()
    return ""


async def _collect_trace_with_retry(langfuse, session_id: str, max_wait: int = 20):
    """Poll Langfuse until the trace has at least one generation or we time out."""
    if not langfuse.enabled:
        from .collectors.langfuse_collector import _empty
        return _empty(session_id, available=False)
    for attempt in range(5):
        wait = [3, 4, 5, 5, 5][attempt]
        await asyncio.sleep(wait)
        trace = await langfuse.collect(session_id)
        if trace.available and trace.generations:
            return trace
        if attempt < 4:
            continue
    return trace  # best-effort after exhausting retries


def _to_turn(idx: int, sent: str, r: EvalResult) -> TurnResult:
    """Snapshot one round-trip into a TurnResult."""
    return TurnResult(
        turn_index=idx,
        sent_content=sent,
        final_text=r.final_text,
        tool_calls=list(r.tool_calls),
        had_hitl=r.had_hitl,
        had_error=r.had_error,
        latency_ms=r.latency_ms,
    )


def _merge_results(a: EvalResult, b: EvalResult) -> EvalResult:
    """Merge two EvalResults from the same session into one for downstream scorers.

    final_text becomes the second turn's text (the answer to grade — that's
    the post-confirmation answer). Tool calls and events are concatenated.
    Existing scorers/judge see the merged result and need no changes.
    """
    return EvalResult(
        session_id=a.session_id,
        query_id=a.query_id,
        messages=a.messages,
        final_text=b.final_text or a.final_text,
        events=a.events + b.events,
        had_hitl=a.had_hitl or b.had_hitl,
        had_error=a.had_error or b.had_error,
        error_message=b.error_message or a.error_message,
        latency_ms=a.latency_ms + b.latency_ms,
        start_ts=a.start_ts,
        end_ts=b.end_ts,
        tool_calls=a.tool_calls + b.tool_calls,
        tool_errors=a.tool_errors + b.tool_errors,
        status_phases=a.status_phases + b.status_phases,
        hitl_commands=a.hitl_commands + b.hitl_commands,
    )


# ── Scenario loading ──────────────────────────────────────────────────────────

def list_scenarios(
    prefix_filter: list[str] | None = None,
    scored_only: bool = False,
    category_filter: set[str] | None = None,
) -> list[Path]:
    dirs = sorted(
        [d for d in SCENARIOS_DIR.iterdir() if d.is_dir() and d.name[0].isdigit()],
        key=lambda d: d.name,
    )
    if prefix_filter:
        dirs = [d for d in dirs if any(d.name.startswith(p) for p in prefix_filter)]
    if category_filter:
        dirs = [d for d in dirs if _read_category(d) in category_filter]
    if scored_only:
        dirs = [d for d in dirs if (d / "expected.md").exists()]
    return dirs


def load_scenario(scenario_dir: Path) -> dict:
    setup_yaml = scenario_dir / "setup.yaml"
    return {
        "id":        scenario_dir.name,
        "query":     (scenario_dir / "query.md").read_text(encoding="utf-8").strip(),
        "setup":     setup_yaml if (setup_yaml.exists() and _has_k8s_content(setup_yaml)) else None,
        "pre_run":   scenario_dir / "pre_run.sh"  if (scenario_dir / "pre_run.sh").exists()  else None,
        "post_run":  scenario_dir / "post_run.sh" if (scenario_dir / "post_run.sh").exists() else None,
        "expected":  (scenario_dir / "expected.md").read_text(encoding="utf-8")
                     if (scenario_dir / "expected.md").exists() else None,
        "verify":    scenario_dir / "verify.sh" if (scenario_dir / "verify.sh").exists() else None,
        "category":  _read_category(scenario_dir),
    }


# ── Fault injection ───────────────────────────────────────────────────────────

def _kubectl(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl"] + args, capture_output=True, text=True)


def _has_k8s_content(setup_yaml: Path) -> bool:
    """True if setup.yaml contains real K8s documents (not only comments/whitespace)."""
    for line in setup_yaml.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return True
    return False


def _run_hook(script: Path, label: str) -> None:
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  [{label} failed]: {r.stderr[:200]}")


def _apply_fault(setup_yaml: Path) -> bool:
    r = _kubectl(["apply", "-f", str(setup_yaml)])
    if r.returncode != 0:
        print(f"  [kubectl apply failed]: {r.stderr[:200]}")
    return r.returncode == 0


def _delete_fault(setup_yaml: Path) -> None:
    _kubectl(["delete", "-f", str(setup_yaml), "--ignore-not-found"])


def _verify_cluster(verify_sh: Path, timeout: int = 90) -> bool:
    """Run verify.sh and return True if the cluster issue was resolved."""
    try:
        r = subprocess.run(
            ["bash", str(verify_sh)],
            capture_output=True, text=True,
            timeout=timeout + 10,
        )
    except subprocess.TimeoutExpired:
        print("  ❌ cluster verify timed out")
        return False
    if r.returncode == 0:
        print("  ✅ cluster: issue resolved")
        return True
    detail = (r.stderr or r.stdout).strip()[:120]
    print(f"  ❌ cluster: issue NOT resolved — {detail}")
    return False


def _wait_for_fault(timeout: int = 60) -> None:
    bad = {"CrashLoop", "ImagePull", "Pending", "OOMKilled", "Error", "Init:", "Terminating"}
    deadline = time.monotonic() + timeout
    print(f"  Waiting for fault in ns/{K8S_NAMESPACE}...", end="", flush=True)
    while time.monotonic() < deadline:
        r = _kubectl(["get", "pods", "-n", K8S_NAMESPACE, "--no-headers"])
        if any(kw in r.stdout for kw in bad):
            print(" confirmed.", flush=True)
            return
        print(".", end="", flush=True)
        time.sleep(3)
    print(" timeout — proceeding.", flush=True)


def _fetch_app_logs(tail: int = 200) -> str:
    """Fetch KubeIntellect pod logs. Returns empty string when running in dev mode (not deployed to cluster)."""
    r = _kubectl(["logs", "deployments/kubeintellect", "-n", "kubeintellect", f"--tail={tail}"])
    if r.returncode != 0:
        return ""  # not deployed in cluster (dev mode) — skip silently
    return r.stdout


# ── Shared helpers ────────────────────────────────────────────────────────────

def _write_metadata(out_dir: Path, tag: str, mode: str, api_url: str) -> None:
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        commit = "unknown"
    (out_dir / "metadata.json").write_text(json.dumps({
        "date": datetime.now(timezone.utc).isoformat(),
        "tag": tag, "mode": mode,
        "git_commit": commit, "api_url": api_url,
    }, indent=2))


def _open_csv(out_dir: Path, fields: list[str]):
    path = out_dir / "results.csv"
    f = open(path, "w", newline="", encoding="utf-8")
    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    return f, w


# ── Mode: run (scenarios) ─────────────────────────────────────────────────────

async def run_scenarios(args) -> None:
    scenarios = list_scenarios(
        prefix_filter=args.scenario,
        scored_only=args.scored,
    )
    if not scenarios:
        print("No scenarios matched. Check evaluation/scenarios/ and your filters.")
        return

    tag     = args.tag or "run"
    ts      = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = RUNS_DIR / f"{tag}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_metadata(out_dir, tag, "run", args.api_url)

    client   = KubeIntellectClient(api_url=args.api_url, api_key=args.api_key)
    langfuse = LangfuseCollector()
    loki     = LokiCollector()
    prom     = PrometheusCollector()
    llm      = _judge.build_client() if args.judge else None

    csv_fields = ["index", "scenario", "query_preview", "status", "latency_ms",
                  "hitl", "cluster_resolved", "score_signal", "score_judge", "judge_result"]
    csv_file, csv_writer = _open_csv(out_dir, csv_fields)

    all_records: list[EvalRecord] = []
    all_judge_scores: dict[str, dict] = {}
    run_start = time.time()

    print(f"\n=== KubeIntellect Evaluation: {tag}_{ts} ===")
    print(f"Scenarios : {len(scenarios)}")
    print(f"URL       : {client.api_url}")
    print(f"Langfuse  : {'✓' if langfuse.enabled else '✗'}  "
          f"Loki: {'✓' if loki.enabled else '✗'}  "
          f"Prometheus: {'✓' if prom.enabled else '✗'}  "
          f"Judge: {'✓' if llm else '✗'}\n")

    for idx, scenario_dir in enumerate(scenarios, 1):
        sc         = load_scenario(scenario_dir)
        session_id = f"eval-{ts}-{sc['id']}"

        print(f"[{idx}/{len(scenarios)}] {sc['id']}")
        print(f"  Q: {sc['query'][:80]}")

        # Pre-run hook (e.g. taints, namespace cleanup)
        if sc["pre_run"]:
            _run_hook(sc["pre_run"], "pre_run")

        # Fault injection
        if sc["setup"]:
            if not _apply_fault(sc["setup"]):
                csv_writer.writerow({"index": idx, "scenario": sc["id"], "status": "setup_failed"})
                csv_file.flush()
                if sc["post_run"]:
                    _run_hook(sc["post_run"], "post_run")
                continue
            _wait_for_fault()
        logs_before = _fetch_app_logs() if sc["setup"] else ""

        # Send query (turn 0)
        try:
            result = await client.query(
                messages=[{"role": "user", "content": sc["query"]}],
                query_id=sc["id"],
                session_id=session_id,
                auto_approve=args.auto_approve,
            )
        except Exception as exc:
            print(f"  ❌ query failed: {exc}")
            if sc["setup"]:
                _delete_fault(sc["setup"])
            if sc["post_run"]:
                _run_hook(sc["post_run"], "post_run")
            continue

        status = "error" if result.had_error else ("hitl" if result.had_hitl else "ok")
        print(f"  {('❌' if result.had_error else '✅')} {result.latency_ms:.0f}ms  "
              f"tools={result.tool_calls}  hitl={result.had_hitl}")

        # Multi-turn follow-up: if the agent self-gated ("Would you like me
        # to apply this fix?") and --multi-turn is enabled, send a default
        # confirmation on the same X-Session-ID and merge the second turn.
        agent_asked_first = False
        follow_up_sent: str | None = None
        turns: list[TurnResult] = [_to_turn(0, sc["query"], result)]

        if args.multi_turn and not result.had_error and agent_self_gated(
            result.final_text, result.tool_calls
        ):
            follow_text = args.follow_up_text or DEFAULT_FOLLOW_UP
            agent_asked_first = True
            follow_up_sent = follow_text
            print(f"  ↪ agent self-gated, sending follow-up: {follow_text!r}")
            try:
                turn2 = await client.query_followup(
                    follow_up_text=follow_text,
                    query_id=sc["id"],
                    session_id=session_id,
                    auto_approve=args.auto_approve,
                )
                result = _merge_results(result, turn2)
                turns.append(_to_turn(1, follow_text, turn2))
                print(f"  ↳ follow-up done {turn2.latency_ms:.0f}ms tools={turn2.tool_calls}")
                # Update status if the follow-up resolved the error/hitl flags
                status = "error" if result.had_error else ("hitl" if result.had_hitl else "ok")
            except Exception as exc:
                print(f"  ❌ follow-up failed: {exc}")

        # Save response + logs
        (out_dir / f"{sc['id']}_response.txt").write_text(result.final_text, encoding="utf-8")
        if sc["setup"] and (logs_before or True):
            logs_after = _fetch_app_logs()
            combined = (logs_before + "\n--- AFTER ---\n" + logs_after).strip()
            if combined and combined != "--- AFTER ---":
                (out_dir / f"{sc['id']}_logs.txt").write_text(combined + "\n")

        # Collect observability signals — retry until Langfuse flushes (up to ~20 s)
        trace = await _collect_trace_with_retry(langfuse, session_id)
        logs  = await loki.collect(session_id, result.start_ts, result.end_ts)

        if trace.available:
            print(f"  📊 tokens={trace.total_tokens} llm_calls={len(trace.generations)}")

        # Cluster verification — must run BEFORE fault cleanup
        cluster_resolved: bool | None = None
        if sc["setup"] and sc["verify"] and not result.had_error:
            cluster_resolved = _verify_cluster(sc["verify"])
            if cluster_resolved is False and status == "ok":
                status = "unresolved"

        # Signal scoring + trace issue detection
        query_dict = {
            "id": sc["id"], "title": sc["id"], "category": "",
            "messages": [{"role": "user", "content": sc["query"]}],
        }
        scores        = score_signals(query_dict, result, trace, logs, cluster_resolved=cluster_resolved)
        t_issues      = detect_issues(
            result, trace, logs,
            agent_asked_first=agent_asked_first,
            cluster_resolved=cluster_resolved,
        )
        agg           = aggregate(scores, t_issues)

        # LLM judge scoring (only if expected.md exists)
        judge_scores: dict | None = None
        judge_total  = ""
        judge_result = ""
        if llm and sc["expected"]:
            print("  Judging with LLM rubric...")
            judge_scores = _judge.score(
                result.final_text, sc["expected"], llm,
                auto_approve=args.auto_approve,
                tool_calls=result.tool_calls,
            )
            all_judge_scores[sc["id"]] = judge_scores
            judge_total  = judge_scores.get("total", "")
            judge_result = judge_scores.get("result", "")
            print(_judge.format_scores(judge_scores))
        elif args.judge and not sc["expected"]:
            print("  (no expected.md — skipping judge)")

        record = EvalRecord(
            query_id=sc["id"], title=sc["id"],
            session_id=session_id, run_id=f"{tag}_{ts}",
            result=result, trace=trace, logs=logs,
            scores=scores, aggregate_score=agg,
            trace_issues=t_issues,
            review_verdict=None,
            timestamp=datetime.now(timezone.utc).isoformat(),
            cluster_resolved=cluster_resolved,
            agent_asked_first=agent_asked_first,
            follow_up_sent=follow_up_sent,
            num_turns=len(turns),
            turns=turns,
        )
        all_records.append(record)
        save_record_json(record, out_dir / f"{sc['id']}.json")

        csv_writer.writerow({
            "index": idx, "scenario": sc["id"],
            "query_preview": sc["query"][:80],
            "status": status, "latency_ms": f"{result.latency_ms:.0f}",
            "hitl": result.had_hitl,
            "cluster_resolved": "" if cluster_resolved is None else ("yes" if cluster_resolved else "no"),
            "score_signal": f"{agg:.2f}",
            "score_judge": judge_total, "judge_result": judge_result,
        })
        csv_file.flush()

        if scores.notes:
            for note in scores.notes[:2]:
                print(f"  ⚠  {note}")
        high = [i for i in t_issues if i.severity == "HIGH"]
        if high:
            for iss in high:
                print(f"  🔴 [{iss.id}] {iss.detail[:100]}")

        # Fault cleanup + post-run hook (e.g. remove taints)
        if sc["setup"]:
            _delete_fault(sc["setup"])
        if sc["post_run"]:
            _run_hook(sc["post_run"], "post_run")
        print()

    csv_file.close()

    # Save LLM judge results
    if all_judge_scores:
        (out_dir / "scores.json").write_text(json.dumps(all_judge_scores, indent=2))
        passed = sum(1 for s in all_judge_scores.values() if s.get("result") == "pass")
        avg    = sum(s.get("total", 0) for s in all_judge_scores.values()) / max(len(all_judge_scores), 1)
        print(f"Judge: {passed}/{len(all_judge_scores)} passed | avg {avg:.1f}/40")

    # Signal scoring report
    if all_records:
        prom_metrics = await prom.collect_window(run_start, time.time())
        report_path  = generate_report(all_records, f"{tag}_{ts}", out_dir)
        avg_score    = sum(r.aggregate_score for r in all_records) / len(all_records)
        completed    = sum(1 for r in all_records if r.scores.completion == 1.0)
        print(f"Signal score: {avg_score:.2f} avg | {completed}/{len(all_records)} completed")
        print(f"Report: {report_path}")

    print(f"Output: {out_dir}")


def run_csv(args) -> None:
    path = Path(args.file)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "query" not in (reader.fieldnames or []):
            print(f"CSV must have a 'query' column. Found: {reader.fieldnames}")
            sys.exit(1)
        queries = [row for row in reader if row.get("query", "").strip()]

    tag     = args.tag or path.stem
    ts      = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = RUNS_DIR / f"{tag}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_metadata(out_dir, tag, "csv", args.api_url)

    fields   = ["index", "query", "status", "latency_s", "hitl", "response_file"]
    csv_file, csv_writer = _open_csv(out_dir, fields)
    client   = KubeIntellectClient(api_url=args.api_url, api_key=args.api_key)

    ok = hitl = error = 0
    latencies: list[float] = []

    print(f"\n=== CSV run: {path.name} ({len(queries)} queries) ===")
    for idx, row in enumerate(queries, 1):
        query = row["query"].strip()
        sid   = f"eval-csv-{ts}-{idx:03d}"
        print(f"[{idx:>3}/{len(queries)}] {query[:70]}", end="  ", flush=True)

        text, lat, had_hitl = client.query_sync(query, sid, auto_approve=args.auto_approve)
        status = "error" if text.startswith("ERROR:") else ("hitl" if had_hitl else "ok")
        print(f"{status}  {lat:.1f}s")

        resp_file = out_dir / f"query_{idx:03d}.txt"
        resp_file.write_text(text, encoding="utf-8")

        if status.startswith("ok"):
            ok += 1
            latencies.append(lat)
        elif "hitl" in status:
            hitl += 1
        else:
            error += 1

        csv_writer.writerow({"index": idx, "query": query[:120], "status": status,
                              "latency_s": f"{lat:.1f}", "hitl": had_hitl,
                              "response_file": resp_file.name})
        csv_file.flush()

    csv_file.close()

    if latencies:
        latencies.sort()
        p95 = latencies[int(len(latencies) * 0.95)]
        print(f"\nok={ok}  hitl={hitl}  error={error}  "
              f"mean={sum(latencies)/len(latencies):.1f}s  p95={p95:.1f}s")
    print(f"Output: {out_dir}")


# ── Mode: stress ──────────────────────────────────────────────────────────────

def run_stress(args) -> None:
    query = args.query or "get all pods across all namespaces"
    print(f"Stress: {args.requests} requests | {args.concurrency} workers")
    print(f"Query: {query}\n")

    ok = error = 0
    latencies: list[float] = []
    start_wall = time.monotonic()
    client     = KubeIntellectClient(api_url=args.api_url, api_key=args.api_key)

    def _send(i: int) -> tuple[float, bool]:
        sid = f"eval-stress-{uuid.uuid4().hex[:8]}"
        _, lat, had_hitl = client.query_sync(query, sid)
        return lat, had_hitl

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(_send, i) for i in range(args.requests)]
        for f in as_completed(futures):
            lat, _ = f.result()
            if lat > 0:
                ok += 1
                latencies.append(lat)
            else:
                error += 1
            print(f"  {lat:.1f}s", flush=True)

    elapsed = time.monotonic() - start_wall
    if latencies:
        latencies.sort()
        p95 = latencies[int(len(latencies) * 0.95)]
        p99 = latencies[int(len(latencies) * 0.99)]
        print(f"\nok={ok}  error={error}  "
              f"mean={sum(latencies)/len(latencies):.1f}s  p95={p95:.1f}s  p99={p99:.1f}s  "
              f"rps={args.requests/elapsed:.2f}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(description="KubeIntellect evaluation runner")
    parser.add_argument("--api-url", default=None,
                        help="API base URL (overrides --target)")
    parser.add_argument("--target", choices=["v1", "v2", "v3"], default="v2",
                        help="Version to target: v1|v2|v3 (sets default API URL)")
    parser.add_argument("--api-key", default=DEFAULT_KEY)
    parser.add_argument("--tag", default="", help="Label for output directory")
    parser.add_argument("--no-auto-approve", dest="auto_approve",
                        action="store_false", default=True)

    sub = parser.add_subparsers(dest="mode", required=True)

    # run — scenario-based evaluation
    rp = sub.add_parser("run", help="Run scenario dirs from evaluation/scenarios/")
    rp.add_argument("--scenario", action="append", metavar="PREFIX",
                    help="Scenario prefix to run (e.g. '01'); repeatable")
    rp.add_argument("--scored", action="store_true",
                    help="Only scenarios that have expected.md (LLM-judgeable)")
    rp.add_argument("--judge", action="store_true",
                    help="Score responses with LLM rubric (8 dim, /40)")
    rp.add_argument("--multi-turn", dest="multi_turn", action="store_true",
                    default=True,
                    help="When the agent self-gates with a confirmation question, "
                         "send a follow-up reply on the same session and re-verify "
                         "(default: enabled).")
    rp.add_argument("--no-multi-turn", dest="multi_turn", action="store_false",
                    help="Disable multi-turn follow-up.")
    rp.add_argument("--follow-up-text", default=None,
                    help="Override the default follow-up reply "
                         f"(default: {DEFAULT_FOLLOW_UP!r})")
    rp.add_argument("--categories", default=None,
                    help="Comma-separated category filter (e.g. debugging,deployment)")

    # csv — bulk query testing
    cp = sub.add_parser("csv", help="Run queries from a CSV file")
    cp.add_argument("file", help="CSV file with a 'query' column")

    # stress — load test
    sp = sub.add_parser("stress", help="Concurrent load test")
    sp.add_argument("--concurrency", type=int, default=5)
    sp.add_argument("--requests",    type=int, default=20)
    sp.add_argument("--query",       default=None, help="Query to repeat")

    args = parser.parse_args()

    # Resolve API URL: explicit --api-url > target preset > env default
    if args.api_url is None:
        args.api_url = TARGET_URLS.get(args.target, DEFAULT_URL)

    if args.mode == "run":
        asyncio.run(run_scenarios(args))
    elif args.mode == "csv":
        run_csv(args)
    elif args.mode == "stress":
        run_stress(args)


if __name__ == "__main__":
    _cli()
