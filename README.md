# ki

KubeIntellect — an LLM-driven operator for Kubernetes. This repository holds
multiple generations of the system (`v1/`–`v5/`) that share a single local
infrastructure stack. Each generation is a self-contained re-architecture of the
same product; together they trace one design lineage from a capability-maximal
multi-agent system to a lean, measurable operator and, in v5, a design-first
program for the next generation.

## Versions at a glance

| Version | What it is (as-built) | Status |
|---|---|---|
| **`v1/`** | Capability-maximal generation: LangGraph **Supervisor + 13 specialised ReAct agents**, parallel diagnostics, **runtime CodeGenerator** tool synthesis (AST sandbox → SHA-256 → PVC → optional GitHub PR), ~100+ Kubernetes-client tools, 4-tier PostgreSQL memory, 7 LLM providers, LibreChat UI + `kube-q` CLI + MCP server. | Frozen legacy (ADR-001) |
| **`v2/`** | **Simplify.** Single LangGraph **coordinator** ReAct loop over **4 guarded tools** (`run_kubectl`, `run_helm`, `query_prometheus`, `query_loki`) with a 7-layer kubectl safety guard, on-demand 4-subagent parallel RCA, pre-fetch snapshot, reflexion + YAML playbooks, and an evaluation harness. | Baseline `main` |
| **`v3/`** | **Reframe.** The v2 behaviour delegated to the **`deepagents`** framework — coordinator + sub-agents over a virtual filesystem (`/snapshot.md`, `/findings/*`), `write_todos` planning, and `task()` delegation. | Experimental |
| **`v4/`** | **Platform.** The v2 system plus feature-flagged V4 layers: sensorium + detector engine, memory hierarchy (L1 episodes + L2 temporal KG), flight recorder, autonomy ladder + watchtower, cortex, predictive detection. Delivered as a **uv monorepo** (`packages/kubeintellect-server`, `packages/kube-q`, `packages/ki-protocol`). | Current implementation |
| **`v5/`** | **Design tier.** A design-first program for the next generation (research corpus, ADRs 101–105, 7 architecture docs, P0 specs). v5 ships **no standalone code** — its P0 slices live as additive, default-off flags inside the v4 server (ADR-101). | Design study |

Lineage: **v1** (capability-maximal) → *simplify* → **v2** (lean, measurable) →
*reframe* → **v3** (framework-delegated) → *productionise* → **v4** (platform) →
*design forward* → **v5** (design study).

## Repository layout

The repository splits responsibilities between the root and each version directory:

- **Root (`Makefile` + `deploy/` + `scripts/`)** — manages the *shared
  infrastructure* used by every version: one Kind cluster, one observability
  stack (Prometheus + Grafana + Loki), and one Langfuse instance with a shared
  project. Run `make help` at the root to list the infra targets.
- **Per-version (`v4/Makefile`, etc.)** — each version directory has its own
  Makefile for *application* build/deploy and Python development, e.g.
  `cd v4 && make kind-build-kubeintellect && make kind-deploy-kubeintellect`.

Per [ADR-001](design/adr/001-standard-version-layout.md), a version directory
contains only product code, `tests/`, `docs/`, `deploy/`, and packaging files;
the shared **evaluation harness** (`evaluation/`) and **paper** (`paper/`,
`architecture-comparison/`) live only at the repo root and are always private.
Documentation across versions follows one canonical surface
([ADR-002](design/adr/002-standard-doc-surface.md)).

## Shared infrastructure

All versions run against one set of shared infrastructure rather than each
standing up its own:

- **One Kind cluster** — `make kind-cluster-create`
- **One observability stack** (Prometheus + Grafana + Loki) — `make monitoring-install`
- **One Langfuse instance + shared project** — `make langfuse-provision`, then `make langfuse-install`
- **Hosts entry** — `make hosts-entry`

`make langfuse-provision` auto-creates a shared Langfuse project and token, and
fans the keys into each version's `.env` (no manual UI step). All versions share
**one** Langfuse project; per-version cost is filtered by a `version:vN` trace tag.

## Quick start (laptop + Kind)

From the repository root:

```bash
make kind-cluster-create     # one shared Kind cluster
make monitoring-install      # Prometheus + Grafana + Loki
make langfuse-provision      # create shared Langfuse project + token, fan keys into each .env
make langfuse-install        # deploy Langfuse
make hosts-entry             # add local hosts entry
```

Then build and deploy a version's application:

```bash
cd v4
make kind-build-kubeintellect
make kind-deploy-kubeintellect
```

Run `make help` at the root at any time to see the available infra targets.
