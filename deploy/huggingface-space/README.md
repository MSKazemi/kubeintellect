---
title: KubeIntellect
emoji: ☸️
colorFrom: indigo
colorTo: blue
sdk: gradio
sdk_version: 6.22.0
app_file: app.py
pinned: true
license: agpl-3.0
short_description: Human-governed AI SRE — chat with a live Kubernetes cluster
models: []
tags:
  - kubernetes
  - devops
  - sre
  - agents
  - langgraph
  - observability
  - aiops
  - multi-agent
---

# ☸️ KubeIntellect — Human-governed AI SRE for Kubernetes

Ask a **real Kubernetes cluster** anything in plain English. A coordinator agent fans out to
specialized subagents that query **kubectl**, **Prometheus** (PromQL) and **Loki** (LogQL)
live, correlates the evidence, and answers. Any change to the cluster pauses for **explicit
human approval**, gated by role-based access control.

This Space is a thin client over the project's **public read-only demo API**. Expand the grey
activity blocks under each answer to see the tool calls the agent actually made.

## What you are talking to

| | |
|---|---|
| **Backend** | `https://api.kubeintellect.com` — OpenAI-compatible `POST /v1/chat/completions` |
| **Cluster** | A small, shared, **read-only** demo cluster |
| **Access** | The public `ki-ro-dev` read-only key (also printed in the project README) |

## Try it

- *How many pods are running in the default namespace?*
- *Are any pods unhealthy or restarting right now?*
- *Show me the most recent warning events in the cluster.*
- *Scale the nginx deployment in the default namespace to 3 replicas* → the agent works out the
  exact `kubectl` command, then **RBAC refuses it** before it runs. The transcript shows both.

## Honest limits

- **Read-only and shared.** The demo key holds the `readonly` role, so writes are refused by
  RBAC — you see the *safety gate*, not the approval flow. The human-in-the-loop prompt (approve
  / deny each risky command) is what you get with an **operator** key on your own deployment.
- Everyone shares one small cluster, so answers reflect whatever is running there at the time.
- **Do not paste secrets.** Your question is sent to the demo backend and on to an LLM
  provider. The project redacts secrets in what it *stores*, not in what it sends to the model.
- **This is a demo, not a service.** No uptime guarantee; the backend may be down or rate
  limited.

## Run it on your own cluster

```bash
pip install kube-q
kq -q "why is my api-server pod crashlooping?"
```

Full local stack (Docker is the only prerequisite) and self-hosting instructions are in the
[quick start](https://github.com/MSKazemi/kubeintellect#quick-start).

## Links

- **Website** — <https://kubeintellect.com/>
- **Source** — <https://github.com/MSKazemi/kubeintellect> (AGPL-3.0)
- **PyPI** — [`kube-q`](https://pypi.org/project/kube-q/) · [`kubeintellect`](https://pypi.org/project/kubeintellect/)
- **Paper** — [*Journal of Grid Computing*, 10.1007/s10723-026-09837-6](https://doi.org/10.1007/s10723-026-09837-6)
  · [arXiv:2509.02449](https://arxiv.org/abs/2509.02449)

Created and maintained by **[Mohsen Seyedkazemi Ardebili](https://github.com/MSKazemi)**.

## Citation

```bibtex
@article{seyedkazemiardebili2026kubeintellect,
  title     = {KubeIntellect: A Modular LLM-Orchestrated Agent Framework for
               End-to-End Kubernetes Management},
  author    = {Seyedkazemi Ardebili, Mohsen and Bartolini, Andrea},
  journal   = {Journal of Grid Computing},
  publisher = {Springer},
  volume    = {24},
  number    = {3},
  year      = {2026},
  doi       = {10.1007/s10723-026-09837-6},
  issn      = {1572-9184}
}
```
