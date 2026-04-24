# KubeIntellect

[![PyPI](https://img.shields.io/pypi/v/kubeintellect.svg)](https://pypi.org/project/kubeintellect/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-mskazemi.github.io-0075C4?logo=materialformkdocs&logoColor=white)](https://mskazemi.github.io/kubeintellect/)

AI-powered Kubernetes management. Natural-language interface to diagnose faults, query cluster state, and execute remediation — with human approval gating all destructive actions.

**[Website](https://kubeintellect.com/)** · **[Live Demo](https://kubeintellect.com/demo)** · **[Docs](https://mskazemi.github.io/kubeintellect/)** · **[v1 (LibreChat backend)](https://github.com/MSKazemi/kubeintellect/tree/v1-legacy)**

---

## Quickstart

### Install (requires Python 3.12+)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install kubeintellect
```

> **Ubuntu 22.04** ships Python 3.10. Check your version first:
> ```bash
> python3 --version   # need 3.12 or higher
> ```
> If you have 3.10, install 3.12 from the deadsnakes PPA:
> ```bash
> sudo add-apt-repository ppa:deadsnakes/ppa -y
> sudo apt-get install -y python3.12
> python3.12 -m venv .venv && source .venv/bin/activate
> pip install kubeintellect
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

**[kube-q](https://github.com/MSKazemi/kube_q)** is the CLI that talks to KubeIntellect. Install it separately and point it at any running instance.

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

| Option | When to use |
|--------|-------------|
| [Docker Compose](docs/deploy-docker-compose.md) | Laptop, no K8s cluster, full stack via Docker |
| [Kind cluster](docs/deploy-kind.md) | Local K8s dev with monitoring + Langfuse |
| [Cloud / VM (Helm)](docs/deploy-cloud.md) | Production, AKS, or company cluster |

Full guide: [kubeintellect.com/quickstart](https://kubeintellect.com/quickstart/)

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
| All install options | [kubeintellect.com/quickstart](https://kubeintellect.com/quickstart/) |
| pip — no cluster (quick try) | [install-pip-no-cluster](https://kubeintellect.com/install-pip-no-cluster/) |
| pip — existing cluster | [install-pip-existing-cluster](https://kubeintellect.com/install-pip-existing-cluster/) |
| pip — local Kind cluster | [install-pip-kind](https://kubeintellect.com/install-pip-kind/) |
| Docker Compose | [deploy-docker-compose](https://kubeintellect.com/deploy-docker-compose/) |
| Kind dev environment (repo) | [deploy-kind](https://kubeintellect.com/deploy-kind/) |
| VM / AKS / cloud (Helm) | [deploy-cloud](https://kubeintellect.com/deploy-cloud/) |
| All config options | [configuration](https://kubeintellect.com/configuration/) |
| Security model | [security](https://kubeintellect.com/security/) |

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

AGPL-3.0. Commercial licenses available — see [LICENSE-COMMERCIAL.md](LICENSE-COMMERCIAL.md).
