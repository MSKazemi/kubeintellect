# KubeIntellect Evaluation — mt_noregress_20260430_091057

## Results

| ID | Title | Score | Resolved | Asked? | ✓ | Tools | Errors | HITL | Latency (ms) | Verdict |
|----|-------|-------|----------|--------|---|-------|--------|------|-------------|---------|
| 01-crashloop | 01-crashloop | 0.96 | ✅ | — | ✅ | 1.00 | ✅ | — | 20455 | _(pending)_ |

## Aggregate

- **Average score:** 0.96
- **Completion rate:** 100%
- **Average latency:** 20455ms
- **Queries with HITL:** 0
- **Queries with errors:** 0
- **Cluster resolved:** 1/1 scenarios with verify.sh
- **Langfuse available:** 1/1
- **Loki available:** 1/1
- **Stable subset (≥0.90):** 1/1 (100%)

# Lessons Learned

## Run Summary

| Metric | Value |
|--------|-------|
| Total queries | 1 |
| Completed | 1 (100%) |
| Average score | 0.96 |
| Average latency | 20455ms |
| **Cluster resolved** | **1/1 (100%)** |
| Error queries | 0 |
| Unexpected HITL | 0 |
| Wrong tools | 0 |
| Slow (latency<0.5) | 1 |

## Latency Outliers

### 01-crashloop: 01-crashloop
- Total latency: 20455ms
- Status phases: ['loading', 'snapshot', 'analyzing']
- Slowest LLM call: `AzureChatOpenAI` — 7ms (6764+769 tokens)

## Trace-Level Issues

Auto-detected by issue_detector after each scenario. HIGH issues require immediate investigation.

### 🟡 LOKI_NOT_USED (MEDIUM) — 1 occurrence(s)

Query mentions logs but query_loki was not called. kubectl logs misses historical data.

Affected scenarios:
- `01-crashloop` — 01-crashloop

### 🟡 MANY_LLM_CALLS (MEDIUM) — 1 occurrence(s)

5 LLM calls in one query. Coordinator may be looping.

Affected scenarios:
- `01-crashloop` — 01-crashloop

### 🔴 TOKEN_EXPLOSION (HIGH) — 1 occurrence(s)

Prompt grew by 2561 tokens across LLM calls (5233 → 7794). Tool output fed raw into next prompt without summarization.

Affected scenarios:
- `01-crashloop` — 01-crashloop

## Per-Query Detail

### 01-crashloop: 01-crashloop

- **Session:** `eval-20260430_091057-01-crashloop`
- **Latency:** 20455ms
- **Tools called:** ['run_kubectl', 'run_kubectl', 'run_kubectl', 'run_kubectl', 'run_kubectl', 'run_kubectl', 'run_kubectl']
- **Status phases:** ['loading', 'snapshot', 'analyzing']
- **Tokens:** 36188 | LLM calls: 5 | LLM time: 18ms
- **Logs:** 0 lines | errors: 0 | warnings: 0
- **Cluster resolved:** ✅ yes
- **Verdict:** _(pending manual review)_

<details><summary>Answer preview</summary>

INVESTIGATION_PLAN:
1. Describe the crashing pod `faulty-app-86c567555-pg9qj` in namespace `scenario-test` to inspect its spec, including `command`, `args`, and container details.
2. Check events in the `scenario-test` namespace for any warnings or errors related to the pod.
3. Verify the services and endpoints in the `scenario-test` namespace to ensure proper connectivity and label matching.
4. If the pod's spec indicates a hardcoded failure (e.g., `exit 1` or `false`), prepare a corrected depl ...

</details>
