<div align="center">
  <img src="v4/docs/assets/brand/ki-c-indigo.svg" alt="KubeIntellect" width="96" height="96" />
  <h1>KubeIntellect</h1>
  <p><strong>Human-governed AI SRE for Kubernetes</strong> — chat with your cluster in plain English.</p>

  [![CI](https://github.com/MSKazemi/kubeintellect/actions/workflows/ci.yml/badge.svg)](https://github.com/MSKazemi/kubeintellect/actions/workflows/ci.yml)
  [![PyPI](https://img.shields.io/pypi/v/kubeintellect.svg)](https://pypi.org/project/kubeintellect/)
  [![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
  [![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
  [![DOI](https://img.shields.io/badge/DOI-10.1007%2Fs10723--026--09837--6-blue)](https://doi.org/10.1007/s10723-026-09837-6)
  [![arXiv](https://img.shields.io/badge/arXiv-2509.02449-b31b1b.svg)](https://arxiv.org/abs/2509.02449)
  [![Website](https://img.shields.io/badge/website-kubeintellect.com-0075C4)](https://kubeintellect.com/)
  [![GitHub Stars](https://img.shields.io/github/stars/MSKazemi/kubeintellect?style=social)](https://github.com/MSKazemi/kubeintellect)

  **[Website](https://kubeintellect.com/)** · **[Live Demo](https://kubeintellect.com/demo)** · **[Current version → `v4/`](v4/)** · **[Paper](https://doi.org/10.1007/s10723-026-09837-6)**

  Created & maintained by **[Mohsen Seyedkazemi Ardebili](https://github.com/MSKazemi)**

  <br/>

  <img src=".github/assets/kubeintellect-demo.gif" alt="KubeIntellect diagnosing a CrashLoopBackOff, then pausing for approval before scaling" width="880" />

  <sub>Ask why a pod is broken → get the root cause. Ask it to <em>change</em> something → it stops and waits for you.<br/>Scripted demo, time-compressed; panels are the real <code>kq</code> UI.</sub>
</div>

---

KubeIntellect is an open-source, LLM-orchestrated multi-agent framework for **autonomous Kubernetes operations**. Ask a question in plain English — it fans out to specialized agents that query **kubectl**, **Prometheus** (PromQL), and **Loki** (LogQL) live, correlates the evidence, and answers. Any change to the cluster pauses for **explicit human-in-the-loop approval** with role-based access control.

```bash
kq "why is my api-server pod crashlooping?"
kq "show me pods with high restart counts in the default namespace"
kq "scale the frontend deployment to 5 replicas"   # pauses for your approval
```

> **Safe by default** — read-only queries run immediately; scale, delete, and restart operations require explicit approval before anything executes.

## What is KubeIntellect?

- **KubeIntellect is an open-source framework for conversational, natural-language Kubernetes operations** — root-cause analysis, live diagnostics, and human-approved cluster actions.
- **It is for** platform engineers, DevOps engineers, SREs, and Kubernetes operators who want to troubleshoot and manage clusters in plain English.
- **It helps you diagnose incidents faster** by correlating kubectl state, Prometheus metrics, and Loki logs through a coordinator that delegates to pod, metrics, logs, and events subagents.
- **Use KubeIntellect when** you want conversational operations with a hard safety gate on every destructive action.
- **It is different from read-only AI diagnostics tools** because it can also *act* — scale, restart, delete — but only after explicit human approval, gated by RBAC (admin / operator / readonly).
- **It is not** a replacement for your observability stack, a GitOps/CD pipeline, or on-call judgment; it queries the tools you already run and pauses before changing anything. It requires an OpenAI or Azure OpenAI API key and Python 3.12+.

## Quick start

**Try it in your browser — zero install.** Open **[kubeintellect.com/demo](https://kubeintellect.com/demo)** (read-only, shared demo cluster).

**Install the CLI (read-only, one `pip install`):**

```bash
pip install kube-q
kq --api-key ki-ro-dev            # kq defaults to https://api.kubeintellect.com
```

**Run the full system on a local cluster** (Docker is the only prerequisite):

```bash
pip install kubeintellect
kubeintellect init                # wizard: creates a Kind cluster, deploys samples, configures kq
```

Full install paths (browser, CLI-only, local Kind, Docker Compose, existing cluster) are in the **[v4 README](v4/README.md)** and **[v4 docs](v4/docs/)**.

## This repository

This repo holds **multiple generations** of KubeIntellect. Each generation is a self-contained re-architecture of the same product — together they trace one design lineage from a capability-maximal multi-agent system to a lean, measurable, human-governed operator.

| Version | What it is | Status |
|---|---|---|
| **[`v4/`](v4/)** | **Platform (recommended).** The lean coordinator plus feature-flagged layers: sensorium + detector engine, memory hierarchy (episodes + temporal knowledge graph), flight recorder, autonomy ladder, and predictive detection. Shipped as a `uv` monorepo (`kubeintellect-server`, `kube-q`, `ki-protocol`). | **Current** |
| **[`v2/`](v2/)** | **Lean baseline.** A single LangGraph coordinator ReAct loop over 4 guarded tools (`run_kubectl`, `run_helm`, `query_prometheus`, `query_loki`) with a 7-layer kubectl safety guard, on-demand 4-subagent RCA, and an evaluation harness. | Baseline |
| **[`v3/`](v3/)** | **Framework-delegated.** The v2 behavior expressed through the [`deepagents`](https://github.com/langchain-ai/deepagents) framework — coordinator + sub-agents over a virtual filesystem with planning and task delegation. | Experimental |
| **[`v1/`](v1/)** | **Capability-maximal origin.** LangGraph supervisor + 13 specialized ReAct agents, runtime tool synthesis, ~100+ Kubernetes tools, multi-provider LLM support. The original published architecture. | Legacy |

**New here? Start with [`v4/`](v4/)** — it's the current implementation. `v1/` is the architecture described in the published paper (see [Citation](#citation)).

> **Lineage:** v1 (capability-maximal) → *simplify* → v2 (lean, measurable) → *reframe* → v3 (framework-delegated) → *productionize* → v4 (platform).

## Repository layout

Responsibilities are split between the repo root and each version directory:

- **Root — `Makefile` + `deploy/` + `scripts/`** manages the *shared infrastructure* every version runs against: one Kind cluster, one observability stack (Prometheus + Grafana + Loki), and one Langfuse instance with a shared project. Run `make help` at the root to list the infra targets.
- **Per-version — `v4/Makefile`, etc.** each version directory has its own Makefile for *application* build/deploy and Python development, plus its own `docs/`, `tests/`, and packaging.

## Shared infrastructure

All versions run against one shared infrastructure stack rather than each standing up its own:

- **One Kind cluster** — `make kind-cluster-create`
- **One observability stack** (Prometheus + Grafana + Loki) — `make monitoring-install`
- **One Langfuse instance + shared project** — `make langfuse-provision`, then `make langfuse-install`
- **Local hosts entry** — `make hosts-entry`

`make langfuse-provision` auto-creates a shared Langfuse project and token and fans the keys into each version's `.env` (no manual UI step). All versions share **one** Langfuse project; per-version cost is filtered by a `version:vN` trace tag.

### Quick start (laptop + Kind)

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

## Documentation

- **[Website](https://kubeintellect.com/)** — overview, live demo, and hosted API.
- **[v4 docs](v4/docs/)** — install, quickstart, configuration, architecture, security, CLI reference, and troubleshooting for the current version.
- **[v4 README](v4/README.md)** — every install path in detail.
- Each version directory (`v1/`–`v4/`) ships its own `README.md` and `docs/`.

## Use cases

- **Incident response** — "why is this pod crashlooping?" correlates events, logs, and metrics into a root-cause answer.
- **Interactive diagnostics** — explore cluster state, restart counts, resource pressure, and PromQL/LogQL results conversationally.
- **Guarded operations** — scale, restart, and delete with an explicit approval gate and RBAC, so an LLM never acts unilaterally.
- **Learning & practice** — the `init` wizard can deploy broken-pod RCA scenarios to practice against.

## How it compares

| | KubeIntellect | Read-only AI diagnostics (e.g. k8sgpt) | Raw `kubectl` + dashboards |
|---|---|---|---|
| Natural-language Q&A | ✅ | ✅ | ❌ |
| Correlates kubectl + Prometheus + Loki | ✅ | Partial | Manual |
| Performs cluster actions | ✅ (approval-gated) | ❌ | ✅ (unguarded) |
| Human-in-the-loop safety gate + RBAC | ✅ | n/a | ❌ |
| Works with no LLM API key | ❌ | Varies (local models supported) | ✅ |
| Runs fully offline / air-gapped | Partial (self-hosted models only) | Varies | ✅ |
| Per-query cost | LLM tokens | LLM tokens | Free |
| Project maturity & community size | Young — small community | **Larger, more adopted** | Universal |

Where the alternatives win is stated on purpose: if you only need read-only
triage, or you cannot send cluster data to a hosted model, a read-only tool or
plain `kubectl` may be the better fit. KubeIntellect earns its cost when you want
conversational diagnosis *and* guarded action in one loop.

## Limitations

Known and deliberate, so you can judge fit before installing:

- **An LLM API key is required.** OpenAI or Azure OpenAI out of the box; v4 also
  supports Anthropic, Qwen, and OpenAI-compatible endpoints. Queries cost tokens.
- **Cluster context leaves your network** unless you point it at a self-hosted or
  in-cluster model endpoint. Review [SECURITY.md](SECURITY.md) before running it
  against production.
- **It is not a replacement** for your observability stack, a GitOps/CD pipeline,
  or on-call judgment. It queries the tools you already run.
- **LLM answers can be wrong.** The approval gate exists precisely because the
  model's proposed action should be read before it runs. Do not enable
  `--auto-approve` outside testing.
- **Autonomy is capped at A1 by default** — detector firings open investigations;
  automatic remediation (A3) requires an explicit allowlist.
- **Young project.** APIs across `v1/`–`v4/` have changed between generations;
  `v4/` is the supported line and the one to build on.

## FAQ

**Does it change my cluster automatically?** No. Read-only queries run immediately; any mutating action (scale/restart/delete) pauses for explicit human approval, subject to RBAC.

**What LLM providers are supported?** OpenAI and Azure OpenAI out of the box (v4 also supports additional providers via configuration). An API key is required.

**Do I need a cluster to try it?** No — use the [browser demo](https://kubeintellect.com/demo) or the read-only `kube-q` CLI. For full features, `kubeintellect init` creates a local Kind cluster for you.

**Which version should I use?** [`v4/`](v4/) — it's the current, actively developed implementation.

## Maintainer

KubeIntellect is created, led, and maintained by **[Mohsen Seyedkazemi Ardebili](https://github.com/MSKazemi)** — see [GOVERNANCE.md](GOVERNANCE.md). Contributions are welcome from everyone; start with [CONTRIBUTING.md](CONTRIBUTING.md) and a [`good first issue`](https://github.com/MSKazemi/kubeintellect/labels/good%20first%20issue). If KubeIntellect is useful to you, a ⭐ helps others find it.

## License

KubeIntellect is **dual-licensed** under the **[GNU AGPL-3.0-or-later](LICENSE)** *or* a **commercial license**. Self-host and modify freely under the AGPL; for closed/SaaS use without AGPL's network-copyleft obligations, a commercial license is available. See **[LICENSING.md](LICENSING.md)**; contact **mohsen.seyedkazemi@gmail.com**.

## Citation

If you use KubeIntellect in your research, please cite the paper (metadata in [CITATION.cff](CITATION.cff)):

> Seyedkazemi Ardebili, M., & Bartolini, A. (2026). *KubeIntellect: A Modular LLM-Orchestrated Agent Framework for End-to-End Kubernetes Management.* Journal of Grid Computing, 24(3). https://doi.org/10.1007/s10723-026-09837-6
