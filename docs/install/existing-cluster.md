---
description: >-
  Connect KubeIntellect to your existing Kubernetes cluster (AKS, EKS, GKE, or any kubeconfig) with a simple pip install.
---

# Install: pip — Existing K8s Cluster

Connect KubeIntellect to a cluster you already have — company cluster, AKS, EKS, GKE, or any kubeconfig.

**Requirements:** Python 3.12+, `kubectl` configured with cluster access, an LLM API key.

---

## 1. Install

```bash
pip install kubeintellect
```

> If `kubeintellect` or `kq` are not found after install:
> ```bash
> echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
> ```

---

## 2. Set up — one command

```bash
kubeintellect init
```

The wizard walks you through:

| Prompt | What to enter |
|--------|--------------|
| LLM provider | `1` OpenAI or `2` Azure OpenAI |
| API key (and endpoint for Azure) | Your key |
| *(kubeconfig detected — no cluster creation offered)* | |
| Access level | `1` admin / `2` operator / `3` readonly |
| Database (if Docker available) | Press Enter — SQLite is the default |
| `PROMETHEUS_URL` | Your Prometheus endpoint (or Enter to skip) |
| `LOKI_URL` | Your Loki endpoint (or Enter to skip) |
| Install as background service? | **Y** — server starts on every login |

> **Access level guidance:**
> - `admin` — full access, all write operations (HITL-gated)
> - `operator` — create/scale/apply; no deletes or drains
> - `readonly` — queries only, no changes ← recommended for production clusters

When `init` finishes it:
- Writes `~/.kubeintellect/.env` with your settings
- Configures `kube-q` automatically (`~/.kube-q/.env`)
- Installs a systemd service so the server starts on every login
- Hands off to `kq` immediately

---

## 3. Connect

```bash
kq
```

No API key to copy — `init` configured it automatically.

---

## 4. Verify

```bash
kubeintellect status
```

Expected output (existing cluster):
```
  Config:    ✓  ~/.kubeintellect/.env
  LLM:       ✓  azure / gpt-4o
  DB:        ✓  sqlite  ~/.kubeintellect/kubeintellect.db
  kubectl:   ✓  found
  Kube:      ✓  ~/.kube/config  context: my-cluster
  Auth:      ✓  enabled
    admin     ki-admin-xxxxxxxxxxxxxxxxxxxx
  Prometheus:✓  http://prometheus.company.com  reachable
  Loki:      ✓  http://loki.company.com  reachable
  Grafana:   -  not configured
  Langfuse:  -  disabled
  kube-q:    ✓  found
```

---

## Update a config value

```bash
kubeintellect set PROMETHEUS_URL=https://prometheus.company.com
kubeintellect set LOKI_URL=https://loki.company.com
```

If the service is running it restarts automatically to apply changes.

---

## Switch to PostgreSQL

SQLite is the default. For production or team use, set a Postgres DSN:

```bash
kubeintellect set DATABASE_URL=postgresql://user:password@host:5432/dbname
kubeintellect db-init    # apply schema
kubeintellect service restart  # pick up new setting
```

---

## Managing the service

```bash
kubeintellect service status     # check if server is running
kubeintellect service logs       # tail live logs
kubeintellect service stop       # stop the server
kubeintellect service uninstall  # remove the service entirely
```

---

## Observability URLs

If your company runs Prometheus and Loki inside the cluster, use the external ingress URLs or ask your platform team:

```bash
kubeintellect set PROMETHEUS_URL=https://prometheus.company.com
kubeintellect set LOKI_URL=https://loki.company.com
```

For Langfuse LLM tracing:
```bash
kubeintellect set LANGFUSE_ENABLED=true
kubeintellect set LANGFUSE_HOST=https://langfuse.company.com
kubeintellect set LANGFUSE_PUBLIC_KEY=pk-lf-...
kubeintellect set LANGFUSE_SECRET_KEY=sk-lf-...
```
