---
description: >-
  Deploy KubeIntellect with Docker Compose — full local stack with PostgreSQL, Prometheus, Grafana, Loki, and optional Langfuse LLM tracing.
---

# Deploy: Docker Compose — Local Full Stack

Run KubeIntellect on your laptop with Docker Compose. No Kubernetes cluster needed — KubeIntellect connects to your cluster via `~/.kube/config` from the host.

**Requirements:** Docker (with Compose v2), a kubeconfig with cluster access, an LLM API key.

---

## 1. Clone the repo

```bash
git clone https://github.com/mskazemi/kubeintellect-v2
cd kubeintellect-v2
```

---

## 2. Configure

```bash
cp .env.example .env
```

Edit `.env` — required fields:

```bash
# LLM — choose one:
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...

# or Azure:
# LLM_PROVIDER=azure
# AZURE_OPENAI_API_KEY=...
# AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
# AZURE_OPENAI_DEPLOYMENT=gpt-4o

# Database (local only)
POSTGRES_PASSWORD=changeme

# API auth key (generate one):
KUBEINTELLECT_ADMIN_KEYS=ki-admin-<run: openssl rand -hex 10>
```

---

## 3. Start

```bash
docker compose up -d
```

Verify:
```bash
curl http://localhost:8000/healthz    # → {"status":"ok"}
```

---

## 4. Connect

```bash
pipx install kube-q
KUBE_Q_API_KEY=<your-admin-key> kq --url http://localhost:8000
```

> If `kq` is not found: `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc`

---

## 5. Add monitoring (optional)

```bash
docker compose --profile monitoring up -d
```

Adds Prometheus + Grafana + Loki locally.

Then add to `.env`:
```bash
PROMETHEUS_URL=http://localhost:9090
LOKI_URL=http://localhost:3100
```

Restart to pick up:
```bash
docker compose up -d
```

Grafana: http://localhost:3000 — pre-wired with Prometheus + Loki datasources.

---

## 6. Add Langfuse LLM tracing (optional)

```bash
docker compose --profile tracing up -d
```

Visit http://localhost:3001 → create account → Settings → API Keys.

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

## Everything at once

```bash
docker compose --profile monitoring --profile tracing up -d
```

---

## Useful commands

```bash
docker compose ps                   # check service status
docker compose logs -f kubeintellect  # tail app logs
docker compose down                 # stop everything
```

---

## Use a different kubeconfig

```bash
KUBECONFIG=/path/to/other/config docker compose up -d
```
