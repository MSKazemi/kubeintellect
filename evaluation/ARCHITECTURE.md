# Evaluation Architecture

## Directory layout

```
evaluation/
├── runner.py               ← single entry point (run / csv / stress)
├── client.py               ← SSE client (async query + sync query_sync)
├── models.py               ← all shared data structures
├── judge.py                ← LLM judge (8-dimension rubric, /40)
├── lessons.py              ← cross-run pattern analysis → markdown
├── report.py               ← per-run markdown report generator
├── analyze_trace.py        ← standalone deep-trace CLI (ad-hoc)
├── run_query.sh            ← ad-hoc one-shot query + trace analysis
├── cleanup.sh              ← delete all fault-injection k8s resources
├── collectors/
│   ├── langfuse_collector.py   ← Langfuse REST (async collect + sync get)
│   ├── loki_collector.py       ← Loki LogQL query
│   └── prometheus_collector.py ← Prometheus range query
├── scorers/
│   ├── signal_scorer.py        ← 5 automated signal dimensions
│   ├── issue_detector.py       ← 7 trace-level issue detectors
│   └── aggregate.py            ← weighted final score
├── scenarios/                  ← 40+ scenario directories
│   ├── 01-crashloop/
│   │   ├── setup.yaml          ← kubectl YAML to inject fault
│   │   ├── query.md            ← the question sent to KubeIntellect
│   │   └── expected.md         ← gold answer for LLM judge
│   └── 26-cluster-overview/
│       └── query.md            ← read-only (no fault, no expected.md)
└── runs/                       ← output of each run
    └── run_20260427_120000/
        ├── metadata.json
        ├── results.csv
        ├── 01-crashloop.json       ← full EvalRecord
        ├── 01-crashloop_response.txt
        ├── scores.json             ← LLM judge scores (--judge only)
        └── report_run_*.md
```

---

## Scenario types

| Range | Type | Has `setup.yaml` | Has `expected.md` |
|---|---|---|---|
| 01–25 | Fault scenarios | Yes — injects broken state | Yes — gold rubric for LLM judge |
| 26–40 | Read-only queries | No | No |

---

## Full run pipeline (`runner run`)

```
for each scenario dir
│
├─ 1. FAULT INJECTION
│    kubectl apply -f setup.yaml
│    poll pods until bad status appears (CrashLoop / Pending / Error…)
│    snapshot app logs before query
│
├─ 2. SEND QUERY  (client.py — async SSE stream)
│    POST /v1/chat/completions
│    headers: X-Session-ID, Authorization
│    body:    { messages, stream:true, auto_approve:true }  ← HITL bypass
│    parse SSE stream:
│      ki_event.type == "tool_call"   → record tool name
│      ki_event.type == "tool_result" → detect tool errors
│      ki_event.type == "status"      → record coordinator phases
│      ki_event.type == "error"       → set had_error
│      choices[].delta.content        → accumulate final_text
│      choices[].hitl_required:true   → record HITL (shouldn't fire with auto_approve)
│      data:[DONE]                    → stop
│
├─ 3. COLLECT OBSERVABILITY  (collectors/)
│    Langfuse  → trace_id by session_id → all observations
│               → LangfuseGeneration (tokens, latency per LLM call)
│               → LangfuseToolSpan  (tool name, input, output, error)
│    Loki      → LogQL {app="kubeintellect"} |= "session_id"
│               → LokiLogLine per entry (level, message)
│               → extracts kubectl commands, HITL events from log text
│    Prometheus → p95 latency + error_rate over the run window
│
├─ 4. SIGNAL SCORING  (scorers/signal_scorer.py)
│    completion      (0/1)   — got non-empty final_text, no error event
│    tool_correctness (0-1)  — overlap with expected_tools (if set)
│    error_free       (0-1)  — degrades per error: stream + Langfuse + Loki
│    hitl_discipline  (0/1)  — unexpected HITL on read-only query = 0.5
│    latency          (0-1)  — 1 - (actual_ms / category_baseline_ms)
│    weighted aggregate:
│      completion 35% + tool_correctness 25% + error_free 20%
│                      + hitl_discipline 10% + latency 10%
│
├─ 5. ISSUE DETECTION  (scorers/issue_detector.py — runs every scenario)
│    TOKEN_EXPLOSION     — prompt grew >400 tokens between LLM calls
│    MANY_LLM_CALLS      — >3 LLM round-trips (coordinator looping?)
│    HIGH_LATENCY        — total >30s
│    LOKI_NOT_USED       — query mentions logs but query_loki never called
│    PROMETHEUS_NOT_USED — query mentions metrics but query_prometheus never called
│    OUTPUT_TRUNCATED    — kubectl output was cut off in a tool span
│    SILENT_TOOL_ERROR   — Langfuse shows errors but final answer looks clean
│
├─ 6. LLM JUDGE  (judge.py — only if --judge AND expected.md exists)
│    8 dimensions × 5 points = 40 total, pass ≥ 28
│    problem_understanding, reasoning_path, root_cause_accuracy,
│    tool_selection, output_clarity, action_safety,
│    recovery, routing_efficiency
│    calls Anthropic (preferred) or Azure OpenAI as judge
│
├─ 7. SAVE RECORD
│    EvalRecord → <id>.json (full data)
│    row → results.csv
│
└─ 8. FAULT CLEANUP
     kubectl delete -f setup.yaml --ignore-not-found
```

After all scenarios:

```
9. LESSONS LEARNED  (lessons.py)
   cross-run patterns surfaced in the report:
   - unexpected HITL triggers (read-only queries that asked for write approval)
   - tool usage mismatches (wrong tools called vs. expected)
   - error patterns (API errors, tool errors, Langfuse errors, Loki ERROR lines)
   - latency outliers (which LLM call was the bottleneck)
   - trace-level issues (aggregated by issue ID across all scenarios)
   - all signal score notes

10. MARKDOWN REPORT  (report.py)
    results table + aggregate stats + lessons + per-query detail
    saved to runs/<tag>_<ts>/report_*.md
```

---

## HITL handling

There are two modes:

**During evaluation runs** — `auto_approve: true` is sent in the request body. The API (`ChatCompletionRequest.auto_approve`) bypasses all HITL gates internally before they reach the SSE stream. No human input needed. The client still records `had_hitl=True` if a `hitl_required` frame somehow arrives, so unexpected HITL is not silently lost — it becomes a `hitl_discipline` score penalty in signal scoring.

**During manual investigation** — `run_query.sh` calls `kq` CLI directly (no `auto_approve`). Real HITL prompts appear in the terminal. You answer them interactively, then the trace is analyzed automatically after the query finishes.

---

## Ad-hoc investigation path

```
run_query.sh "why is pod X crashing?"
  1. kq CLI → live streaming output in terminal (HITL prompts are interactive)
  2. sleep 4s (Langfuse trace ingestion)
  3. python -m evaluation.analyze_trace --latest
       → deep-analyzes raw Langfuse API data (9 detectors incl. NO_PROMPT_CACHING,
         EMPTY_MEMORY_CONTEXT, HIGH_COST — not available from typed models)
       → appends structured entry to evaluation/findings/investigation-findings.md
```

`analyze_trace.py` hits the raw Langfuse API so it can detect cost, prompt-caching absence, and memory context issues that are not captured in the typed `LangfuseTrace` model. The two paths are complementary — the pipeline runs on every scenario automatically; `analyze_trace.py` is for deeper post-hoc inspection.

---

## Data flow summary

```
Scenario dir
    → fault injection (kubectl)
    → KubeIntellect API (SSE stream)  →  EvalResult
    → Langfuse REST                   →  LangfuseTrace
    → Loki LogQL                      →  LokiLogs
    → Prometheus range query          →  PrometheusMetrics
    → signal_scorer                   →  SignalScores
    → issue_detector                  →  list[TraceIssue]
    → LLM judge (optional)            →  dict (8 dim scores)
    → EvalRecord  →  .json + .csv row
    → lessons.py  →  cross-run analysis
    → report.py   →  report_*.md
```

---

## Quick reference — runner commands

```bash
# Run all scenarios (signal scoring only)
uv run python -m evaluation.runner run

# Run specific scenarios with LLM judge
uv run python -m evaluation.runner run --scenario 01 --scenario 06 --judge

# Run only scenarios that have expected.md (scored ones)
uv run python -m evaluation.runner run --scored --judge

# Bulk CSV testing (any CSV with a 'query' column)
uv run python -m evaluation.runner csv my_queries.csv

# Concurrent load test
uv run python -m evaluation.runner stress --concurrency 10 --requests 50

# Ad-hoc one-shot investigation
./evaluation/run_query.sh "why is pod X crashing?"

# Deep-analyze last N traces manually
uv run python -m evaluation.analyze_trace --latest
uv run python -m evaluation.analyze_trace --count 5
uv run python -m evaluation.analyze_trace --trace-id <id>
```
