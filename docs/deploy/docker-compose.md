---
description: >-
  Deploy KubeIntellect with Docker Compose — full local stack with PostgreSQL,
  Prometheus, Grafana, Loki, and optional Langfuse LLM tracing.
---

# Deploy: Docker Compose

Run the full KubeIntellect stack on your laptop with a single command. No Kubernetes cluster is needed to run the server — KubeIntellect connects to your cluster via the kubeconfig on your host machine.

**Requirements:** Docker (with Compose v2), a kubeconfig with cluster access, an LLM API key.

---

## How it works

`docker compose up` builds the KubeIntellect image from the local `Dockerfile` and starts these services:

| Service | Always? | What it is |
|---|---|---|
| `kubeintellect` | ✓ core | The KubeIntellect API server |
| `postgres` | ✓ core | Database for conversation memory and audit log |
| `prometheus` | `--profile monitoring` | Metrics collection |
| `loki` | `--profile monitoring` | Log aggregation |
| `grafana` | `--profile monitoring` | Dashboard UI — pre-wired with Prometheus + Loki |
| `langfuse` | `--profile tracing` | LLM call tracing UI |

**Postgres is always included** — you don't need to install or configure it separately.

**Monitoring and Langfuse are optional.** If your Kubernetes cluster already has Prometheus/Loki running, skip `--profile monitoring` and point `PROMETHEUS_URL` / `LOKI_URL` at your existing endpoints instead. If you don't need LLM call tracing, skip `--profile tracing` entirely.

All configuration is read from the `.env` file — nothing is baked into the image.

---

## 1. Clone the repository

```bash
git clone https://github.com/mskazemi/kubeintellect
cd kubeintellect
```

---

## 2. Configure

```bash
cp .env.example .env
```

Open `.env`. The file is fully documented — you only need to fill in **three things** to get started:

```bash
# 1. LLM provider — choose one:

# OpenAI:
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...

# Azure OpenAI:
# LLM_PROVIDER=azure
# AZURE_OPENAI_API_KEY=...
# AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/

# 2. Database password (Postgres is started automatically by Compose):
POSTGRES_PASSWORD=changeme          # use something stronger than this

# 3. An API key so kube-q can authenticate:
KUBEINTELLECT_ADMIN_KEYS=ki-admin-$(openssl rand -hex 10)
```

> **Kubeconfig:** By default the container reads `~/.kube/config` from your host.
> To use a different file: `KUBECONFIG=/path/to/config docker compose up -d`

> **Tip:** `.env` is already in `.gitignore` — it will never be committed.

---

## 3. Start

```bash
docker compose up -d
```

The first run builds the image from source — this takes ~2 minutes. Subsequent starts are instant.

Verify the server is healthy:

```bash
curl http://localhost:8000/healthz
# → {"status":"ok"}
```

---

## 4. Install kube-q and connect

```bash
pip install kube-q
KUBE_Q_API_KEY=<your-admin-key> kq --url http://localhost:8000
```

> If `kq` is not found after install:
> ```bash
> echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
> ```

---

## 5. Add monitoring (optional)

**Option A — your cluster already has Prometheus/Loki:**

Skip `--profile monitoring`. Just add the URLs to `.env` and restart:

```bash
PROMETHEUS_URL=https://prometheus.your-company.com
LOKI_URL=https://loki.your-company.com
```

```bash
docker compose up -d
```

**Option B — spin up a local Prometheus/Loki/Grafana with Compose:**

```bash
docker compose --profile monitoring up -d
```

Then add to `.env`:

```bash
PROMETHEUS_URL=http://localhost:9090
LOKI_URL=http://localhost:3100
```

Restart to pick up the new values:

```bash
docker compose up -d
```

Grafana is available at <http://localhost:3000> — datasources are pre-configured.

---

## 6. Add Langfuse LLM tracing (optional)

Starts a self-hosted Langfuse instance for inspecting every LLM call the agent makes:

```bash
docker compose --profile tracing up -d
```

Visit <http://localhost:3001> → create an account → **Settings → API Keys**.

Add to `.env`:

```bash
LANGFUSE_ENABLED=true
LANGFUSE_HOST=http://localhost:3001
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

Restart:

```bash
docker compose up -d
```

---

## Start everything at once

```bash
docker compose --profile monitoring --profile tracing up -d
```

---

## Use a different kubeconfig

The host kubeconfig is mounted read-only at `/home/app/.kube/config` inside the container.
To use a different kubeconfig:

```bash
KUBECONFIG=/path/to/other/config docker compose up -d
```

---

## Useful commands

```bash
docker compose ps                        # check service status
docker compose logs -f kubeintellect     # tail application logs
docker compose down                      # stop and remove containers
docker compose down -v                   # stop and remove containers + volumes (wipes data)
```
