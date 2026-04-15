---
hide:
  - navigation
  - toc
---

<div class="hero-section" markdown>

# Chat with your Kubernetes cluster in plain English

**KubeIntellect** is an AI-powered Kubernetes management platform.
Describe a problem — a `CrashLoopBackOff`, a pending pod, an RBAC error — and a
multi-agent LLM system diagnoses it, proposes a fix, shows you a dry-run diff,
and waits for your approval before touching anything.

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://github.com/MSKazemi/kubeintellect/blob/main/LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org)
[![Docker](https://img.shields.io/badge/ghcr.io-MSKazemi%2Fkubeintellect-2496ED.svg?logo=docker&logoColor=white)](https://github.com/MSKazemi/kubeintellect/pkgs/container/kubeintellect-release)
[![Build](https://github.com/MSKazemi/kubeintellect/actions/workflows/docker-ghcr.yml/badge.svg)](https://github.com/MSKazemi/kubeintellect/actions/workflows/docker-ghcr.yml)

[Get Started :material-arrow-right:](installation.md){ .md-button .md-button--primary .hero-cta }
[Architecture :material-sitemap:](architecture.md){ .md-button .hero-cta }
[GitHub :material-github:](https://github.com/MSKazemi/kubeintellect){ .md-button .hero-cta }

</div>

---

## Why KubeIntellect?

<div class="grid cards" markdown>

-   :material-magnify: **Root Cause Analysis**

    ---

    Correlates logs, metrics, events, and resource config **in parallel** — not just a log
    dump. Surfaces the actual cause, not a list of things to check.

-   :material-shield-check: **Human-in-the-Loop by Design**

    ---

    Every write operation produces a dry-run diff and pauses for your explicit approval.
    No unreviewed changes on live clusters — ever.

-   :material-graph: **Multi-Agent Orchestration**

    ---

    14 specialized agents (Logs, RBAC, Metrics, Security, Lifecycle, CodeGenerator, …) routed
    by a Supervisor LLM via LangGraph StateGraph.

-   :material-code-braces-box: **Dynamic Tool Generation**

    ---

    Need a capability that doesn't exist yet? KubeIntellect generates Python tools, sandboxes
    them, and registers them for reuse — all with your approval.

-   :material-brain: **Persistent Memory**

    ---

    Per-user context: sticky namespace, routing lessons, 30 pre-seeded failure patterns,
    and preference learning across sessions.

-   :material-swap-horizontal: **LLM-Agnostic**

    ---

    Azure OpenAI, OpenAI, Anthropic Claude, Google Gemini, AWS Bedrock, Ollama, LiteLLM —
    swap providers with a single env var.

</div>

---

## See It in Action

=== ":material-bug: Diagnose a crash"

    ```text
    > Why is my payment-api pod crashing in the prod namespace?

    Fetching logs, events, and resource config in parallel...

    Root cause: OOMKilled — container hit the 256Mi memory limit.
    Last 3 events: BackOff restarts (5m ago, 3m ago, 1m ago).

    Recommendation: increase memory limit to 512Mi.
    Dry-run diff ready — confirm to apply? [approve / deny]
    ```

=== ":material-shield-account: Audit RBAC"

    ```text
    > Who has cluster-admin access?

    3 service accounts hold cluster-admin:
      • default/tiller           (inactive — consider revoking)
      • kube-system/admin-sa     (system — expected)
      • kubeintellect/core-sa    (this platform — expected)

    Tiller is a known attack surface. Show remediation steps?
    ```

=== ":material-tools: Generate a tool"

    ```text
    > I need a tool that shows pods sorted by restart count

    No matching tool in registry. Generating...

    [HITL] Review generated code before registration? [approve / deny]
    [approve]

    Tool registered as 'list_pods_by_restart_count'. Running now:
      pod/api-6d4f9b       14 restarts
      pod/worker-2          3 restarts
      pod/scheduler-1       0 restarts
    ```

=== ":material-scale-balance: Scale with approval"

    ```text
    > Scale down all staging deployments to 0 replicas overnight

    I'll scale the following (dry-run diff attached):
      api      3 → 0 replicas
      worker   2 → 0 replicas
      cron     1 → 0 replicas

    Confirm? [approve / deny]
    ```

---

## Architecture at a Glance

```
User query
  → Memory Orchestrator (reflections + failure hints + user prefs + registered tools ≤ 550 tokens)
  → Supervisor LLM     (LangGraph StateGraph routing)
  → Specialized agents (ReAct loops → Kubernetes API)
  → HITL gate          (diff + approval for all write operations)
  → Streaming SSE response
```

| Layer | Technology |
|-------|-----------|
| Orchestration | LangGraph StateGraph |
| API Server | FastAPI + Server-Sent Events |
| State & Checkpoints | PostgreSQL (LangGraph checkpointer) |
| Chat History | MongoDB (LibreChat) |
| Dynamic Tools | PVC + PostgreSQL registry |
| Observability | Langfuse · Prometheus · Loki |
| Frontend | LibreChat |

[:material-sitemap: Full Architecture Diagrams](flowcharts/index.md){ .md-button }

---

## Explore the Documentation

<div class="grid cards" markdown>

-   :material-rocket-launch: **[Getting Started](installation.md)**

    ---

    Deploy KubeIntellect locally with Kind or to Azure AKS.
    Full prerequisites, credentials, and Helm walkthrough.

-   :material-sitemap: **[Architecture](architecture.md)**

    ---

    Deep-dive into the multi-agent system, the supervisor routing logic,
    tool design patterns, and all storage layers.

-   :material-file-tree: **[Flowcharts](flowcharts/index.md)**

    ---

    Interactive Mermaid diagrams — system overview, supervisor flow,
    CodeGenerator pipeline, and complete workflow topology.

-   :material-cog: **[Operations](runbook.md)**

    ---

    Deployment runbooks, known issues, troubleshooting guides,
    observability stack setup, and backup / restore procedures.

-   :material-shield-lock: **[Security](security-model.md)**

    ---

    CodeGenerator sandbox (AST + exec timeout + SHA-256),
    RBAC model, secret handling policy, and GDPR compliance.

-   :material-human-queue: **[HITL Workflow](hitl.md)**

    ---

    How human-in-the-loop approval works — breakpoints,
    checkpoint/resume cycle, and the API contract.

-   :material-chart-line: **[Observability](observability.md)**

    ---

    Langfuse LLM tracing, Prometheus metrics, Loki log aggregation,
    and self-hosted stack configuration.


</div>

---

## Quick Start

```bash
# 1. Clone & configure
git clone https://github.com/MSKazemi/kubeintellect
cd kubeintellect
cp .env.example .env       # fill in LLM credentials

# 2. Deploy to local Kind cluster (full setup in one command)
make kind-kubeintellect-clean-deploy

# 3. Access the UI
make port-forward-librechat  # → http://localhost:3080
```

!!! tip "Fastest path"
    Run `make kind-kubeintellect-clean-deploy` — it creates the Kind cluster,
    generates secrets from `.env`, builds the image, and deploys via Helm.
    Total time: ~5 minutes on first run.

See [Installation](installation.md) for Azure AKS, N1, or other targets.
