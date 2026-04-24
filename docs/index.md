---
hide:
  - navigation
  - toc
---

<div class="ki-hero">
  <div class="ki-hero-inner">
    <img src="assets/brand/ki-mark.svg" alt="KubeIntellect" class="ki-hero-mark" />
    <h1 class="ki-wordmark"><span>KUBE</span><span class="ki-green">INTELLECT</span></h1>
    <p class="ki-tagline">AUTONOMOUS KUBERNETES OPERATIONS</p>
    <p class="ki-hero-desc">
      An AI-powered operations assistant that diagnoses, explains, and acts on your cluster —
      with parallel specialist agents for pods, metrics, and logs, and a human-approval gate
      before any destructive command runs.
    </p>
    <div class="ki-ctas">
      <a href="quickstart/" class="md-button md-button--primary">Get Started →</a>
      <a href="https://github.com/mskazemi/kubeintellect-v2" class="md-button">View on GitHub</a>
    </div>
  </div>
</div>

<div class="ki-stats">
  <div class="ki-stat">
    <span class="ki-stat-value">93%</span>
    <span class="ki-stat-label">Tool synthesis success</span>
  </div>
  <div class="ki-stat">
    <span class="ki-stat-value">200</span>
    <span class="ki-stat-label">Queries tested</span>
  </div>
  <div class="ki-stat">
    <span class="ki-stat-value">4</span>
    <span class="ki-stat-label">Parallel subagents</span>
  </div>
  <div class="ki-stat">
    <span class="ki-stat-value">3</span>
    <span class="ki-stat-label">Role tiers</span>
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
    Three role tiers (admin / operator / readonly) limit what each API key can request.
    Shell injection is blocked before any subprocess runs.

-   :material-brain: **Stateful Conversations**

    ---

    Sessions are checkpointed in PostgreSQL or SQLite. Ask follow-up questions, approve
    a pending action hours later, or replay a session post-mortem — all in the same thread.

</div>

---

## How it works

```
You (kq CLI)
     │  POST /v1/chat/completions  (SSE streaming)
     ▼
┌──────────────────────────────────────────────────┐
│  Coordinator                                     │
│  ┌──────────┐  ┌──────────┐  ┌────────────────┐  │
│  │  kubectl │  │Prometheus│  │      Loki      │  │
│  │   tool   │  │   tool   │  │      tool      │  │
│  └──────────┘  └──────────┘  └────────────────┘  │
│                                                  │
│  On complex issues — fan out to 4 subagents:     │
│  pod │ metrics │ logs │ events                   │
│             fan-in → synthesis                   │
└──────────────────────────────────────────────────┘
     │  HITL interrupt on destructive commands
     ▼
LangGraph checkpoint store (Postgres / SQLite)
```

Responses stream back as Server-Sent Events. The API is OpenAI-compatible — point any
SSE client at `/v1/chat/completions`.

---

## Pick your path

<div class="grid cards" markdown>

-   **:material-lightning-bolt: Quickest — no cluster**

    ---

    `pip install kubeintellect` + SQLite. Explore the API and CLI in minutes, no Kubernetes needed.

    [→ Install guide](install-pip-no-cluster.md)

-   **:material-server-network: Existing cluster**

    ---

    Connect KubeIntellect to any cluster you already have — AKS, EKS, GKE, or any kubeconfig.

    [→ Install guide](install-pip-existing-cluster.md)

-   **:material-docker: Docker Compose**

    ---

    Full local stack with PostgreSQL, optional Prometheus + Grafana + Loki, and optional Langfuse LLM tracing.

    [→ Deploy guide](deploy-docker-compose.md)

-   **:material-cloud-upload: Production (Helm)**

    ---

    Helm chart for AKS / EKS / GKE. Includes RBAC, secrets management, ingress, and resource limits.

    [→ Deploy guide](deploy-cloud.md)

</div>

---

## Quick install

=== "pip (no cluster)"

    ```bash
    pip install kubeintellect
    kubeintellect init   # setup wizard — installs kubectl, configures kube-q,
                         # optionally creates Kind cluster + observability,
                         # installs systemd service, then hands off to kq
    kq                   # open a new terminal and run — that's it
    ```

=== "Docker Compose"

    ```bash
    git clone https://github.com/mskazemi/kubeintellect-v2
    cd kubeintellect-v2
    cp .env.example .env        # add your LLM key
    docker compose up -d
    kq --url http://localhost:8000
    ```

=== "Kind (local K8s)"

    ```bash
    git clone https://github.com/mskazemi/kubeintellect-v2
    cd kubeintellect-v2
    make kind-cluster-create
    cp .env.example .env        # add your LLM key
    make kind-deploy-kubeintellect
    make cli                    # opens REPL
    ```
