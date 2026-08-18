# Which version do I want?

This repository holds **four generations** of KubeIntellect. They are not a version history —
each is a self-contained re-architecture, kept side by side because the *differences between
them are the research result*.

Two answers cover almost everyone:

!!! tip "Running it? Use v4."

    `v4/` is the current platform and the only line that accepts behavioural changes.
    Start at the [Quickstart](quickstart.md).

!!! warning "Read the paper and want the architecture it describes? That is v1, not v4."

    The peer-reviewed paper (*Journal of Grid Computing*, 2026) describes **v1** — the
    capability-maximal supervisor with 13 specialised agents and ~100+ tools. `v4/` is a
    deliberately different system. If you are reproducing or citing the paper, read `v1/`.

## The four generations

| | What it is | Status | Changes accepted |
|---|---|---|---|
| **v4** | **Platform — recommended.** Lean coordinator plus feature-flagged layers: sensorium + detector engine, memory hierarchy (episodes + temporal knowledge graph), flight recorder, autonomy ladder, predictive detection. A `uv` monorepo shipping `kubeintellect-server`, `kube-q`, `ki-protocol`. | Current | ✅ Yes — work happens here |
| **v3** | **Framework-delegated.** v2's behaviour expressed through [`deepagents`](https://github.com/langchain-ai/deepagents) — coordinator plus sub-agents over a virtual filesystem, with planning and task delegation. | Experimental | 🔸 Bug fixes and docs only |
| **v2** | **Lean baseline.** One LangGraph coordinator ReAct loop over 4 guarded tools (`run_kubectl`, `run_helm`, `query_prometheus`, `query_loki`), a 7-layer kubectl safety guard, on-demand 4-subagent RCA, and an evaluation harness. | Baseline | 🔸 Bug fixes and docs only |
| **v1** | **Capability-maximal origin.** LangGraph supervisor + 13 specialised ReAct agents, runtime tool synthesis, ~100+ Kubernetes tools, multi-provider LLM support. **The published architecture.** | Legacy | ❄️ Frozen — typos and docs only |

**Lineage:** v1 (capability-maximal) → *simplify* → v2 (lean, measurable) → *reframe* → v3
(framework-delegated) → *productionize* → v4 (platform).

## Why keep all four?

Because the finding is the comparison. The interesting result of this project was that the
13-agent, ~100-tool system was **worse** than a lean coordinator — on quality *and* on cost.
Deleting v1 and v2 would delete the evidence for the claim that v4 makes.

That is also why the older directories are not refactored to share code with v4. They are
independent snapshots of a design lineage, and two of them are cited by a published paper.
Duplication between version directories is deliberate, not debt.

## Do not mix them

Each version has its own `Makefile`, `docs/`, `tests/` and packaging, and its own
`.env.example`. What they *share* is the infrastructure underneath: one Kind cluster, one
observability stack (Prometheus + Grafana + Loki), one Langfuse project — all managed from the
repository root. See the root `Makefile` (`make help`) for the infra targets.

## Where to go next

- **Run it** → [Quickstart](quickstart.md) · [Install without a cluster](install/no-cluster.md)
- **Understand it** → [How it works](how-it-works.md) · [Architecture](architecture.md)
- **Compare v2 and v4 directly** → [V2 vs V4 (models)](v2-vs-v4-models.md)
- **Cite it** → [Research](research.md)
