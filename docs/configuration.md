---
description: >-
  Complete reference for all KubeIntellect environment variables — LLM provider, authentication, database, kubectl settings, and observability.
---

# Configuration Reference

All KubeIntellect settings are environment variables. They can be set in:

- `~/.kubeintellect/.env` — written by `kubeintellect init`; used for pip installs
- `.env` in the project directory — takes precedence, used for dev overrides
- Helm `values.yaml` (in-cluster deploy) — maps to a Kubernetes Secret and ConfigMap
- Shell environment — highest priority, overrides all files

**Quickest way to change a value:**

```bash
kubeintellect set KEY=VALUE          # updates ~/.kubeintellect/.env; restarts service if active
kubeintellect set A=1 B=2 C=3        # multiple values at once
```

---

## LLM provider

| Variable | Default | Values | Description |
|---|---|---|---|
| `LLM_PROVIDER` | `azure` | `openai` \| `azure` | Which LLM backend to use |

**OpenAI:**

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | — | Your OpenAI API key |
| `OPENAI_COORDINATOR_MODEL` | `gpt-4o` | Model for the coordinator agent |
| `OPENAI_SUBAGENT_MODEL` | `gpt-4o-mini` | Model for domain subagents |

**Azure OpenAI:**

| Variable | Default | Description |
|---|---|---|
| `AZURE_OPENAI_API_KEY` | — | Azure OpenAI API key |
| `AZURE_OPENAI_ENDPOINT` | — | Resource endpoint — must include protocol: `https://....openai.azure.com/` |
| `AZURE_OPENAI_API_VERSION` | `2024-02-01` | API version |
| `AZURE_COORDINATOR_DEPLOYMENT` | `gpt-4o` | Deployment name for coordinator |
| `AZURE_SUBAGENT_DEPLOYMENT` | `gpt-4o-mini` | Deployment name for subagents |

---

## Database

KubeIntellect supports PostgreSQL (production) and SQLite (local/no-Docker).

`kubeintellect serve` auto-detects which to use — manual configuration is only needed to override the defaults.

### PostgreSQL

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | — | Full DSN — overrides all `POSTGRES_*` vars |
| `POSTGRES_HOST` | `localhost` | DB host (`postgres` when using docker-compose) |
| `POSTGRES_PORT` | `5432` | DB port |
| `POSTGRES_DB` | `kubeintellect` | Database name |
| `POSTGRES_USER` | `kubeintellect` | DB user |
| `POSTGRES_PASSWORD` | `password` | **Change this** |
| `POSTGRES_POOL_MIN_CONN` | `1` | Connection pool minimum |
| `POSTGRES_POOL_MAX_CONN` | `10` | Connection pool maximum |

### SQLite (local fallback)

| Variable | Default | Description |
|---|---|---|
| `USE_SQLITE` | `false` | Force SQLite mode (auto-set by `kubeintellect serve` if no postgres) |
| `SQLITE_PATH` | `~/.kubeintellect/kubeintellect.db` | Path to SQLite database file |

SQLite is included in `pip install kubeintellect` — no extra needed. State persists to disk across restarts.
Not used in Helm deployments (`DATABASE_URL` is always set there).

---

## Kubernetes access

KubeIntellect uses `kubectl` to interact with your cluster. `kubectl` must be installed and on PATH.

| Variable | Default | Description |
|---|---|---|
| `KUBECONFIG_PATH` | `~/.kube/config` | Path to kubeconfig file |
| `KUBECTL_TIMEOUT_SECONDS` | `30` | Timeout for read operations |
| `KUBECTL_DESTRUCTIVE_TIMEOUT_SECONDS` | `300` | Timeout for write operations |
| `KUBECTL_BLOCKED_NAMESPACES` | see below | Namespaces the agent will never touch |
| `KUBECTL_BLOCKED_RESOURCES` | `secret,secrets,...` | Resource types always blocked |

**Laptop/pip mode**: uses your local `~/.kube/config` — no in-cluster setup needed.
**In-cluster (Helm) mode**: uses the mounted ServiceAccount — `KUBECONFIG_PATH` is ignored.

Default blocked namespaces (safety fence):
```
kubeintellect, monitoring, kube-system, kube-public, kube-node-lease, ingress-nginx, cert-manager
```

---

## Authentication (RBAC)

Auth is optional. If no keys are set, all requests are accepted (open access).

| Variable | Default | Description |
|---|---|---|
| `KUBEINTELLECT_ADMIN_KEYS` | `""` | Comma-separated bearer tokens — full access, HITL-gated writes |
| `KUBEINTELLECT_OPERATOR_KEYS` | `""` | Medium-risk ops only; delete/drain blocked |
| `KUBEINTELLECT_READONLY_KEYS` | `""` | Read-only; all writes rejected |

Generate a key: `openssl rand -hex 20`

Clients set the key via `Authorization: Bearer <key>` header, or `KUBE_Q_API_KEY` env var in kube-q.

---

## kube-q CLI (set on the client machine)

| Variable | Default | Description |
|---|---|---|
| `KUBE_Q_URL` | `http://localhost:8000` | Backend URL |
| `KUBE_Q_API_KEY` | — | Bearer token (must match one of the `*_KEYS` above) |

```bash
mkdir -p ~/.kube-q
echo "KUBE_Q_URL=http://localhost:8000" >> ~/.kube-q/.env
echo "KUBE_Q_API_KEY=ki-admin-xxx"     >> ~/.kube-q/.env
```

---

## Observability (optional)

| Variable | Default | Description |
|---|---|---|
| `PROMETHEUS_URL` | `""` | Prometheus endpoint; enables PromQL queries |
| `LOKI_URL` | `""` | Loki endpoint; enables LogQL queries |
| `GRAFANA_URL` | `""` | Grafana endpoint; shown in `kubeintellect status` |

`kubeintellect init` sets these automatically when you choose to install the observability stack (NodePort URLs on a Kind cluster). For in-cluster deployments they are set via Helm `values.yaml`.

If empty, kubectl-based queries still work — only metrics and log queries are unavailable.

---

## LLM tracing with Langfuse (optional)

| Variable | Default | Description |
|---|---|---|
| `LANGFUSE_ENABLED` | `false` | Enable Langfuse tracing |
| `LANGFUSE_PUBLIC_KEY` | — | Langfuse project public key |
| `LANGFUSE_SECRET_KEY` | — | Langfuse project secret key |
| `LANGFUSE_HOST` | `http://langfuse-web...` | Langfuse server URL |

Install the tracing extra:
```bash
pip install 'kubeintellect[tracing]'
```

---

## App settings

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FORMAT` | `text` | `text` or `json` |
| `DEBUG` | `false` | Enable FastAPI debug mode |
| `ALLOWED_ORIGINS` | `http://localhost:3080` | CORS allowed origins (comma-separated) |
