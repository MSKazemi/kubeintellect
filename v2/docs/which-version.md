---
description: >-
  How KubeIntellect's four architecture generations (v1–v4) compare — a
  side-by-side table, an evolution timeline, and video walkthroughs of the
  current v4 platform.
---
# Which version do I want?

You're reading the **v2 docs** — the lean baseline. This repository actually holds **four
generations** of KubeIntellect side by side. They are not a version history where the newest
replaces the last one; each is a self-contained re-architecture, kept on purpose because **the
differences between them are the research result**.

!!! tip "Just want the diagnosis-and-approval workflow, today? v2 (this docs site) is a fully working, minimal way to do that."

!!! note "Want the current, actively-developed platform — memory, autonomy, predictive detection? That's v4."

    v4 is where feature work happens now. It isn't published under this docs site yet; see the
    [GitHub repo](https://github.com/MSKazemi/kubeintellect/tree/main/v4) for its own docs.

## The four generations, at a glance

<div class="ki-compare" markdown>

| | v1 | **v2 (this site)** | v3 | v4 |
|---|---|---|---|---|
| What it is | Capability-maximal origin | **Lean baseline** | Framework-delegated | Platform |
| Agents | Supervisor + 13 specialised ReAct agents | 1 coordinator, on-demand 4-subagent RCA | Coordinator + sub-agents via [`deepagents`](https://github.com/langchain-ai/deepagents) | Lean coordinator + feature-flagged layers |
| Tools | ~100+ runtime-synthesised tools | 4 guarded tools (`run_kubectl`, `run_helm`, `query_prometheus`, `query_loki`) | Same 4-tool core, virtual-filesystem planning | Same core + sensorium, memory, flight recorder |
| Safety gate | Per-agent checks | 7-layer kubectl safety guard | Inherits v2's guard | Inherits v2's guard + RBAC tiers |
| Status | ❄️ Frozen — the published architecture | 🔸 Baseline — bug fixes and docs only | 🔸 Experimental — bug fixes and docs only | ✅ Current — all feature work |
| Cited by | JGC 2026 paper | Fair-comparison campaign | — | — |

</div>

**Lineage:** v1 (capability-maximal) → *simplify* → **v2** (lean, measurable) → *reframe* → v3
(framework-delegated) → *productionize* → v4 (platform).

<div class="ki-timeline" markdown>

<div class="ki-tl-step" markdown>
<div class="ki-tl-dot">v1</div>
<div class="ki-tl-status">Frozen</div>
<div class="ki-tl-title">Capability-maximal</div>
<div class="ki-tl-desc">13 specialised ReAct agents, ~100+ runtime-synthesised tools. The architecture the paper describes.</div>
</div>

<div class="ki-tl-step ki-tl-current" markdown>
<div class="ki-tl-dot">v2</div>
<div class="ki-tl-status">You are here</div>
<div class="ki-tl-title">Lean baseline</div>
<div class="ki-tl-desc">One coordinator, 4 guarded tools, a 7-layer kubectl safety guard. Simpler — and the finding was that simpler also scored better.</div>
</div>

<div class="ki-tl-step" markdown>
<div class="ki-tl-dot">v3</div>
<div class="ki-tl-status">Experimental</div>
<div class="ki-tl-title">Framework-delegated</div>
<div class="ki-tl-desc">v2's behaviour re-expressed through <code>deepagents</code> — a virtual filesystem plus task delegation.</div>
</div>

<div class="ki-tl-step" markdown>
<div class="ki-tl-dot">v4</div>
<div class="ki-tl-status">Current</div>
<div class="ki-tl-title">Platform</div>
<div class="ki-tl-desc">The lean coordinator plus feature-flagged layers: sensorium, memory hierarchy, flight recorder, autonomy ladder.</div>
</div>

</div>

## Why keep all four instead of just shipping the latest?

Because the finding *is* the comparison. The interesting result of this project was that the
13-agent, ~100-tool system (v1) was **worse** than a lean coordinator (v2) — on quality *and* on
cost. Deleting v1 would delete the evidence for that claim. Each version has its own `Makefile`,
`docs/`, `tests/`, and packaging; they are independent snapshots of a design lineage, not
duplicated debt.

## See v4 — the current platform — in action

The two videos below were recorded against **v4**, not against this v2 baseline. v4 adds a
memory hierarchy, an autonomy ladder, and a sensorium/detector engine that v2 does not have — so
treat these as "where the project is headed," not as a demo of what you get by installing v2.

<div class="ki-video" data-video-id="je-K_w3vgGY" data-video-title="KubeIntellect v4 — an AI SRE for Kubernetes that asks before it acts" role="button" tabindex="0" aria-label="Play: KubeIntellect v4 full demo">
  <img src="https://img.youtube.com/vi/je-K_w3vgGY/maxresdefault.jpg" alt="KubeIntellect v4 demo thumbnail" loading="lazy" />
  <span class="ki-video-play"></span>
  <span class="ki-video-caption"><span>KubeIntellect v4 — full demo, 8 min</span><span class="ki-badge">v4</span></span>
</div>

<div class="ki-video" data-video-id="lgmalgXmDfg" data-video-title="KubeIntellect v4 architecture, animated" role="button" tabindex="0" aria-label="Play: KubeIntellect v4 architecture walkthrough">
  <img src="https://img.youtube.com/vi/lgmalgXmDfg/maxresdefault.jpg" alt="KubeIntellect v4 architecture animation thumbnail" loading="lazy" />
  <span class="ki-video-play"></span>
  <span class="ki-video-caption"><span>v4 architecture, animated — 38s</span><span class="ki-badge">v4</span></span>
</div>

## Do not mix them

Each version has its own `Makefile`, `docs/`, `tests/`, and `.env.example`. What they *share* is
the infrastructure underneath — one Kind cluster, one observability stack (Prometheus + Grafana +
Loki) — managed from the repository root.

## Where to go next

- **Run v2** → [Quickstart](quickstart.md) · [Architecture](architecture.md)
- **Compare against other tools** → [How it compares](comparison.md)
- **Read the v1 paper** → [GitHub: v1](https://github.com/MSKazemi/kubeintellect/tree/main/v1)
- **Read v4's own docs** → [GitHub: v4](https://github.com/MSKazemi/kubeintellect/tree/main/v4)
