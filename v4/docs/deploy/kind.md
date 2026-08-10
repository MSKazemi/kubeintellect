---
description: >-
  Full KubeIntellect dev environment on Kind: 2-node cluster, hot-reload, monitoring stack, and Langfuse LLM tracing for contributors.
---

# Deploy: Kind — Full Dev Environment

Full local Kubernetes environment for developing KubeIntellect — 2-node cluster, hot-reload, monitoring, and Langfuse tracing.

**Requirements:** Docker. Everything else is installed automatically.

> **Two Makefiles.** Shared infrastructure (the Kind cluster, Prometheus/Grafana/Loki,
> and Langfuse) is managed from the **repo root** — one cluster + one observability stack
> serve every version. Per-version app build/deploy lives in `vN/` (this guide uses `v4`).
> So: **infra commands run from the repo root, app commands run from `cd v4`.**

---

## 1. Clone the repo

```bash
git clone https://github.com/mskazemi/kubeintellect
cd kubeintellect
```

---

## 2. Create the Kind cluster  *(repo root)*

```bash
make kind-cluster-create
```

Installs `kind`, `kubectl`, `helm` if missing. Creates a 2-node cluster with nginx ingress. Takes ~2 minutes.

---

## 3. Install monitoring (optional)  *(repo root)*

```bash
make monitoring-install       # Prometheus + Grafana + Loki → monitoring namespace
```

---

## 4. Provision + install Langfuse (optional)  *(repo root)*

```bash
cp .env.example .env          # root .env holds shared infra config
make langfuse-provision       # auto-creates ONE shared Langfuse project + API token,
                              # writes the keys into root .env AND every vN/.env
make langfuse-install         # deploys Langfuse, seeded with those keys
```

`langfuse-provision` replaces the old "create a project in the UI and copy the keys" step —
all versions share one project and are told apart by a per-version trace tag (`version:vN`).

---

## 5. Configure app secrets  *(v4)*

```bash
cd v4
cp .env.example .env
```

Edit `v4/.env` — required fields (the `LANGFUSE_*` keys were already filled in by
`make langfuse-provision`):
```bash
LLM_PROVIDER=azure            # or: openai
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
POSTGRES_PASSWORD=changeme
KUBEINTELLECT_ADMIN_KEYS=ki-admin-<run: openssl rand -hex 10>
# KI_VERSION=v4              # tags this version's Langfuse traces (default v4)
```

---

## 6. Build and deploy  *(v4)*

```bash
make kind-build-kubeintellect    # build Docker image + load into Kind
make kind-deploy-kubeintellect   # Helm install KubeIntellect (picks up LANGFUSE_* from v4/.env)
```

---

## 7. Add hostnames to /etc/hosts  *(repo root)*

```bash
make hosts-entry              # adds api.kubeintellect.local + langfuse/loki/prometheus.local (sudo)
```

---

## 8. Verify

```bash
curl http://api.kubeintellect.local/healthz    # → {"status":"ok"}
```

---

## 9. Connect  *(v4)*

```bash
make cli                      # opens kq REPL → http://api.kubeintellect.local
# or:
pipx install kube-q
KUBE_Q_API_KEY=<your-key> kq --url http://api.kubeintellect.local
```

---

## Services

| Service | URL | Notes |
|---------|-----|-------|
| KubeIntellect API | http://api.kubeintellect.local | |
| Langfuse trace UI | http://langfuse.local | login `admin@kubeintellect.local` / `langfuse-admin` |
| Prometheus | http://prometheus.local | |
| Grafana | `kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80` → http://localhost:3000 (`admin`/`admin`) | no ingress |
| Loki | *(no web UI)* | query logs via Grafana → Explore → Loki |

---

## After code changes  *(v4)*

```bash
make kind-build-kubeintellect    # rebuild image + reload into Kind (hot-reload picks up packages/kubeintellect-server/app/ changes)
```

---

## Full redeploy (wipe and restart)  *(v4)*

```bash
make kind-redeploy-kubeintellect    # uninstalls KubeIntellect and redeploys from scratch
```

---

## Teardown  *(repo root)*

```bash
make kind-cluster-cleanup     # deletes the entire Kind cluster (all versions + monitoring)
```

---

## VM variant (Kind on a headless server)

```bash
# infra (repo root)
make kind-cluster-create-vm       # no host mounts, no hot-reload
make monitoring-install           # optional
make langfuse-provision           # optional
make langfuse-install             # optional
# app (v4)
cd v4 && make kind-build-kubeintellect && make vm-deploy-kubeintellect
```
