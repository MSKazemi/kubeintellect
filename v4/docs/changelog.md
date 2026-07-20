---
description: >-
  KubeIntellect release history and the five-generation architecture lineage
  (v1 → v5), with what changed in each and where to read more.
---

# Changelog & Release History

KubeIntellect is one product across **five generations** that share a single local
infrastructure stack. Each generation is a self-contained re-architecture of the same
system, and together they trace one design lineage from a capability-maximal
multi-agent system to a lean, measurable operator and — in v5 — a design-first program
for the next generation. **v4 is the current implementation.**

This page summarizes that lineage and the notable, user-facing changes. It is a
distillation; the authoritative source is the repo-wide `CHANGELOG.md`, and the
reasoning-engine differences between generations are covered in
[V2 vs V4](v2-vs-v4-models.md).

---

## The generation lineage (v1 → v5)

| Version | What it is (as-built) | Status |
|---|---|---|
| **v1** | **Capability-maximal.** A LangGraph Supervisor + 13 specialised ReAct agents, parallel diagnostics, a runtime code-generator tool synthesis path, ~100+ Kubernetes-client tools, a 4-tier PostgreSQL memory, multiple LLM providers, and a UI + `kube-q` CLI + MCP server. | Frozen legacy |
| **v2** | **Simplify (lean).** A single LangGraph coordinator ReAct loop over **4 guarded tools** (`run_kubectl`, `run_helm`, `query_prometheus`, `query_loki`), a kubectl safety guard, on-demand 4-subagent parallel RCA, a pre-fetch snapshot, reflexion + 18 YAML playbooks, and an evaluation harness. | Baseline |
| **v3** | **Reframe (framework spike).** The v2 behaviour delegated to the `deepagents` framework — coordinator + sub-agents over a virtual filesystem, `write_todos` planning, and `task()` delegation. | Experimental |
| **v4** | **Platform / operator.** The v2 system **plus** feature-flagged V4 layers: sensorium + detector engine, memory hierarchy (L1 episodes + L2 temporal KG), flight recorder, autonomy ladder + watchtower, the opt-in cortex, and predictive detection. Delivered as a uv monorepo (`kubeintellect-server`, `kube-q`, `ki-protocol`). | **Current implementation** |
| **v5** | **Design tier.** A design-first program for the next generation (research corpus, ADRs 101–105, architecture docs, P0 specs). v5 ships **no standalone code** — its slices live as additive, default-off flags inside the v4 server. | Design study |

Lineage: **v1** (capability-maximal) → *simplify* → **v2** (lean, measurable) →
*reframe* → **v3** (framework-delegated) → *productionise* → **v4** (platform) →
*design forward* → **v5** (design study).

!!! note "Flags, not forks"
    v4 and v5 are not separate codebases. Everything risky ships **default-off**, and
    you enable it one flag at a time. With every `KI_V5_*` flag unset and
    `CORTEX_V5_ENABLED=false`, the running server is byte-identical to V4. See the
    [Upgrade & Feature-Flag Guide](upgrade.md) for how to turn layers on safely.

---

## Release history

The repo-wide changelog follows [Keep a Changelog](https://keepachangelog.com/). It
tracks shared infrastructure and the **active development line** — the v4 platform and
the v5 design tier. Frozen generations keep their own history: v3 has its own
changelog, and v1 / v2 are versioned by their git tags (`v1.0`, `v2.0.x`). The most
relevant user-facing entries are summarized below.

### Unreleased

- **Documentation drift guard.** A `check_doc_claims.py` script reads the canonical
  numbers straight from code — 18 shipped playbooks, 16 baseline compiled detectors,
  the valid LLM-provider set, and the count of `KI_V5_*` flags — and asserts every
  numbered claim in the docs still matches, failing on drift. Wired as `make
  docs-check`.
- **Documentation standardized across all five versions (v1–v5).** A canonical doc
  surface + mkdocs nav + metadata standard was applied to every version's docs
  as-built, with verified-against-code accuracy fixes (18 playbooks, the
  `openai | azure | qwen | anthropic` provider set, 4 role tiers, 18 failure
  detectors). The root README now documents the full v1 → v5 lineage.
- **Architecture reference extended to five generations.** The architecture-comparison
  reference (Markdown and LaTeX/PDF) gained code-grounded V4 (platform) and V5
  (design-tier) chapters, a V4 architecture diagram, and updated lineage / comparison
  tables.
- **v5 trust plane, anticipation, and fleet slices (additive, all default-off).**
  Layered on the v4 server as `KI_V5_*`-gated slices: a blast-radius / spend budget
  gate (kill switch, change freeze, spend cap), a statistical promotion engine, a
  mutating-verb chokepoint with rollback, server-side dry-run, transactional
  apply → verify → auto-rollback, a misconfig auto-repair + fix-PR generator,
  predictive pre-capture, and cross-cluster fleet memory with strict tenant
  isolation. Every slice is inert unless its flag is set (flags off ⇒ byte-identical
  V4).

### 2.1.0 — 2026-07-05

- **Version identity surface.** `GET /healthz` now returns three version axes — `arm`
  (the generation), `version` (the package SemVer that distinguishes v4 / v4.1 /
  v4.2), and the active `experimental_flags` — so a running instance is fully
  identifiable. Server SemVer moved **2.0.2 → 2.1.0**.
- **Additional LLM provider — Qwen.** `LLM_PROVIDER=qwen` became a first-class
  provider (Alibaba DashScope OpenAI-compatible endpoint, `qwen-max` coordinator +
  `qwen-plus` subagents), joining OpenAI, Azure, and Anthropic.
- **Operator-preference memory.** KubeIntellect learns how *you* like to operate —
  explicit rules you set plus behaviour-inferred ones (e.g. your default namespace) —
  and injects them into future sessions. New `kq preference set/list/forget` CLI and
  `/v1/preferences` API.
- **Memory hierarchy upgrade (default-off flags).** Additive slices ship inside V4:
  hybrid recall (trigram + full-text via Reciprocal Rank Fusion), a bi-temporal
  knowledge graph, multi-hop blast-radius (Personalized PageRank), write
  reconciliation, episode → rule → detector-candidate promotion, importance/surprise-
  weighted retention, prospective "did-the-fix-hold?" re-checks, a security-hardened
  write path with a tamper-evident hash chain, and a RAPTOR-style summary tree. All
  default off; memory failures never break a user response.
- **Three new V4 operator capabilities (flag-gated).** Anticipatory / predictive
  detection (`PREDICTIVE_DETECTION_ENABLED`) that fires a `predicted` finding before a
  slow-burn failure manifests (capped at A1, never auto-fix); grounded incident
  postmortems (`kq postmortem`, `GET /v1/episodes/{id}/postmortem`) over the
  hash-chained flight recorder; and natural-language detector authoring
  (`NL_DETECTOR_AUTHORING_ENABLED`, `kq detector`) that compiles a plain-English
  failure into a shadow detector a human then promotes.
- **Multi-cloud deployment.** First-class Helm overlays and runbooks for AWS EKS and
  GCP GKE were added alongside the existing Azure AKS and Alibaba paths, with the LLM
  provider decoupled from the cloud.
- **Relicensed v4 to AGPL-3.0-or-later** (dual-licensed; a separate commercial license
  is available).

For the complete, unabridged entries — including internal infrastructure and
evaluation changes — see the repo-wide `CHANGELOG.md`.

---

## Where to go next

- Moving between generations or enabling a default-off layer? →
  [Upgrade & Feature-Flag Guide](upgrade.md).
- Want the reasoning-engine differences between V2 and the V4 cortex, and how model
  tiers are assigned? → [V2 vs V4](v2-vs-v4-models.md).

---

## Related

- [V2 vs V4](v2-vs-v4-models.md) — the two reasoning graphs and the V4 model tiers.
- [Upgrade & Feature-Flag Guide](upgrade.md) — how to move between generations and turn flags on safely.
- [What you can ask](capabilities.md) — the current V4 capability catalog.
- [Autonomous Operations](autonomy.md) — the watchtower and the A0–A3 autonomy ladder.
