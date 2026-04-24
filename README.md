# KubeIntellect V2

AI-powered Kubernetes management. Natural-language interface to diagnose faults, query cluster state, and execute remediation — with human approval gating all destructive actions.

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

### Other deployment options

| Option | When to use |
|--------|-------------|
| [Docker Compose](docs/deploy-docker-compose.md) | Laptop, no K8s cluster, full stack via Docker |
| [Kind cluster](docs/deploy-kind.md) | Local K8s dev with monitoring + Langfuse |
| [Cloud / VM (Helm)](docs/deploy-cloud.md) | Production, AKS, or company cluster |

Full guide: [docs/quickstart.md](docs/quickstart.md)

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
  kind/create-kind-cluster.sh
  vm/setup-nginx.sh, setup-tls.sh
tests/
docs/
```

---

## `kubeintellect kind-setup` vs `make kind-cluster-create`

Two ways to get a local Kind cluster — pick based on who you are:

| | `kubeintellect kind-setup` | `make kind-cluster-create` |
|---|---|---|
| Requires repo clone | No | Yes |
| Cluster config | Single-node | 2-node, hot-reload mounts |
| Ingress | Basic nginx | Tuned for Kind dev |
| Cluster DNS auto-config | Yes — `svc.cluster.local` works from host | No |
| Monitoring / Langfuse | Via `make` targets (after cloning) | `make monitoring-install` / `make langfuse-install` |
| Who it's for | End users, ops teams | KubeIntellect developers |

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

| Topic | File |
|---|---|
| All install options | [docs/quickstart.md](docs/quickstart.md) |
| pip — no cluster (quick try) | [docs/install-pip-no-cluster.md](docs/install-pip-no-cluster.md) |
| pip — existing cluster | [docs/install-pip-existing-cluster.md](docs/install-pip-existing-cluster.md) |
| pip — local Kind cluster | [docs/install-pip-kind.md](docs/install-pip-kind.md) |
| Docker Compose | [docs/deploy-docker-compose.md](docs/deploy-docker-compose.md) |
| Kind dev environment (repo) | [docs/deploy-kind.md](docs/deploy-kind.md) |
| VM / AKS / cloud (Helm) | [docs/deploy-cloud.md](docs/deploy-cloud.md) |
| All config options | [docs/configuration.md](docs/configuration.md) |
