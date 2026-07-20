---
description: >-
  Answers to the most common KubeIntellect questions — which install path,
  which LLM provider, does it need PostgreSQL, is it safe to run writes, and more.
---

# Frequently Asked Questions

Short answers to the questions people ask before and during their first hour with
KubeIntellect. Each one links to the page that covers it in depth.

---

## Getting started

### What is KubeIntellect in one line? Who is it for?

KubeIntellect is a **Human-Governed AI SRE for Kubernetes**: you ask questions in
plain English ("why is the payments pod crashing?"), it investigates your cluster
with real tools, explains what it found, and — with your approval — fixes it. It
is for anyone who operates a Kubernetes cluster and would rather describe a symptom
than remember `kubectl`, PromQL, and LogQL syntax. See
[What you can ask](capabilities.md).

### Which install path should I pick?

Pick by what you already have — the [Quickstart](quickstart.md) has a full table:

- **No install, no cluster** — the [browser demo](install/no-cluster.md#try-it-in-your-browser-zero-install) (read-only) or **C1**, the `pip install kube-q` CLI against the hosted API.
- **No Docker, no cluster** — **C2**: install Docker + Kind for the full local stack.
- **You already have a cluster** — **A** ([Docker Compose](deploy/docker-compose.md)) if you have Docker, or **B** ([pip install + existing cluster](install/existing-cluster.md)) if you don't.
- **A local Kind cluster** — **D** (`pip install` + Kind) or **E** ([Kind from the repo](deploy/kind.md), with monitoring + Langfuse).
- **Production on AKS / EKS / GKE** — **F**, the [Helm chart](deploy/cloud.md).

The fastest way to a working server is `pip install kubeintellect && kubeintellect init` —
the wizard installs `kubectl`, optionally creates a Kind cluster, and configures `kq`.

### Which LLM provider does it use by default?

The default is **Azure OpenAI** (`LLM_PROVIDER=azure`), driving `gpt-4o` for the
coordinator/synthesis tier and `gpt-4o-mini` for the subagents. Three other
providers are supported: `openai` (same models, OpenAI-hosted), `qwen` (Alibaba
DashScope, OpenAI-compatible — `qwen-max` / `qwen-plus`), and `anthropic`
(`claude-sonnet-4-6` / `claude-haiku-4-5`, wired only through the V4 cortex). You
set exactly one. See [Configuration → LLM provider](configuration.md#llm-provider).

### Do I need PostgreSQL?

No — **SQLite works out of the box** for local and no-Docker use, and all
diagnostics work on it. `kubeintellect serve` auto-detects PostgreSQL and falls
back to SQLite (`~/.kubeintellect/kubeintellect.db`) if none is reachable.
PostgreSQL unlocks the cross-session cognitive layers: the
[reflexion subsystem](reflexion.md), the [memory hierarchy](memory.md) (episodes
+ temporal knowledge graph), the memory loader, and the hash-chained
[flight recorder](flight-recorder.md) — all PostgreSQL-native. In SQLite mode these
are disabled; nothing else is. See [Configuration → Database](configuration.md#database).

---

## Safety and reach

### Is it safe? Will it delete things?

Not without you saying so. **Every write operation stops for human approval
first** — the agent shows you the exact `kubectl` command and waits for `yes` /
`/approve` before it runs. A set of cascading-blast actions (`delete namespace`,
`delete pv/crd`, `set image/resources`, `drain`) **always** requires confirmation,
even in auto-approve mode. Secrets and ServiceAccounts are blocked for every role,
and writes to infrastructure namespaces (`kube-system`, `monitoring`,
`kubeintellect`, …) are blocked by default. See [Security](security.md) and
[Safe changes](capabilities.md#safe-changes-human-in-the-loop).

### What can it actually reach?

Up to four live data sources, each behind a dedicated tool:

- **`run_kubectl`** — pods, deployments, events, nodes, logs, `describe`, YAML (reads free; writes gated).
- **`run_helm`** (read-only) — release list, values, status, history.
- **`query_prometheus`** — metric time series, when `PROMETHEUS_URL` is set.
- **`query_loki`** — logs over a time window, when `LOKI_URL` is set.

Full catalog in [What it can reach](capabilities.md#what-it-can-reach).

### Does it need Prometheus / Loki?

No — they are optional. Without `PROMETHEUS_URL` / `LOKI_URL` set, KubeIntellect
still answers every `kubectl`-shaped question; only metric and log history queries
are unavailable. Wire them up in [Configuration → Observability](configuration.md#observability-optional).

---

## V4 autonomous features

### What are the V4 autonomous features, and are they on by default?

V4 works *between* your questions. The default-on layers are the
[flight recorder](flight-recorder.md) (`FLIGHT_RECORDER_ENABLED`), the
[sensorium + detector engine](autonomy.md) (`SENSORIUM_ENABLED`) that recognizes
the 18 common failures at **zero LLM tokens**, the [memory hierarchy](memory.md)
(`MEMORY_HIERARCHY_ENABLED`), and the [watchtower](autonomy.md)
(`WATCHTOWER_ENABLED`) which opens A1 investigations (`AUTONOMY_LEVEL=A1` —
investigate + report, never auto-fix). Default-**off**: anticipatory
`PREDICTIVE_DETECTION_ENABLED`, the V4 reasoning graph `CORTEX_V4_ENABLED`,
NL detector authoring, and every experimental Memory V5 slice. See the
[Upgrade & Feature-Flag Guide](upgrade.md) for the full table.

### How much does it cost to run / how many LLM tokens?

The autonomous perception layer is genuinely **zero-token**: the compiled
detectors recognize known failures from the live watch stream without any LLM
call, and the morning [digest](autonomy.md#the-morning-digest) and deterministic
postmortem timeline are built without LLM calls too. Tokens are spent when the LLM
actually reasons — an interactive query or a watchtower A1 investigation. On the
default Azure path the cheap `gpt-4o-mini` handles subagents and the
`2024-10-01-preview` API version enables prefix caching on long coordinator
prompts. We don't publish a per-query dollar figure — it depends on your provider
pricing, cluster size, and how many investigations run.

---

## Production, licensing, upgrades

### Is it production-ready? What's the license?

KubeIntellect is **open-source under AGPL-3.0**. The V2 reasoning graph (the
default) plus the default-on V4 layers are the supported surface; the V4 cortex
and all Memory V5 / v5 slices ship default-off as opt-in previews. The Helm chart
covers AKS / EKS / GKE with RBAC, secrets, ingress, and resource limits — see the
[cloud deploy guide](deploy/cloud.md).

### How do I upgrade or turn on experimental features?

Enable one flag at a time and verify before the next. Everything risky ships
default-off, and memory/recorder/sensorium failures never break a user response.
The [Upgrade & Feature-Flag Guide](upgrade.md) is the runbook; the full
experimental catalog is in [v5 experimental flags](v5-experimental-flags.md).

### How is it different from k8sgpt / kubectl-ai / plain ChatGPT?

Plain ChatGPT can't see your cluster; KubeIntellect grounds every answer in live
`kubectl` / Helm / Prometheus / Loki data. Beyond running commands, it adds
capabilities those command-shaped tools don't combine: **parallel specialist
subagents** (pod / metrics / logs / events) that fan out for a real root-cause
analysis, **18 deterministic playbooks** that guide known-failure investigations,
a **human-in-the-loop approval gate** on every write, a **memory + reflexion**
layer that recalls past incidents and promotes verified fixes, and an **autonomy**
layer that opens its own investigations when a detector fires. See
[What you can ask](capabilities.md) and [V2 vs V4](v2-vs-v4-models.md).

---

## Related

- [Quickstart](quickstart.md) — pick an install path.
- [Configuration Reference](configuration.md) — every environment variable and default.
- [What you can ask](capabilities.md) — the capability catalog and example queries.
- [Security](security.md) — HITL, RBAC roles, and the protection layers.
- [Upgrade & Feature-Flag Guide](upgrade.md) — enable V4/V5 features safely.
- [Troubleshooting](troubleshooting.md) — when something doesn't work.
