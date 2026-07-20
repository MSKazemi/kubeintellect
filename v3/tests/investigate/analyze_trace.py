#!/usr/bin/env python3
"""
analyze_trace.py — Deep-analyze a KubeIntellect query trace from Langfuse.

Usage:
  uv run python tests/investigate/analyze_trace.py --latest
  uv run python tests/investigate/analyze_trace.py --count 5        # last 5 traces
  uv run python tests/investigate/analyze_trace.py --trace-id <id>
  uv run python tests/investigate/analyze_trace.py --all-recent     # all from past 24h

Each run appends a structured entry to the findings file so you accumulate
observations across all queries without losing anything.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# ── Config ────────────────────────────────────────────────────────────────────

LANGFUSE_URL        = os.environ.get("LANGFUSE_URL", "http://langfuse.local")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-6492d57f-1f9a-4cb7-ba41-32aa25b5912c")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-change-me")
FINDINGS_FILE       = Path(os.environ.get(
    "FINDINGS_FILE",
    Path(__file__).parent.parent.parent / ".claude/plans/investigation-findings.md"
))

AUTH = (LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY)

# ── Langfuse API helpers ──────────────────────────────────────────────────────

def lf_get(path: str, **params) -> Any:
    url = f"{LANGFUSE_URL}/api/public{path}"
    r = httpx.get(url, auth=AUTH, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def get_traces(limit: int = 20) -> list[dict]:
    return lf_get("/traces", limit=limit).get("data", [])


def get_observations(trace_id: str) -> list[dict]:
    return lf_get("/observations", traceId=trace_id, limit=100).get("data", [])


# ── Issue detectors ───────────────────────────────────────────────────────────

def detect_issues(trace: dict, obs: list[dict], llm_spans: list[dict]) -> list[dict]:
    issues = []

    # 1. Token explosion between LLM calls
    if len(llm_spans) >= 2:
        tokens_by_call = sorted(
            [(s.get("usage", {}).get("input", 0) or 0, s) for s in llm_spans],
            key=lambda x: x[0]
        )
        first_tokens = tokens_by_call[0][0]
        last_tokens  = tokens_by_call[-1][0]
        delta = last_tokens - first_tokens
        if delta > 400:
            issues.append({
                "id": "TOKEN_EXPLOSION",
                "severity": "HIGH",
                "detail": (
                    f"Prompt grew by {delta} tokens between LLM calls "
                    f"({first_tokens} → {last_tokens}). "
                    "Tool output is being fed raw into the next prompt without summarization."
                ),
            })

    # 2. Empty memory context (DB hit for nothing)
    inp = trace.get("input", {})
    if isinstance(inp, dict) and inp.get("memory_context") == "":
        issues.append({
            "id": "EMPTY_MEMORY_CONTEXT",
            "severity": "LOW",
            "detail": "memory_loader ran a DB round-trip but returned empty context. "
                      "No short-circuit when user has no stored preferences.",
        })

    # 3. High total cost
    cost = trace.get("totalCost") or 0
    if cost > 0.01:
        issues.append({
            "id": "HIGH_COST",
            "severity": "HIGH",
            "detail": f"${cost:.4f} for a single query. At scale = ${cost * 1000:.2f}/1k queries.",
        })

    # 4. High latency (API returns seconds)
    latency_ms = (trace.get("latency") or 0) * 1000
    if latency_ms > 5000:
        issues.append({
            "id": "HIGH_LATENCY",
            "severity": "MEDIUM",
            "detail": f"{latency_ms:.0f}ms total. Investigate which node is the bottleneck.",
        })

    # 5. System prompt sent multiple times (no caching signal)
    sys_prompt_count = sum(
        1 for s in llm_spans
        if isinstance(s.get("input"), list)
        and any(
            isinstance(m, dict) and m.get("role") == "system"
            for m in s["input"]
        )
    )
    if sys_prompt_count >= 2:
        issues.append({
            "id": "NO_PROMPT_CACHING",
            "severity": "HIGH",
            "detail": (
                f"System prompt sent {sys_prompt_count}× in this trace with no prompt caching. "
                "Each LLM call re-sends the full coordinator system prompt (~800+ tokens)."
            ),
        })

    # 6. No Loki/Prometheus tool used for log/metrics questions
    tool_names = {o.get("name", "") for o in obs}
    query_text = ""
    if isinstance(inp, dict):
        for m in (inp.get("messages") or []):
            if isinstance(m, dict) and m.get("type") == "human":
                query_text = m.get("content", "").lower()
                break
    log_keywords   = ("log", "error", "crash", "exception", "stderr")
    metric_keywords = ("cpu", "memory", "metric", "usage", "trend", "latency")
    if any(k in query_text for k in log_keywords) and "query_loki" not in tool_names:
        issues.append({
            "id": "LOKI_NOT_USED",
            "severity": "MEDIUM",
            "detail": "Query mentions logs but query_loki was not called — "
                      "coordinator used kubectl logs instead. May miss historical data.",
        })
    if any(k in query_text for k in metric_keywords) and "query_prometheus" not in tool_names:
        issues.append({
            "id": "PROMETHEUS_NOT_USED",
            "severity": "MEDIUM",
            "detail": "Query mentions metrics/usage but query_prometheus was not called. "
                      "kubectl top gives only current snapshot, not trends.",
        })

    # 7. Large number of LLM calls
    if len(llm_spans) > 3:
        issues.append({
            "id": "MANY_LLM_CALLS",
            "severity": "MEDIUM",
            "detail": f"{len(llm_spans)} LLM calls in one query. "
                      "Coordinator may be looping unnecessarily.",
        })

    # 8. kubectl output likely truncated
    kubectl_obs = [o for o in obs if o.get("name") == "run_kubectl"]
    for ko in kubectl_obs:
        out = str(ko.get("output") or "")
        if "[truncated" in out or "chars omitted" in out:
            issues.append({
                "id": "OUTPUT_TRUNCATED",
                "severity": "MEDIUM",
                "detail": f"kubectl output was truncated. LLM may be missing data. "
                          f"Command: {ko.get('input', {}).get('command', '?')}",
            })

    return issues


# ── Core analysis ─────────────────────────────────────────────────────────────

def analyze(trace_id: str) -> dict:
    trace = lf_get(f"/traces/{trace_id}")
    obs   = get_observations(trace_id)

    # Classify spans
    llm_spans    = [o for o in obs if o.get("type") == "GENERATION"]
    tool_obs     = [o for o in obs if o.get("name") in ("run_kubectl", "query_prometheus", "query_loki")]
    node_obs     = {
        o["name"]: o for o in obs
        if o.get("name") in ("memory_loader", "coordinator", "route_coordinator",
                             "subagent_executor", "_synthesize")
    }

    # Extract query text
    query = "(unknown)"
    inp = trace.get("input", {})
    if isinstance(inp, dict):
        for m in (inp.get("messages") or []):
            if isinstance(m, dict) and m.get("type") == "human":
                query = m.get("content", "(unknown)")[:300]
                break

    # Token summary
    total_input_tokens  = sum(s.get("usage", {}).get("input",  0) or 0 for s in llm_spans)
    total_output_tokens = sum(s.get("usage", {}).get("output", 0) or 0 for s in llm_spans)
    total_tokens        = total_input_tokens + total_output_tokens
    total_cost          = trace.get("totalCost") or 0
    total_latency_ms    = (trace.get("latency") or 0) * 1000  # API returns seconds

    # Per-LLM-call breakdown (sorted chronologically by latency as proxy)
    llm_calls = [
        {
            "input_tokens":  s.get("usage", {}).get("input",  0) or 0,
            "output_tokens": s.get("usage", {}).get("output", 0) or 0,
            "latency_ms":    (s.get("latency") or 0) * 1000,
            "cost":          s.get("calculatedTotalCost") or 0,
        }
        for s in sorted(llm_spans, key=lambda x: x.get("startTime", ""))
    ]

    # Tool calls — input may arrive as a repr string or a dict
    def _parse_input(raw) -> dict:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                import ast
                return ast.literal_eval(raw)
            except Exception:
                return {}
        return {}

    tool_calls = [
        {
            "tool":       o.get("name"),
            "command":    _parse_input(o.get("input")).get("command", "") if o.get("name") == "run_kubectl" else "",
            "latency_ms": (o.get("latency") or 0) * 1000,
        }
        for o in tool_obs
    ]

    # Node latencies — latency field is in seconds, convert to ms
    node_latencies = {
        name: (o.get("latency") or 0) * 1000
        for name, o in node_obs.items()
    }

    # RCA path?
    rca_used = isinstance(inp, dict) and inp.get("rca_required", False)
    findings_count = len((inp.get("findings") or [])) if isinstance(inp, dict) else 0

    issues = detect_issues(trace, obs, llm_spans)

    return {
        "trace_id":            trace_id,
        "timestamp":           trace.get("timestamp", ""),
        "query":               query,
        "total_latency_ms":    total_latency_ms,
        "total_cost_usd":      total_cost,
        "total_input_tokens":  total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens":        total_tokens,
        "llm_call_count":      len(llm_spans),
        "llm_calls":           llm_calls,
        "tool_calls":          tool_calls,
        "node_latencies_ms":   node_latencies,
        "rca_used":            rca_used,
        "subagent_findings":   findings_count,
        "observation_count":   len(obs),
        "issues":              issues,
    }


# ── Formatters ────────────────────────────────────────────────────────────────

SEV_ICON = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵"}

def print_report(r: dict) -> None:
    ts = r["timestamp"][:19].replace("T", " ") if r["timestamp"] else "?"
    print(f"\n{'='*70}")
    print(f"  TRACE: {r['trace_id']}")
    print(f"  Time:  {ts}")
    print(f"  Query: {r['query'][:120]}")
    print(f"{'='*70}")
    print(f"  Latency:  {r['total_latency_ms']:.0f} ms")
    print(f"  Cost:     ${r['total_cost_usd']:.5f}")
    print(f"  Tokens:   {r['total_input_tokens']} in / {r['total_output_tokens']} out "
          f"(Σ {r['total_tokens']})")
    print(f"  LLM calls:{r['llm_call_count']}")
    print(f"  Observations: {r['observation_count']}")
    print(f"  RCA path: {'YES' if r['rca_used'] else 'no'}")
    print()

    if r["llm_calls"]:
        print("  LLM call breakdown:")
        for i, c in enumerate(r["llm_calls"], 1):
            print(f"    Call {i}: {c['input_tokens']} in → {c['output_tokens']} out  "
                  f"| {c['latency_ms']:.0f}ms  | ${c['cost']:.5f}")
    print()

    if r["tool_calls"]:
        print("  Tool calls:")
        for t in r["tool_calls"]:
            cmd = f"  `{t['command']}`" if t["command"] else ""
            print(f"    {t['tool']}{cmd}  ({t['latency_ms']:.0f}ms)")
    print()

    if r["node_latencies_ms"]:
        print("  Node latencies (ms):")
        for name, lat in sorted(r["node_latencies_ms"].items(), key=lambda x: -x[1]):
            bar = "█" * min(int(lat / 200), 30)
            print(f"    {name:<22} {lat:>6.0f}ms  {bar}")
    print()

    if r["issues"]:
        print(f"  Issues detected ({len(r['issues'])}):")
        for iss in r["issues"]:
            icon = SEV_ICON.get(iss["severity"], "⚪")
            print(f"    {icon} [{iss['id']}]  ({iss['severity']})")
            print(f"       {iss['detail']}")
    else:
        print("  ✅ No issues detected.")
    print()


def findings_entry(r: dict) -> str:
    ts = r["timestamp"][:19].replace("T", " ") if r["timestamp"] else "?"
    lines = [
        f"\n### Query: `{r['query'][:120]}`",
        f"- **Trace ID:** `{r['trace_id']}`  |  **Time:** {ts}",
        f"- **Latency:** {r['total_latency_ms']:.0f}ms  |  "
        f"**Cost:** ${r['total_cost_usd']:.5f}  |  "
        f"**Tokens:** {r['total_input_tokens']}→{r['total_output_tokens']} (Σ{r['total_tokens']})",
        f"- **LLM calls:** {r['llm_call_count']}  |  "
        f"**Tools:** {', '.join(t['tool'] for t in r['tool_calls']) or 'none'}  |  "
        f"**RCA:** {'yes' if r['rca_used'] else 'no'}",
    ]
    if r["llm_calls"]:
        lines.append("- **LLM breakdown:**")
        for i, c in enumerate(r["llm_calls"], 1):
            lines.append(f"  - Call {i}: `{c['input_tokens']}→{c['output_tokens']} tokens`, "
                         f"`{c['latency_ms']:.0f}ms`, `${c['cost']:.5f}`")
    if r["issues"]:
        lines.append("- **Issues:**")
        for iss in r["issues"]:
            icon = SEV_ICON.get(iss["severity"], "⚪")
            lines.append(f"  - {icon} `{iss['id']}` — {iss['detail']}")
    else:
        lines.append("- **Issues:** none")
    return "\n".join(lines)


def append_to_findings(entry: str) -> None:
    FINDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with FINDINGS_FILE.open("a", encoding="utf-8") as f:
        f.write(entry + "\n")
    print(f"  → Appended to {FINDINGS_FILE}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze KubeIntellect traces from Langfuse")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--latest",    action="store_true", help="Analyze the most recent trace")
    group.add_argument("--count",     type=int,            help="Analyze the N most recent traces")
    group.add_argument("--trace-id",  metavar="ID",        help="Analyze a specific trace by ID")
    group.add_argument("--all-recent",action="store_true", help="Analyze all traces from past 24h")
    parser.add_argument("--no-save",  action="store_true", help="Don't append to findings file")
    args = parser.parse_args()

    trace_ids: list[str] = []

    if args.trace_id:
        trace_ids = [args.trace_id]
    elif args.latest:
        traces = get_traces(limit=1)
        if not traces:
            print("No traces found in Langfuse.")
            sys.exit(1)
        trace_ids = [traces[0]["id"]]
    elif args.count:
        traces = get_traces(limit=args.count)
        trace_ids = [t["id"] for t in traces]
    elif args.all_recent:
        traces = get_traces(limit=50)
        trace_ids = [t["id"] for t in traces]

    if not trace_ids:
        print("No traces to analyze.")
        sys.exit(1)

    print(f"\nAnalyzing {len(trace_ids)} trace(s)...")

    for tid in trace_ids:
        try:
            result = analyze(tid)
            print_report(result)
            if not args.no_save:
                entry = findings_entry(result)
                append_to_findings(entry)
        except Exception as e:
            print(f"  ERROR analyzing trace {tid}: {e}")


if __name__ == "__main__":
    main()
