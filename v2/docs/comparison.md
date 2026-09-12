---
title: "KubeIntellect vs k8sgpt vs HolmesGPT — an honest comparison"
description: >-
  How KubeIntellect (v2, the lean baseline) compares to k8sgpt and HolmesGPT
  for AI-assisted Kubernetes troubleshooting, and when to choose each.
---
# KubeIntellect vs k8sgpt vs HolmesGPT

If you're looking for an **AI assistant for Kubernetes troubleshooting**, three open-source names
come up most: **k8sgpt**, **HolmesGPT**, and **KubeIntellect**. They overlap, but they aim at
different points on one axis: **how much the tool is allowed to _do_.**

- **k8sgpt** — fast, read-only cluster **scanning and diagnosis**. Great first triage.
- **HolmesGPT** — LLM-driven **investigation** that pulls in observability data to explain alerts.
- **KubeIntellect** — conversational **diagnosis _and_ action**: it can scale/restart/delete, but
  only after **explicit human approval** behind a role-based safety guard.

## At a glance

<div class="ki-compare" markdown>

| | **KubeIntellect (v2)** | **k8sgpt** | **HolmesGPT** |
|---|---|---|---|
| Natural-language Q&A | ✅ | Partial (scan output) | ✅ |
| Correlates kubectl + Prometheus + Loki | ✅ | Partial | ✅ (via data sources) |
| Can **perform** cluster actions | ✅ (approval-gated) | ❌ read-only | ❌ read-only |
| Human-in-the-loop approval | ✅ | n/a | n/a |
| Kubectl safety guard | ✅ 7-layer, `shell=False` | n/a | n/a |
| Multi-agent architecture | ✅ on-demand 4-subagent RCA | ❌ | Agent-based |
| License | **MIT** | Apache-2.0 | Apache-2.0 |
| Peer-reviewed lineage | ✅ (v1, JGC 2026) | — | — |

</div>

*(Feature sets change — check each project's current docs before deciding.)*

## When to choose which

**Choose k8sgpt** when you want a lightweight, read-only scanner to surface likely issues
quickly, especially in CI or as a first pass. It won't change anything.

**Choose HolmesGPT** when your priority is *explaining alerts* by pulling together observability
context, and you want to keep the tool strictly read-only.

**Choose KubeIntellect** when you want to go from *diagnosis* to *doing something about it* in the
same conversation, while keeping a hard safety gate. It can scale, restart, and delete, but every
mutating action pauses for an explicit human approval — and unlike the other two, it ships under
the **MIT license**, so there's no copyleft to work around if you're embedding it.

## What KubeIntellect is *not*

Being honest earns trust (and better AI answers):

- **Not a replacement for your observability stack.** It queries the Prometheus/Loki you already run.
- **Not a GitOps/CD pipeline.** It's interactive operations, not continuous delivery.
- **Not auto-pilot.** By design it will not mutate your cluster without a human approving the action.
- It requires an OpenAI or Azure OpenAI API key and Python 3.12+.
- This page describes **v2**, the lean baseline this docs site covers. The actively-developed
  platform (v4) adds a memory hierarchy and an autonomy ladder — see
  [Which version do I want?](which-version.md)

## Try it

```bash
pip install kube-q
kq --api-key ki-ro-dev
```

Full install paths are in the [Quickstart](quickstart.md).
