---
description: >-
  Full KubeIntellect dev environment on Kind: 2-node cluster, hot-reload, monitoring stack, and Langfuse LLM tracing for contributors.
---

# Deploy: Kind — Full Dev Environment

Full local Kubernetes environment for developing KubeIntellect — 2-node cluster, hot-reload, monitoring, and Langfuse tracing.

**Requirements:** Docker. Everything else is installed automatically.

---

## 1. Clone the repo

```bash
git clone https://github.com/mskazemi/kubeintellect
cd kubeintellect
```

---

## 2. Create the Kind cluster

```bash
make kind-cluster-create
```

Installs `kind`, `kubectl`, `helm` if missing. Creates a 2-node cluster with nginx ingress. Takes ~2 minutes.

---

## 3. Configure secrets

```bash
cp .env.example .env
```

Edit `.env` — required fields:
```bash
LLM_PROVIDER=azure            # or: openai
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
POSTGRES_PASSWORD=changeme
KUBEINTELLECT_ADMIN_KEYS=ki-admin-<run: openssl rand -hex 10>
```

---

## 4. Install monitoring (optional)

```bash
make monitoring-install       # Prometheus + Grafana + Loki → monitoring namespace
```

---

## 5. Install Langfuse LLM tracing (optional)

```bash
make langfuse-install         # Langfuse → monitoring namespace
```

---

## 6. Build and deploy

```bash
make kind-build-kubeintellect    # build Docker image + load into Kind
make kind-deploy-kubeintellect   # Helm install KubeIntellect
```

---

## 7. Add hostnames to /etc/hosts

```bash
make hosts-entry              # adds api.kubeintellect.local + langfuse.local (requires sudo)
```

---

## 8. Verify

```bash
curl http://api.kubeintellect.local/healthz    # → {"status":"ok"}
```

---

## 9. Connect

```bash
make cli                      # opens kq REPL → http://api.kubeintellect.local
# or:
pipx install kube-q
KUBE_Q_API_KEY=<your-key> kq --url http://api.kubeintellect.local
```

---

## Services

| Service | URL |
|---------|-----|
| KubeIntellect API | http://api.kubeintellect.local |
| Langfuse trace UI | http://langfuse.local |

Langfuse default credentials: `admin@local.dev` / `changeme`

---

## After code changes

```bash
make kind-build-kubeintellect    # rebuild image + reload into Kind (hot-reload picks up app/ changes)
```

---

## Full redeploy (wipe and restart)

```bash
make kind-redeploy-kubeintellect    # uninstalls KubeIntellect and redeploys from scratch
```

---

## Teardown

```bash
make kind-cluster-cleanup     # deletes the entire Kind cluster
```

---

## VM variant (Kind on a headless server)

```bash
make kind-cluster-create-vm       # no host mounts, no hot-reload
make kind-build-kubeintellect
make monitoring-install           # optional
make langfuse-install             # optional
make vm-deploy-kubeintellect
```
