<div align="center">
  <img src="v4/docs/assets/brand/ki-c-indigo.svg" alt="KubeIntellect" width="96" height="96" />
  <h1>KubeIntellect</h1>
  <p><strong>Human-governed AI SRE for Kubernetes</strong> — chat with your cluster in plain English.</p>

  [![CI](https://github.com/MSKazemi/kubeintellect/actions/workflows/ci.yml/badge.svg)](https://github.com/MSKazemi/kubeintellect/actions/workflows/ci.yml)
  [![PyPI](https://img.shields.io/pypi/v/kubeintellect.svg)](https://pypi.org/project/kubeintellect/)
  [![kq downloads](https://img.shields.io/pypi/dm/kube-q?label=kq%20installs)](https://pypi.org/project/kube-q/)
  [![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
  [![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
  [![DOI](https://img.shields.io/badge/DOI-10.1007%2Fs10723--026--09837--6-blue)](https://doi.org/10.1007/s10723-026-09837-6)
  [![arXiv](https://img.shields.io/badge/arXiv-2509.02449-b31b1b.svg)](https://arxiv.org/abs/2509.02449)
  [![Website](https://img.shields.io/badge/website-kubeintellect.com-0075C4)](https://kubeintellect.com/)
  [![good first issues](https://img.shields.io/github/issues/MSKazemi/kubeintellect/good%20first%20issue?label=good%20first%20issues&color=7057ff)](https://github.com/MSKazemi/kubeintellect/contribute)
  [![GitHub Stars](https://img.shields.io/github/stars/MSKazemi/kubeintellect?style=social)](https://github.com/MSKazemi/kubeintellect)

  **[Website](https://kubeintellect.com/)** · **[Live Demo](https://kubeintellect.com/demo)** · **[Current version → `v4/`](v4/)** · **[Contributing](#contributing)** · **[Paper](https://doi.org/10.1007/s10723-026-09837-6)**

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
docker run --rm -it ghcr.io/mskazemi/kubeintellect:2.2.0 --help
```

Or install the server from PyPI:

```bash
pip install kubeintellect
kubeintellect init         # setup wizard — writes ~/.kubeintellect/.env
kubeintellect kind-setup   # optional: create a local Kind cluster to try it against
kubeintellect serve        # start the API server on :8000
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
- **[Data handling](v4/docs/data-handling.md)** — what v4 sends to model and telemetry endpoints, stores, and redacts.
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
  in-cluster model endpoint. Review [SECURITY.md](SECURITY.md) and the
  [data-handling notes](v4/docs/data-handling.md) before running it against
  production.
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

## Contributing

**Contributions are wanted, and the barrier is deliberately low.** You do **not** need a
Kubernetes cluster, a Docker daemon, or an LLM API key to contribute — the test suites are
fully mocked. Python 3.12+ is the only prerequisite.

```bash
git clone https://github.com/MSKazemi/kubeintellect.git
cd kubeintellect
make setup     # installs the v4 workspace, then runs the exact gates CI runs (~1 min)
```

`make setup` ends by telling you whether your environment is correct, so you never debug your
setup and your change at the same time. Prefer zero install? Open the repo in a
[**GitHub Codespace**](https://codespaces.new/MSKazemi/kubeintellect) — `.devcontainer/` runs
the same setup for you.

| I want to… | Go here |
|---|---|
| **Find something to work on** | [`/contribute`](https://github.com/MSKazemi/kubeintellect/contribute) — the curated [`good first issue`](https://github.com/MSKazemi/kubeintellect/labels/good%20first%20issue) list, each scoped small on purpose |
| **See a first PR done end to end** | [CONTRIBUTING.md → *Your first PR, start to finish*](CONTRIBUTING.md#your-first-pr-start-to-finish) — a real open issue, every command, nothing skipped |
| **Contribute with an AI coding agent** | [AGENTS.md](AGENTS.md) — the machine-readable version of the rules. AI assistance is [explicitly welcome](CONTRIBUTING.md#ai-assistance); please just disclose it |
| **Ask before writing code** | [Discussions](https://github.com/MSKazemi/kubeintellect/discussions) — questions are never a bother, and an unclear doc is our bug, not yours |
| **Help without writing code** | Docs, a reproducible bug report, triage, testing on a platform we lack, or adding yourself to [ADOPTERS.md](ADOPTERS.md) — all credited equally |

**What you can expect back:** every issue and PR gets a *human* first response — even if the
answer is "not this way", it arrives rather than silence ([TRIAGE.md](TRIAGE.md)). Every merged
contributor is named in the release notes. And one thing that will look broken but isn't —
**on a first-time contributor's fork PR,
GitHub runs no CI until a maintainer approves the run**, so your PR sits with no checks. That is
expected, it is not your mistake, and you do not need to do anything.

## Maintainer

KubeIntellect is created, led, and maintained by **[Mohsen Seyedkazemi Ardebili](https://github.com/MSKazemi)** — see [GOVERNANCE.md](GOVERNANCE.md).

Where the project is going — and what it deliberately **won't** do — is in **[ROADMAP.md](ROADMAP.md)**. It has one maintainer today; the contributor ladder in [GOVERNANCE.md](GOVERNANCE.md) is a real invitation, not a formality, and areas are genuinely available to own.

If KubeIntellect is useful to you, a ⭐ helps other people find it — and [#51](https://github.com/MSKazemi/kubeintellect/issues/51) is where to say what you're using it for.

### Contributors

People other than the maintainer whose work is in the shipped code. The list is short because
the project is young — which is exactly why being on it is worth something.

| | Contributed |
|---|---|
| **[@hariomlohardev](https://github.com/hariomlohardev)** | Removed the executable bit from 94 non-script modules ([#70](https://github.com/MSKazemi/kubeintellect/pull/70)) — mode-only, zero content lines, and it fixed the cause rather than silencing the rule. Also [#57](https://github.com/MSKazemi/kubeintellect/pull/57) and [#65](https://github.com/MSKazemi/kubeintellect/pull/65). Cleared three ruff-0.16 rule families ([#110](https://github.com/MSKazemi/kubeintellect/pull/110)) without touching `UP045`, the one whose autofix would have disabled RBAC and the HITL gate. Wrote the `DeploymentRolloutStuck` playbook ([#112](https://github.com/MSKazemi/kubeintellect/pull/112)) — the first to route to downstream playbooks instead of duplicating them, and the first with an `expected_evidence` entry naming when it does **not** apply. Then took on the whole `kq` cookbook gap in one sitting — worked examples for the eight undocumented subcommands ([#119](https://github.com/MSKazemi/kubeintellect/pull/119)–[#126](https://github.com/MSKazemi/kubeintellect/pull/126)), every transcript real and re-verified byte-for-byte against `kube-q` 1.5.0 — plus the `PvcPending` and `LivenessProbeFailing` playbooks ([#127](https://github.com/MSKazemi/kubeintellect/pull/127), [#128](https://github.com/MSKazemi/kubeintellect/pull/128)), the `.env.example` restore ([#117](https://github.com/MSKazemi/kubeintellect/pull/117)) — where he found the live cause, a `.gitignore` pattern, rather than the one the issue blamed — and the Homebrew style pass ([#118](https://github.com/MSKazemi/kubeintellect/pull/118)). |
| **[@AdvaitVarhade](https://github.com/AdvaitVarhade)** | Fixed the demo UI's `set-state-in-effect` errors ([#73](https://github.com/MSKazemi/kubeintellect/pull/73)) — and corrected the issue itself, which had named the wrong file. Also proposed the `kq export` command and reported the Python 3.13 syntax warnings. |
| **[@shaurya703](https://github.com/shaurya703)** | Repointed the PyPI metadata at the canonical repository and adopted PEP 639 ([#78](https://github.com/MSKazemi/kubeintellect/pull/78)) — noticing that `license-files` resolves per package, which meant two wheels had been shipping **without the AGPL text at all**. Spotted the same defect in `mkdocs.yml`, which led to a 13-file sweep. Also cleared the `I001` import backlog ([#77](https://github.com/MSKazemi/kubeintellect/pull/77)). |
| **[@uuzzrm](https://github.com/uuzzrm)** | Wrote [`v4/docs/data-handling.md`](v4/docs/data-handling.md) ([#105](https://github.com/MSKazemi/kubeintellect/pull/105)) — the page that states what reaches a model provider, what is persisted, and precisely where the redactor does **not** apply. Every claim in it was verified against source. Their honest test-failure report also uncovered [#106](https://github.com/MSKazemi/kubeintellect/issues/106), a real environment-sensitivity bug in our own suite. Also fixed the Homebrew formula ([#111](https://github.com/MSKazemi/kubeintellect/pull/111)) — which declared MIT on AGPL code — and reported plainly that they could not run `brew` rather than passing static checks off as an install test. Later made the memory recall similarity floor configurable ([#116](https://github.com/MSKazemi/kubeintellect/pull/116)) — one setting replacing the same constant hard-coded independently in two recall modules, with tests that pin the default and prove the override reaches both paths. It merged unchanged. |
| **[@floze-the-genius](https://github.com/floze-the-genius)** | Made the `kq` suite independent of the terminal it runs in ([#109](https://github.com/MSKazemi/kubeintellect/pull/109)) — tests used to fail on a narrow or `NO_COLOR` terminal before a contributor changed anything. Pinned the environment in `pytest_configure`, **before** module import, which is what a fixture alone cannot do, and **without weakening a single assertion**. Reported a five-environment before/after matrix in which every number reproduced independently. |
| **[@Priyanshu608](https://github.com/Priyanshu608)** | Wrote the `NetworkPolicyBlocking` playbook ([#108](https://github.com/MSKazemi/kubeintellect/pull/108)) — the 19th, and the only one whose signal is the **absence** of evidence: a policy denial is dropped in the CNI datapath, so no event is ever emitted and the connection simply hangs. Captured that as a positive finding rather than a gap. |
| **[@AshSgDe29071999](https://github.com/AshSgDe29071999)** | Independently diagnosed the terminal-sensitivity bug and submitted the fixture-only fix ([#107](https://github.com/MSKazemi/kubeintellect/pull/107)). It did not merge — #109 arrived against a claimed issue — but running it as a control is the only reason we know the `pytest_configure` hook is load-bearing rather than incidental. The `claimed` label exists because of the collision they hit. |

Every merged contribution is credited by name in [CHANGELOG.md](CHANGELOG.md) and in the
release notes.

**Non-code work is credited as a first-class contribution type.** The project follows the
[All Contributors](https://allcontributors.org/) specification (`.all-contributorsrc`), so
documentation, bug reports, reviews, ideas, triage, and **verifying KubeIntellect on a
Kubernetes platform our CI does not cover** are all recorded — not just merged commits.

That last one is a real, open lane: CI runs on Kind only, so nobody has confirmed the install
path on **k3s, EKS, GKE, AKS or OpenShift**. Those issues need a cluster and about an hour, and
**no Python at all** — see
[`area/deploy`](https://github.com/MSKazemi/kubeintellect/issues?q=is%3Aopen+label%3Aarea%2Fdeploy).
A report saying *"it did not work, here is exactly where it stopped"* is the single most useful
thing the project cannot get any other way.

> **One honest caveat.** GitHub's Contributors graph counts *merged commits* only, so a comment
> or a platform report will not appear there however valuable it is. If you want to be on that
> graph too, the one-line docs PR that comes out of what you found will do it — and is usually
> warranted anyway. See [GOVERNANCE.md](GOVERNANCE.md) for the full ladder.

## Other projects by the same author

- **[YazSes](https://github.com/MSKazemi/yazses)** — offline voice dictation for Linux, macOS
  and Windows. Hold a key, speak, release; speech-to-text runs on your own CPU and nothing is
  sent to a server. Apache-2.0, and [good first issues are tagged and
  waiting](https://github.com/MSKazemi/yazses/issues?q=is%3Aopen+label%3A%22good+first+issue%22).
- **[AOBench](https://github.com/MSKazemi/aobench)** — role-aware, permission-enforced
  benchmark for LLM agents operating HPC systems. A policy violation hard-fails the task,
  however correct the answer looked.

## License

KubeIntellect is **dual-licensed** under the **[GNU AGPL-3.0-or-later](LICENSE)** *or* a **commercial license**. Self-host and modify freely under the AGPL; for closed/SaaS use without AGPL's network-copyleft obligations, a commercial license is available. See **[LICENSING.md](LICENSING.md)**; contact **mohsen.seyedkazemi@gmail.com**.

## Citation

If you use KubeIntellect in your research, please cite the paper (metadata in [CITATION.cff](CITATION.cff)):

> Seyedkazemi Ardebili, M., & Bartolini, A. (2026). *KubeIntellect: A Modular LLM-Orchestrated Agent Framework for End-to-End Kubernetes Management.* Journal of Grid Computing, 24(3). https://doi.org/10.1007/s10723-026-09837-6
