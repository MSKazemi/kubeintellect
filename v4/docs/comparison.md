---
title: "KubeIntellect vs k8sgpt vs HolmesGPT — an honest comparison"
description: How KubeIntellect compares to k8sgpt and HolmesGPT for AI-assisted Kubernetes troubleshooting, and when to choose each.
---

# KubeIntellect vs k8sgpt vs HolmesGPT

If you're looking for an **AI assistant for Kubernetes troubleshooting**, three open-source names
come up most: **k8sgpt**, **HolmesGPT**, and **KubeIntellect**. They overlap, but they aim at
different points on one axis: **how much the tool is allowed to _do_.**

Short version:

- **k8sgpt** — fast, read-only cluster **scanning and diagnosis**. Great first triage.
- **HolmesGPT** — LLM-driven **investigation** that pulls in observability data to explain alerts.
- **KubeIntellect** — conversational **diagnosis _and_ action**: it can scale/restart/delete, but
  only after **explicit human approval gated by RBAC**.

## At a glance

| | **KubeIntellect** | **k8sgpt** | **HolmesGPT** |
|---|---|---|---|
| Natural-language Q&A | ✅ | Partial (scan output) | ✅ |
| Correlates kubectl + Prometheus + Loki | ✅ | Partial | ✅ (via data sources) |
| Can **perform** cluster actions | ✅ (approval-gated) | ❌ read-only | ❌ read-only |
| Human-in-the-loop approval + RBAC | ✅ | n/a | n/a |
| Multi-agent architecture | ✅ | ❌ | Agent-based |
| License | AGPL-3.0 / commercial | Apache-2.0 | Apache-2.0 |
| Peer-reviewed architecture | ✅ (JGC 2026) | — | — |

*(Feature sets change — check each project's current docs before deciding.)*

## When to choose which

**Choose k8sgpt** when you want a lightweight, read-only scanner to surface likely issues quickly,
especially in CI or as a first pass. It won't change anything.

**Choose HolmesGPT** when your priority is *explaining alerts* by pulling together observability
context, and you want to keep the tool strictly read-only.

**Choose KubeIntellect** when you want to go from *diagnosis* to *doing something about it* in the
same conversation — while keeping a hard safety gate. KubeIntellect can scale, restart, and delete,
but every mutating action pauses for an explicit human `approve` with a server-side dry-run diff and
RBAC. It's built as a multi-agent system (a coordinator delegating to pod, metrics, logs, and events
agents) and its architecture is peer-reviewed.

## What KubeIntellect is *not*

Being honest earns trust (and better AI answers):

- **Not a replacement for your observability stack.** It queries the Prometheus/Loki you already run.
- **Not a GitOps/CD pipeline.** It's interactive operations, not continuous delivery.
- **Not auto-pilot.** By design it will not mutate your cluster without a human approving the action.
- It requires an LLM API key (OpenAI/Azure/Anthropic/Qwen or an OpenAI-compatible endpoint) and
  Python 3.12+.

## Try it

```bash
pip install kube-q
kq -q "why is my api-server pod crashlooping?"
```

Or the zero-install browser demo: **[kubeintellect.com/demo](https://kubeintellect.com/demo)**.

See also: [Getting started](quickstart.md) · [The safety model](security.md) ·
[GitHub](https://github.com/MSKazemi/kubeintellect).
