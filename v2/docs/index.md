---
description: >-
  AI-powered Kubernetes management. Diagnose CrashLoopBackOff, pending pods, and RBAC issues in plain English — human-approval gates before any write operation.
hide:
  - navigation
  - toc
---

<div class="ki-hero">
  <div class="ki-hero-inner">
    <img src="assets/brand/ki-c-indigo.svg" alt="KubeIntellect" class="ki-hero-mark" />
    <h1 class="ki-wordmark"><span>KUBE</span><span class="ki-green">INTELLECT</span></h1>
    <p class="ki-tagline">AI DEVOPS ENGINEER FOR KUBERNETES</p>
    <p class="ki-hero-desc">
      Diagnose CrashLoopBackOff, pending pods, and RBAC issues in plain English.
      Parallel specialist agents investigate your cluster — a human-approval gate
      waits before any write operation runs.
    </p>
    <div class="ki-ctas">
      <a href="quickstart/" class="md-button md-button--primary">Get Started →</a>
      <a href="https://github.com/MSKazemi/kubeintellect" class="md-button">View on GitHub</a>
    </div>
  </div>
</div>

<div class="ki-stats">
  <div class="ki-stat">
    <span class="ki-stat-value">18</span>
    <span class="ki-stat-label">Failure playbooks</span>
  </div>
  <div class="ki-stat">
    <span class="ki-stat-value">4</span>
    <span class="ki-stat-label">Parallel subagents</span>
  </div>
  <div class="ki-stat">
    <span class="ki-stat-value">4</span>
    <span class="ki-stat-label">Role tiers</span>
  </div>
  <div class="ki-stat">
    <span class="ki-stat-value">HITL</span>
    <span class="ki-stat-label">All write ops gated</span>
  </div>
</div>

---

## What it does

<div class="grid cards" markdown>

-   :material-kubernetes: **Kubernetes Intelligence**

    ---

    Runs `kubectl` across get, describe, logs, top, events, apply, scale, and delete.
    Routes complex diagnostics to four parallel specialist subagents (pod, metrics, logs, events)
    and synthesises findings into a single root-cause report.

-   :material-chart-line: **Metrics + Logs**

    ---

    Native Prometheus PromQL and Loki LogQL integration. The coordinator automatically
    delegates to the right data source — you ask in plain English, it picks the tool.

-   :material-shield-check: **Safety Gates**

    ---

    Every destructive operation pauses for human approval before kubectl is called.
    Four role tiers (superadmin / admin / operator / readonly) limit what each API
    key can request. Shell injection, secret/serviceaccount access, and writes to
    infrastructure namespaces are blocked before any subprocess runs.

-   :material-brain: **Stateful Conversations**

    ---

    Sessions are checkpointed in PostgreSQL or SQLite. Ask follow-up questions, approve
    a pending action hours later, or replay a session post-mortem — all in the same thread.

</div>

---

## See it in action

<div class="ki-terminal">
  <div class="ki-terminal-bar">
    <span class="ki-terminal-dot red"></span>
    <span class="ki-terminal-dot amber"></span>
    <span class="ki-terminal-dot green"></span>
    <span class="ki-terminal-title">kq — KubeIntellect CLI</span>
  </div>
  <div class="ki-terminal-body">
    <div><span class="prompt">❯</span> <span class="cmd">kq</span></div>
    <div class="dim">Connected to KubeIntellect · cluster: prod-aks · namespace: default</div>
    <br/>
    <div><span class="prompt">You</span> <span class="cmd">why is the payments pod crashing?</span></div>
    <br/>
    <div class="output">  Dispatching to DiagnosticsOrchestrator …</div>
    <div class="output">  ├─ pod-agent     → OOMKilled (exit 137, last 3 restarts)</div>
    <div class="output">  ├─ metrics-agent → memory.working_set spiking to 512 Mi (limit: 256 Mi)</div>
    <div class="output">  └─ log-agent     → "java.lang.OutOfMemoryError: Java heap space"</div>
    <br/>
    <div><span class="hi">Root cause:</span> <span class="cmd">payments-api is hitting its memory limit.</span></div>
    <div class="cmd">Heap dump shows unbounded cache growth in PaymentSessionCache.</div>
    <br/>
    <div><span class="hi">Proposed fix:</span> <span class="cmd">patch memory limit → 512 Mi + add cache eviction policy.</span></div>
    <br/>
    <div><span class="warn">⚠ Requires approval before kubectl patch runs.</span></div>
    <div><span class="prompt">You</span> <span class="cmd">approve</span></div>
    <div class="output">  ✓ patched deployment/payments-api — rollout in progress</div>
  </div>
</div>

---

## How it works

```
You (kq CLI or any OpenAI-compatible client)
   │  POST /v1/chat/completions  (SSE streaming)
   ▼
┌──────────────────────────────────────────────────────────────────┐
│  LangGraph workflow                                              │
│                                                                  │
│  memory_loader  →  context_fetcher  →  coordinator               │
│   (DB context)    (live snapshot +      (LLM + tools)            │
│                    playbook match)                               │
│                                              │                   │
│           ┌──────────────────────────────────┴──────────┐        │
│           ▼ TARGETED      ▼ RCA_REQUIRED               ▼ direct │
│   targeted_investigator   subagent_executor × 4     answer →END │
│   (3 parallel reads)      (pod | metrics |                       │
│           │                logs | events,                        │
│           │                parallel fan-out)                     │
│           ▼                       │                              │
│       coordinator ← findings — coordinator (synthesis) → END     │
│                                                                  │
│  Tools: run_kubectl │ query_prometheus (PromQL) │ query_loki     │
│  HITL interrupt fires on every destructive kubectl verb.         │
└──────────────────────────────────────────────────────────────────┘
   │
   ▼
LangGraph checkpoint store (Postgres / SQLite)
+ rca_outcomes / failure_patterns (reflexion subsystem)
```

Each turn passes through five additive [agent behaviors](agent-behaviors.md) —
error interpretation, snapshot bias, parallel discipline, playbook injection,
and a visible investigation plan. Verified outcomes feed the
[reflexion subsystem](reflexion.md), which promotes recurring fixes back into
future prompts (cluster-scoped, with cooldown and decay).

Responses stream back as Server-Sent Events. The API is OpenAI-compatible — point any
SSE client or your own tooling at `/v1/chat/completions`.

---

## Pick your path

<div class="grid cards" markdown>

-   **:material-lightning-bolt: Quickest — no cluster**

    ---

    `pip install kubeintellect` + SQLite. Explore the API and CLI in minutes, no Kubernetes needed.

    [→ Install guide](install/no-cluster.md)

-   **:material-server-network: Existing cluster**

    ---

    Connect KubeIntellect to any cluster you already have — AKS, EKS, GKE, or any kubeconfig.

    [→ Install guide](install/existing-cluster.md)

-   **:material-docker: Docker Compose**

    ---

    Full local stack with PostgreSQL, optional Prometheus + Grafana + Loki, and optional Langfuse LLM tracing.

    [→ Deploy guide](deploy/docker-compose.md)

-   **:material-cloud-upload: Production (Helm)**

    ---

    Helm chart for AKS / EKS / GKE. Includes RBAC, secrets management, ingress, and resource limits.

    [→ Deploy guide](deploy/cloud.md)

</div>

---

## Quick install

=== "pip (no cluster)"

    ```bash
    pip install kubeintellect
    kubeintellect init   # setup wizard — configures LLM key,
                         # optionally creates Kind cluster,
                         # installs systemd service
    kq                   # open a new terminal — that's it
    ```

=== "Docker Compose"

    ```bash
    git clone https://github.com/MSKazemi/kubeintellect
    cd kubeintellect
    cp .env.example .env        # set LLM key + KUBEINTELLECT_ADMIN_KEYS
    docker compose up -d
    pip install kube-q
    KUBE_Q_API_KEY=<your-admin-key> kq --url http://localhost:8000
    ```

=== "Kind (local K8s)"

    ```bash
    git clone https://github.com/MSKazemi/kubeintellect
    cd kubeintellect
    make kind-cluster-create
    cp .env.example .env        # add your LLM key
    make kind-deploy-kubeintellect
    make cli                    # opens kq REPL
    ```

=== "Helm (production)"

    ```bash
    # the chart is published as an OCI artifact — no `helm repo add` step
    helm install kubeintellect oci://ghcr.io/mskazemi/charts/kubeintellect \
      --set llm.provider=openai \
      --set llm.apiKey=<YOUR_KEY>
    ```

---

## Supported LLM providers

<div class="grid cards" markdown>

-   :material-brain: **OpenAI**

    GPT-4o (coordinator) + GPT-4o-mini (subagents). Set `LLM_PROVIDER=openai`.

-   :material-microsoft-azure: **Azure OpenAI**

    Any Azure-hosted deployment. Default `AZURE_OPENAI_API_VERSION=2024-10-01-preview`
    enables automatic prefix caching. Set `LLM_PROVIDER=azure`.

</div>

See [Configuration → LLM provider](configuration.md#llm-provider) for the full
list of model and deployment variables.

---

<div style="text-align:center; padding: 1rem 0 0.5rem; color: #374151; font-size: 0.78rem; letter-spacing: 0.08em; text-transform: uppercase;">
  Open-source · AGPL-3.0 · <a href="https://github.com/MSKazemi/kubeintellect" style="color: #00e676;">GitHub</a> · <a href="v1/" style="color: #64748b;">KubeIntellect v1 (legacy)</a>
</div>
