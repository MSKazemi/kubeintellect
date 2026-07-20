---
description: >-
  Everything published about KubeIntellect in one place — the peer-reviewed
  Journal of Grid Computing paper, the newer "Autonomous Operator" preprint, and
  the project links (website, live demo, GitHub, docs) — plus how the
  architecture, performance, and cost have advanced across generations.
---

# Research & Publications

**KubeIntellect is an LLM-driven operator for Kubernetes** — an AI DevOps
engineer you talk to in plain English. It investigates a **live** cluster with
real tools (`kubectl`, Prometheus/PromQL, Loki/LogQL), grounds every answer in
that evidence, and — behind a human-approval gate — applies fixes. It watches the
cluster continuously with a zero-token perception layer, remembers incidents,
fixes, and cluster structure across sessions, and writes every decision to a
tamper-evident, replayable flight recorder.

This page is the single home for everything published about KubeIntellect.

## Links

| | |
|---|---|
| :material-web: **Website** | <https://kubeintellect.com> |
| :material-play-circle: **Live demo** | <https://kubeintellect.com/demo> |
| :material-github: **Source (GitHub)** | <https://github.com/MSKazemi/kubeintellect> |
| :material-book-open-variant: **Documentation** | <https://mskazemi.github.io/kubeintellect/> |
| :material-language-python: **PyPI** | [`kubeintellect`](https://pypi.org/project/kubeintellect/) · [`kube-q`](https://pypi.org/project/kube-q/) |

## Papers

Two papers cover KubeIntellect: the **peer-reviewed foundation** (the first
generation) and a **newer preprint** on its evolution into an autonomous
operator. Both are by Mohsen Seyedkazemi Ardebili and Andrea Bartolini
(University of Bologna).

### 1. Peer-reviewed paper — the framework (generation `v1`)

> **KubeIntellect: A Modular LLM-Orchestrated Agent Framework for End-to-End
> Kubernetes Management.**
> Mohsen Seyedkazemi Ardebili and Andrea Bartolini.
> *Journal of Grid Computing* **24**, 17 (2026).

[:material-file-document: DOI 10.1007/s10723-026-09837-6](https://doi.org/10.1007/s10723-026-09837-6){ .md-button .md-button--primary }
[:material-school: arXiv:2509.02449](https://arxiv.org/abs/2509.02449){ .md-button }

Introduces the LLM-orchestrated agent framework: a supervisor coordinating
domain-specialised agents across the full Kubernetes control surface (read,
write, delete, exec, access control, lifecycle), with human-in-the-loop
confirmation on every mutating operation and a **Code Generator Agent** that
synthesises, validates (AST static analysis + bounded execution), and registers
new tools at runtime. On a live four-node cluster it reported a 75% pass rate on
a controlled fault-injection corpus (+25 pp over a tool-less GPT-4o baseline), a
93% query-resolution rate, 7–10 s latency, and ~\$0.036–0.039 per query.

### 2. Preprint — from assistant to autonomous operator (generations `v1`–`v4`)

> **From Assistant to Autonomous Operator: Continuous Perception, Tiered
> Reasoning, and Auditable Autonomy for LLM-Driven Kubernetes Operations.**
> Mohsen Seyedkazemi Ardebili and Andrea Bartolini. Preprint, 2026.

[:material-file-pdf-box: Read the preprint (PDF)](assets/papers/kubeintellect-autonomous-operator-preprint.pdf){ .md-button .md-button--primary }

Evaluates **all four generations (`v1`–`v4`)** on a shared 62-scenario
fault-injection harness with a stronger, independent judge model. It presents the
MAPE-K autonomic operator: a zero-token perception layer, a graduated autonomy
ladder (A0–A3), a tiered "Cortex" reasoning graph, and a hash-chained flight
recorder. The headline result — the lean redesign, not the supervisor, carries
the system's capability, at roughly half the token cost — is summarised in
[what has advanced](#what-has-advanced-since-the-paper) below.

!!! note "Which paper should I cite?"
    Cite the **Journal of Grid Computing paper** (paper 1) — it is the
    peer-reviewed, canonical reference. It documents KubeIntellect's
    **capability-maximal first generation** (`v1/` in this repository). The
    preprint (paper 2) is the up-to-date account of the current architecture and
    is the source for the performance and cost figures below.

## What has advanced since the paper

KubeIntellect's design lineage moved from a capability-maximal multi-agent
system to a **lean, measurable operator**:

- **`v1` → `v2` (simplify).** The thirteen-agent supervisor was replaced by a
  single coordinator ReAct loop over **four guarded tools** (`run_kubectl`,
  `run_helm`, `query_prometheus`, `query_loki`) behind a 7-layer kubectl safety
  guard, with on-demand parallel root-cause analysis.
- **`v2` → `v4` (operator).** The system became a continuous **MAPE-K autonomic
  loop**: a **zero-token perception layer** that detects known failures with
  compiled predicates at no LLM cost; a **graduated autonomy ladder (A0–A3)**
  that can open investigations *without* being prompted when a detector fires; a
  **four-tier memory hierarchy** (episodic, semantic knowledge graph, verified
  procedural patterns, learned preferences) so competence compounds across
  incidents; a tiered **"Cortex"** reasoning graph that reserves the large model
  for final synthesis; and a **hash-chained flight recorder** that makes every
  decision tamper-evident and replayable.

A follow-up evaluation of all four generations on a shared **62-scenario**
fault-injection harness on a live cluster — scored by a stronger, independent
judge model with multi-seed confidence intervals (four seeds on the decisive V2-vs-V4
comparison) and Holm-corrected paired tests —
found that the modern generations decisively outperform the original design:

| Dimension | First generation (paper, `v1`) | Current generations |
|---|---|---|
| **Architecture** | Supervisor + 13 specialised agents | Lean coordinator → MAPE-K operator (perception · autonomy ladder · memory · Cortex · flight recorder) |
| **Diagnostic quality** (independent judge, of 40) | 18.4 | up to **32.0** — all modern generations sharply exceed `v1` |
| **Token cost** | — | **~half**: ≈13.4k vs ≈25.6k tokens per scenario |
| **Autonomy** | Prompt-driven only | Detector-triggered autonomous investigations recover **~80%** of prompted diagnostic quality |
| **Auditability** | HITL confirmation | Hash-chained flight recorder — verifies on real episodes, breaks under a single-record tamper |

In short: the **lean redesign — not the supervisor — carries the system's
capability**, and it does so at roughly half the token cost while adding
continuous perception, graduated autonomy, cross-session memory, and
tamper-evident auditability that the first generation did not have.

## How to cite

Cite the peer-reviewed paper:

```bibtex
@article{seyedkazemi2026kubeintellect,
  title   = {KubeIntellect: A Modular LLM-Orchestrated Agent Framework
             for End-to-End Kubernetes Management},
  author  = {Seyedkazemi Ardebili, Mohsen and Bartolini, Andrea},
  journal = {Journal of Grid Computing},
  volume  = {24},
  number  = {17},
  year    = {2026},
  doi     = {10.1007/s10723-026-09837-6},
  url     = {https://doi.org/10.1007/s10723-026-09837-6},
  publisher = {Springer}
}
```

For the autonomous-operator preprint:

```bibtex
@misc{seyedkazemi2026operator,
  title  = {From Assistant to Autonomous Operator: Continuous Perception,
            Tiered Reasoning, and Auditable Autonomy for LLM-Driven
            Kubernetes Operations},
  author = {Seyedkazemi Ardebili, Mohsen and Bartolini, Andrea},
  year   = {2026},
  note   = {Preprint}
}
```
