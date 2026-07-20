---
description: >-
  The KubeIntellect container image — what it contains, how to build and push it,
  and how to configure it for Docker Compose vs Kubernetes.
---

# Container Image

## What the image is

The KubeIntellect image is a **stateless, config-free artifact**. Build it once, push it to a registry, and run it anywhere — the same image works in Docker Compose (local) and Kubernetes (Helm) by changing only the environment variables you inject into it.

**What is inside:**

| Included | Why |
|---|---|
| `app/` — Python application | The server itself |
| Python virtualenv | All dependencies, pre-installed and bytecode-compiled |
| `kubectl` binary | Pinned version, SHA256-verified at build time |
| `ca-certificates` | TLS verification for Azure OpenAI, LangSmith, Langfuse |

**What is NOT inside:**

| Excluded | Why |
|---|---|
| Docs, markdown, plans | Not needed at runtime |
| `.env` / secrets | Never bake credentials into an image |
| Helm charts, compose files, scripts | Deploy tooling belongs outside the image |
| Tests, build tools, `uv` | Build-stage only — stripped in the final image |

---

## Build

```bash
docker build -t kubeintellect:latest .
```

For CI/CD — pass image metadata so the OCI labels are meaningful:

```bash
docker build \
  --build-arg VERSION=$(git describe --tags --always) \
  --build-arg GIT_SHA=$(git rev-parse HEAD) \
  -t kubeintellect:latest .
```

Override the `kubectl` version (default: `v1.32.4`):

```bash
docker build --build-arg KUBECTL_VERSION=v1.33.0 -t kubeintellect:latest .
```

> Check the current stable version at: <https://dl.k8s.io/release/stable.txt>

---

## Push to a registry

```bash
# Tag for your registry
docker tag kubeintellect:latest ghcr.io/mskazemi/kubeintellect:latest

# Push
docker push ghcr.io/mskazemi/kubeintellect:latest
```

Once pushed, both `docker-compose.yaml` and the Helm chart reference this image by tag — no rebuild needed per environment.

---

## How configuration reaches the container

The image reads all settings from environment variables. It has no built-in defaults for secrets or service addresses — those must be supplied at runtime.

```
┌───────────────────────────┐     ┌───────────────────────────┐
│    Docker Compose         │     │    Kubernetes (Helm)      │
│                           │     │                           │
│  .env file                │     │  ConfigMap (non-secret)   │
│  ┌──────────────────────┐ │     │  ┌──────────────────────┐ │
│  │ LLM_PROVIDER=azure   │ │     │  │ LLM_PROVIDER=azure   │ │
│  │ PROMETHEUS_URL=...   │ │─────│  │ PROMETHEUS_URL=...   │ │
│  │ POSTGRES_PASSWORD=.. │ │     │  └──────────────────────┘ │
│  └──────────────────────┘ │     │                           │
│                           │     │  Secret (sensitive)       │
│  env: block               │     │  ┌──────────────────────┐ │
│  ┌──────────────────────┐ │     │  │ AZURE_OPENAI_API_KEY │ │
│  │ POSTGRES_HOST=       │ │     │  │ POSTGRES_PASSWORD=.. │ │
│  │   postgres           │ │     │  └──────────────────────┘ │
│  └──────────────────────┘ │     │                           │
└───────────────────────────┘     └───────────────────────────┘
              │                                 │
              └─────────────┬───────────────────┘
                            ▼
              ┌─────────────────────────┐
              │   kubeintellect:latest  │
              │  (same image, always)   │
              └─────────────────────────┘
```

---

## Kubernetes access

This is the most important configuration difference between the two deployment modes.

### Docker Compose — kubeconfig file

The container reads a kubeconfig file that is mounted from the host:

```yaml
# docker-compose.yaml
services:
  kubeintellect:
    volumes:
      - ${KUBECONFIG:-~/.kube/config}:/home/app/.kube/config:ro
```

The app user's home directory is `/home/app`. When `KUBECONFIG_PATH` is not set, the app defaults to `~/.kube/config` which resolves to `/home/app/.kube/config` — exactly where the volume is mounted.

To use a different kubeconfig:
```bash
KUBECONFIG=/path/to/other/config docker compose up -d
```

### Kubernetes (Helm) — ServiceAccount token

No kubeconfig file is needed. The Helm chart:

1. Creates a `ServiceAccount` (`kubeintellect-sa`) bound to RBAC roles you configure
2. Sets `KUBECONFIG_PATH: ""` in the ConfigMap

When `KUBECONFIG_PATH` is empty, `kubectl` skips kubeconfig file loading and uses the pod's mounted ServiceAccount token at `/var/run/secrets/kubernetes.io/serviceaccount/token` automatically.

RBAC scope is controlled in `values.yaml`:

```yaml
rbac:
  clusterAdmin: false         # full cluster admin — use only in dev
  createNamespaced: true      # read within specific namespaces
  createClusterReadOnly: true # read-only across all namespaces
  createClusterOps: false     # write ops (delete/patch/scale) — enable per env, always HITL-gated
  allowExec: false            # pods/exec — off by default to protect secrets
```

---

## Observability (optional)

Leave any of these empty — the app works without them. `kubectl`-based queries are always available regardless.

### Prometheus

Enables PromQL metric queries through the agent.

| Mode | Value |
|---|---|
| Docker Compose | `PROMETHEUS_URL=http://localhost:9090` |
| Kubernetes | `config.prometheusUrl: http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090` |

### Loki

Enables LogQL log queries through the agent.

| Mode | Value |
|---|---|
| Docker Compose | `LOKI_URL=http://localhost:3100` |
| Kubernetes | `config.lokiUrl: http://loki.monitoring.svc.cluster.local:3100` |

### Langfuse (LLM tracing)

Sends every LLM call to Langfuse so you can inspect prompts, token counts, and latency.

| Variable | docker-compose | Kubernetes |
|---|---|---|
| `LANGFUSE_ENABLED` | `LANGFUSE_ENABLED=true` in `.env` | `config.langfuseEnabled: true` |
| `LANGFUSE_HOST` | `LANGFUSE_HOST=http://localhost:3001` | `config.langfuseHost: http://langfuse-web.monitoring.svc.cluster.local:3000` |
| `LANGFUSE_PUBLIC_KEY` | In `.env` | In Helm `secrets.langfusePublicKey` |
| `LANGFUSE_SECRET_KEY` | In `.env` | In Helm `secrets.langfuseSecretKey` |

---

## All environment variables at a glance

These are the variables the container reads. For full details on each, see the [Configuration Reference](../configuration.md).

=== "Kubernetes access"

    | Variable | Default | Notes |
    |---|---|---|
    | `KUBECONFIG_PATH` | `~/.kube/config` | Empty string = in-cluster ServiceAccount (Helm) |
    | `KUBECTL_TIMEOUT_SECONDS` | `30` | Read operations |
    | `KUBECTL_DESTRUCTIVE_TIMEOUT_SECONDS` | `300` | Write operations |
    | `KUBECTL_BLOCKED_NAMESPACES` | `kubeintellect,monitoring,kube-system,...` | Agent can never touch these |
    | `KUBECTL_BLOCKED_RESOURCES` | `secret,secrets,serviceaccount,...` | Agent can never access these |

=== "LLM provider"

    | Variable | Default | Notes |
    |---|---|---|
    | `LLM_PROVIDER` | `azure` | `azure` or `openai` |
    | `AZURE_OPENAI_API_KEY` | — | Required for `azure` |
    | `AZURE_OPENAI_ENDPOINT` | — | Required for `azure` |
    | `AZURE_OPENAI_API_VERSION` | `2024-02-01` | |
    | `AZURE_COORDINATOR_DEPLOYMENT` | `gpt-4o` | |
    | `AZURE_SUBAGENT_DEPLOYMENT` | `gpt-4o-mini` | |
    | `OPENAI_API_KEY` | — | Required for `openai` |
    | `OPENAI_COORDINATOR_MODEL` | `gpt-4o` | |
    | `OPENAI_SUBAGENT_MODEL` | `gpt-4o-mini` | |

=== "Database"

    | Variable | Default | Notes |
    |---|---|---|
    | `DATABASE_URL` | — | Full DSN — overrides all `POSTGRES_*` vars |
    | `POSTGRES_HOST` | `localhost` | `postgres` in docker-compose |
    | `POSTGRES_PORT` | `5432` | |
    | `POSTGRES_DB` | `kubeintellect` | |
    | `POSTGRES_USER` | `kubeintellect` | |
    | `POSTGRES_PASSWORD` | — | **Required — no default in production** |

=== "Observability"

    | Variable | Default | Notes |
    |---|---|---|
    | `PROMETHEUS_URL` | `""` | Leave empty to disable |
    | `LOKI_URL` | `""` | Leave empty to disable |
    | `LANGFUSE_ENABLED` | `false` | |
    | `LANGFUSE_HOST` | `""` | |
    | `LANGFUSE_PUBLIC_KEY` | — | |
    | `LANGFUSE_SECRET_KEY` | — | |

=== "Auth & App"

    | Variable | Default | Notes |
    |---|---|---|
    | `KUBEINTELLECT_ADMIN_KEYS` | `""` | Comma-separated bearer tokens |
    | `KUBEINTELLECT_OPERATOR_KEYS` | `""` | Medium-risk ops only |
    | `KUBEINTELLECT_READONLY_KEYS` | `""` | Read-only |
    | `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
    | `LOG_FORMAT` | `text` | `text` or `json` |
    | `ALLOWED_ORIGINS` | `http://localhost:3080` | CORS — comma-separated |

---

## Security properties of the image

- Runs as non-root user `app` (UID/GID 1001) — enforced at the OS level
- `PYTHONDONTWRITEBYTECODE=1` — no `.pyc` writes at runtime
- No shell, no package manager, no build tools in the final image
- `curl` is never in the runtime image — kubectl is fetched in a separate build stage and copied as a binary only
- All TLS certificates updated from Debian's `ca-certificates` package at build time
