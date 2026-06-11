---
description: >-
  The KubeIntellect evaluation harness — 62 reproducible scenarios across six
  categories, fault injection, scoring, LLM-judging, on-cluster fix verification,
  and version comparison.
---

# Evaluation Harness

KubeIntellect ships a full evaluation framework (in `evaluation/`) so you can
measure how well it actually diagnoses and fixes problems — not just whether it
responds. It injects real faults into a cluster, asks KubeIntellect to handle
them, and scores the result on multiple axes, including whether the cluster was
**actually fixed**.

This is how regressions are caught and how versions are compared.

---

## What's in the suite

**62 scenarios** under `evaluation/scenarios/<id>/`, across six categories:

| Category | Scenarios | Examples |
|---|---|---|
| Debugging | 27 | CrashLoopBackOff, OOMKilled, ImagePullBackOff, stuck rollouts |
| Observability | 15 | "what errored last night", latency/throughput questions |
| Deployment | 5 | rollout, image, config questions |
| Optimization | 5 | right-sizing, resource limits |
| Maintenance | 5 | cleanup, drains, node operations |
| Security | 5 | privileged pods, missing limits, exposure |

Each scenario directory contains:

| File | Purpose |
|---|---|
| `query.md` | The natural-language question put to KubeIntellect. |
| `metadata.yaml` | Category, tags, difficulty. |
| `setup.yaml` *(optional)* | The fault to inject before the run. |
| `expected.md` | The gold answer the LLM judge grades against. |
| `pre_run.sh` / `post_run.sh` | Setup and teardown hooks. |
| `verify.sh` | Checks the cluster *after* the run — did the fix actually land? |

---

## How a run scores

For each scenario the harness runs this pipeline:

1. **Inject** the fault (`setup.yaml` / `pre_run.sh`).
2. **Query** KubeIntellect over the streaming API with `auto_approve: true`.
3. **Collect telemetry** from Langfuse (tokens, latency), Loki, and Prometheus.
4. **Signal score** — a weighted blend of completion, tool usage, error rate,
   HITL behavior, and latency.
5. **Issue detectors** — seven heuristics flag common failure modes.
6. **LLM judge** *(optional)* — an 8-dimension rubric scored out of 40; **pass ≥
   28**.
7. **Verify** — `verify.sh` checks the live cluster and sets `cluster_resolved`
   (did KubeIntellect's fix actually work?).
8. **Record** — an `EvalRecord` JSON per scenario plus an aggregate `results.csv`.

!!! tip "The metric that matters"
    `cluster_resolved` rate — the share of scenarios where the cluster was
    genuinely repaired — is the recommended headline metric to track over time.
    Answer quality without a working fix doesn't count.

---

## Running it

You need a target server (a running KubeIntellect) and a test cluster.

```bash
# Run the whole suite
uv run python -m evaluation.runner run

# A single scenario
uv run python -m evaluation.runner run --scenario 07

# Specific categories, with the LLM judge and full scoring
uv run python -m evaluation.runner run --categories debugging,security --judge --scored

# Point at a specific deployment
uv run python -m evaluation.runner run --target http://localhost:8000
```

Other modes:

```bash
# Export results to CSV
uv run python -m evaluation.runner csv results.csv

# Load / concurrency test
uv run python -m evaluation.runner stress --concurrency 8 --requests 100

# Ad-hoc single query with trace analysis
evaluation/run_query.sh "why is the checkout pod crashing?"
```

### Configuration

Set these before running:

| Variable | Purpose |
|---|---|
| `KUBEINTELLECT_URL`, `KUBEINTELLECT_API_KEY` | Target server + key. |
| `LANGFUSE_*`, `LOKI_URL`, `PROMETHEUS_URL` | Telemetry sources for scoring. |
| `ANTHROPIC_API_KEY` *(preferred)* or `AZURE_OPENAI_API_KEY` | Model for the LLM judge. |

---

## Comparing versions

`evaluation/compare.py` runs the suite against multiple targets (e.g. `v1` vs
`v2`) and produces a statistical comparison — paired t-tests plus matplotlib
figures — so improvements (or regressions) are quantified, not guessed.

```bash
uv run python evaluation/compare.py
```

---

## Related

- [What you can ask](capabilities.md) — the capabilities these scenarios exercise.
- [Agent Behaviors](agent-behaviors.md) — what the harness is measuring.
- [Architecture](architecture.md) — the system under test.
