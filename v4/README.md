<div align="center">
  <img src="docs/assets/brand/ki-c-indigo.svg" alt="KubeIntellect" width="96" height="96" />
  <h1>KubeIntellect</h1>
  <p>AI DevOps engineer for Kubernetes</p>

  [![PyPI](https://img.shields.io/pypi/v/kubeintellect.svg)](https://pypi.org/project/kubeintellect/)
  [![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
  [![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
  [![Docs](https://img.shields.io/badge/docs-mskazemi.github.io-0075C4?logo=materialformkdocs&logoColor=white)](https://mskazemi.github.io/kubeintellect/)
  [![GitHub Stars](https://img.shields.io/github/stars/MSKazemi/kubeintellect?style=social)](https://github.com/MSKazemi/kubeintellect)

  **[Website](https://kubeintellect.com/)** · **[Live Demo](https://kubeintellect.com/demo)** · **[Docs](https://mskazemi.github.io/kubeintellect/)** · **[v1 (legacy)](https://github.com/MSKazemi/kubeintellect/tree/v1-legacy)**
</div>

Ask KubeIntellect a question in plain English — it queries kubectl, Prometheus, and Loki live, then answers. Destructive operations pause for your explicit approval before anything runs.

```bash
kq "why is my api-server pod crashlooping?"
kq "show me pods with high restart counts in the default namespace"
kq "scale the frontend deployment to 5 replicas"   # pauses for your approval
```

> **Safe by default** — read-only queries run immediately; scale, delete, and restart operations require explicit human-in-the-loop approval.

---

## Quickstart — Pick Your Path

| Starting point | Path |
|----------------|------|
| Try it instantly — no install at all | [Browser demo](https://kubeintellect.com/demo) — open in browser, slower, read-only |
| Try it fast — no Docker, no cluster | [A — kube-q CLI](#a--kube-q-cli-no-install-except-pip) (read-only, one `pip install`) |
| Try it fast — no cluster, install Docker | [B — create local cluster](#b--create-local-cluster) (~5 min, all features) |
| Have Docker, no cluster | [pip install + `kubeintellect init`](#c--local-install-have-docker-or-existing-cluster) |
| Have an existing cluster | [pip install + `kubeintellect init`](#c--local-install-have-docker-or-existing-cluster) |
| Want Docker Compose / production setup | [Docker Compose](#docker-compose-laptop--vm---no-cluster-required-to-run-the-server) |

---

### A — kube-q CLI (no install except pip)

Install only the thin CLI. `kq` defaults to `https://api.kubeintellect.com` — no `--url` needed.

```bash
pip install kube-q
kq --api-key ki-ro-dev
```

> Read-only — the demo cluster is shared. Destructive ops are disabled. For full access use path B.

---

### Browser demo (zero install)

No terminal, no install. Open **[kubeintellect.com/demo](https://kubeintellect.com/demo)** directly.

> Slower than the CLI — the browser terminal shares a single hosted instance. Read-only access.

---

### B — Create local cluster

Docker is the only prerequisite. `kubeintellect init` installs Kind, creates the cluster, deploys sample workloads, and starts a background service.

**1. Install Docker**
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
```

**2. Install KubeIntellect**
```bash
pip install kubeintellect
```

**3. Run the wizard — answer Y to everything**
```bash
kubeintellect init
```

The wizard:
- Asks your LLM provider (OpenAI or Azure) and API key
- Creates a local Kind cluster with sample workloads
- Optionally installs Prometheus, Grafana, and Loki
- Optionally deploys 5 broken-pod RCA scenarios to practise with
- Generates an API key and configures `kq` automatically
- Installs a systemd service so the server starts on every login

**4. Open a new terminal**
```bash
kq
```

No manual server start, no copy-pasting API keys.

---

### C — Local install (have Docker or existing cluster)

```bash
pip install kubeintellect
```

> **`kubeintellect: command not found` / `kq: command not found`?**
> pip installs scripts to `~/.local/bin` which may not be on your PATH. Fix it permanently:
> ```bash
> echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
> ```

**Prefer isolated install?** pipx manages a private env automatically — no PATH fiddling:
```bash
pipx install kubeintellect   # apt install pipx  if missing
```

> **Ubuntu 22.04** ships Python 3.10. You need 3.12+:
> ```bash
> sudo add-apt-repository ppa:deadsnakes/ppa -y
> sudo apt-get install -y python3.12 python3.12-distutils
> python3.12 -m pip install kubeintellect
> ```

### First-time setup — one command

```bash
kubeintellect init
```

The wizard will:
- Ask your LLM provider (OpenAI or Azure) and API key
- Offer to create a local Kind cluster with sample workloads
- Offer to install Prometheus, Grafana, and Loki for observability
- Offer to deploy broken-pod RCA scenarios to practise with
- Choose SQLite (default) or PostgreSQL for persistence
- Generate an API key and configure `kq` automatically
- Install a systemd service so the server starts on every login

After `init` completes, open a new terminal and run:

```bash
kq
```

That's it. No manual server start, no copy-pasting API keys.

### What `kubeintellect status` shows

```
  Config:    ✓  ~/.kubeintellect/.env
  LLM:       ✓  azure / gpt-4o
  DB:        ✓  sqlite  ~/.kubeintellect/kubeintellect.db
  kubectl:   ✓  found
  Kube:      ✓  ~/.kube/config  context: kind-kubeintellect
  Auth:      ✓  enabled
    admin     ki-admin-xxxxxxxxxxxxxxxxxxxx   ← use this as KUBE_Q_API_KEY
  Prometheus:✓  http://172.18.0.2:30090  reachable
  Loki:      ✓  http://172.18.0.2:30100  reachable
  Grafana:   ✓  http://172.18.0.2:30080  reachable
  kube-q:    ✓  found
```

### Database

| Mode | When | Setup |
|------|------|-------|
| SQLite | Default — local / testing | None — `init` sets it automatically |
| PostgreSQL | Production / team | Set `DATABASE_URL` in `~/.kubeintellect/.env` |

---

## kube-q — Terminal Client

**kube-q** is the CLI that talks to KubeIntellect. Since V4 it is developed in this repository (`packages/kube-q/`) and ships with the server install; it remains available standalone on PyPI and can point at any running instance.

```bash
pip install kube-q
kq "why is my pod crashlooping?"
```

[![PyPI](https://img.shields.io/pypi/v/kube-q.svg)](https://pypi.org/project/kube-q/)
[![GitHub](https://img.shields.io/badge/github-MSKazemi%2Fkube__q-blue)](https://github.com/MSKazemi/kube_q)

---

## Architecture

```
kq (CLI)  ──► KubeIntellect API (FastAPI + LangGraph)
                │
                ├── Coordinator (GPT-4o)
                │     ├── simple query  → direct tool use → answer
                │     └── complex fault → fan-out to 4 parallel subagents
                │           ├── Pod subagent     (kubectl)
                │           ├── Metrics subagent (Prometheus / PromQL)
                │           ├── Logs subagent    (Loki / LogQL)
                │           └── Events subagent  (kubectl events)
                │
                ├── HITL gate — destructive ops pause for approval
                └── Role check — admin / operator / readonly enforced
```

**Checkpointing**: conversation state persists to PostgreSQL (production) or SQLite (local).

### V4 platform layers (feature-flagged)

| | |
|---|---|
| **Zero-token detection** | compiled playbook predicates watch the cluster and flag known failures without any LLM call (`kq findings`) |
| **Autonomous investigations** | detector firings open their own investigations and publish reports — no question needed (`kq digest`) |
| **Memory hierarchy** | similar past episodes + a temporal knowledge graph of cluster changes are injected into answers |
| **Memory V5 upgrade** (experimental, off by default) | state-of-the-art-grounded, additive slices behind feature flags: hybrid recall, bi-temporal KG, blast-radius (PPR), write reconciliation, episode→rule→detector promotion, importance/prospective memory, MINJA-hardened writes, and a theme summary tree — with **OpsMemBench** to measure them |
| **Flight recorder** | every agent decision is hash-chained and replayable (`kq replay <session>`); mutations arm rollback points |

See the [docs](https://mskazemi.github.io/kubeintellect/) for configuration and the autonomy ladder (A0–A3).

---

## Authentication

Optional — if no keys are set, all requests are accepted.

```bash
# ~/.kubeintellect/.env  (written by kubeintellect init)
KUBEINTELLECT_ADMIN_KEYS=ki-admin-abc123
KUBEINTELLECT_OPERATOR_KEYS=ki-op-def456
KUBEINTELLECT_READONLY_KEYS=ki-ro-xyz789
```

Generate keys: `openssl rand -hex 20`

---

## Other deployment options

### Docker Compose (laptop / VM — no cluster required to run the server)

```bash
git clone https://github.com/MSKazemi/kubeintellect
cd kubeintellect
cp .env.example .env
```

Open `.env` and fill in three things:

```bash
# 1. LLM key — any provider: OpenAI, Azure, Qwen (DashScope), or Anthropic
OPENAI_API_KEY=sk-...          # LLM_PROVIDER=openai
#                              # or LLM_PROVIDER=qwen  + DASHSCOPE_API_KEY=sk-...
#                              # or LLM_PROVIDER=azure + AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT
#                              # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY=sk-ant-...

# 2. Database password
POSTGRES_PASSWORD=changeme     # use something stronger

# 3. Admin API key — generate one, then paste the same value as KUBE_Q_API_KEY below
KUBEINTELLECT_ADMIN_KEYS=ki-admin-$(openssl rand -hex 10)
```

```bash
docker compose up -d
pip install kube-q
```

```bash
# KUBE_Q_API_KEY = the value you set in KUBEINTELLECT_ADMIN_KEYS above
KUBE_Q_API_KEY=<your-admin-key> kq --url http://localhost:8000
```

Full guide: [Deploy: Docker Compose](https://mskazemi.github.io/kubeintellect/deploy/docker-compose/)

### Kubernetes — runs on any cloud

KubeIntellect is a standard Helm workload, so it deploys to **any Kubernetes cluster
on any cloud**, with the LLM provider chosen independently (OpenAI / Azure / Qwen /
Anthropic). Each cloud ships a ready-made values overlay + `make` target + runbook:

| Target cluster | One-liner | Guide |
|--------|-------------|-------|
| Local Kind (dev) | `make kind-deploy-kubeintellect` | [Kind](https://mskazemi.github.io/kubeintellect/deploy/kind/) |
| AWS EKS | `make aws-deploy-kubeintellect` | [AWS](https://mskazemi.github.io/kubeintellect/deploy/aws/) |
| Google Cloud GKE | `make gcp-deploy-kubeintellect` | [GCP](https://mskazemi.github.io/kubeintellect/deploy/gcp/) |
| Azure AKS / any cluster | `make aks-deploy-kubeintellect` | [Cloud / Helm](https://mskazemi.github.io/kubeintellect/deploy/cloud/) |
| Alibaba Cloud ACK | `make alibaba-deploy-kubeintellect` | [Alibaba](https://mskazemi.github.io/kubeintellect/deploy/alibaba/) |
| Alibaba ECS + k3s (cheapest) | `scripts/alibaba_ecs_k3s_bootstrap.sh` | [Alibaba](https://mskazemi.github.io/kubeintellect/deploy/alibaba/) |

See the [provider matrix](https://mskazemi.github.io/kubeintellect/deploy/cloud/) for
ingress class / managed DB / storage class per cloud.

Full guide: [Quickstart](https://mskazemi.github.io/kubeintellect/quickstart/)

---

## CLI reference

| Command | Purpose |
|---------|---------|
| `kubeintellect init` | Setup wizard — LLM key, cluster, observability, kube-q, systemd service |
| `kubeintellect serve` | Start the API server (default: `0.0.0.0:8000`) |
| `kubeintellect status` | Show config + connectivity for all components |
| `kubeintellect set KEY=VALUE` | Update a value in `~/.kubeintellect/.env` |
| `kubeintellect db-init` | Apply schema to PostgreSQL |
| `kubeintellect kind-setup` | Create a Kind cluster + DNS config |
| `kubeintellect service <action>` | Manage the systemd background service (`install` / `uninstall` / `start` / `stop` / `status` / `logs`) |

---

## Docs

| Topic | Link |
|-------|------|
| All install options | [Quickstart](https://mskazemi.github.io/kubeintellect/quickstart/) |
| pip — no cluster (quick try) | [Install: no cluster](https://mskazemi.github.io/kubeintellect/install/no-cluster/) |
| pip — existing cluster | [Install: existing cluster](https://mskazemi.github.io/kubeintellect/install/existing-cluster/) |
| pip — local Kind cluster | [Install: Kind](https://mskazemi.github.io/kubeintellect/install/kind/) |
| Docker Compose | [Deploy: Docker Compose](https://mskazemi.github.io/kubeintellect/deploy/docker-compose/) |
| Kind dev environment (repo) | [Deploy: Kind](https://mskazemi.github.io/kubeintellect/deploy/kind/) |
| VM / AKS / cloud (Helm) | [Deploy: cloud / Helm](https://mskazemi.github.io/kubeintellect/deploy/cloud/) |
| All config options | [Configuration reference](https://mskazemi.github.io/kubeintellect/configuration/) |
| Security model | [Security](https://mskazemi.github.io/kubeintellect/security/) |

---

## Repo layout

```
app/                        # core Python source (shared by all deployments)
deploy/
  docker-compose/           # monitoring configs (prometheus.yml, loki-config.yml, grafana)
  helm/
    kubeintellect/          # Helm chart + values for all environments
    langfuse/               # Langfuse LLM tracing chart
  kind/                     # Kind cluster configs
docker-compose.yaml         # laptop deployment entry point
scripts/
docs/
tests/
```

---

## v1 (LibreChat backend)

The original KubeIntellect used a LibreChat frontend with a LangGraph multi-agent backend (Supervisor → specialized worker agents, HITL checkpoints, dynamic tool generation). It is preserved on the [`v1-legacy`](https://github.com/MSKazemi/kubeintellect/tree/v1-legacy) branch.

---

## License

KubeIntellect is **dual-licensed**:

- **Open source — GNU AGPL-3.0-or-later** ([LICENSE](LICENSE)). You may use, modify, and
  self-host it freely. Because AGPL-3.0 includes the network-use clause, if you run a
  **modified** version as a network service you must make the modified source available to
  its users under the same license.
- **Commercial license** — if you want to use KubeIntellect (modified or not) in a closed-
  source or proprietary product/service without the AGPL obligations, a separate commercial
  license is available. Contact **mohsen.seyedkazemi@gmail.com**.

If you use KubeIntellect in academic work, please cite it — see [CITATION.cff](CITATION.cff).
