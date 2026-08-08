---
description: >-
  Deploy KubeIntellect to AKS, EKS, GKE, or any Kubernetes cluster using Helm — production-ready with RBAC, secrets, ingress, and HPA.
---

# Deploy: Cloud / VM (Helm)

KubeIntellect is a standard Helm-deployed workload, so it runs on **any Kubernetes
cluster on any cloud**. The LLM provider is fully decoupled from where you deploy —
mix and match freely.

**Requirements:** `kubectl` pointing at your cluster, `helm`, Docker (to build/mirror the image).

## Provider matrix — deploy anywhere

Every cloud ships a ready-made values overlay, a `make` target, and a runbook. Pick
the LLM provider independently (`azure` | `openai` | `qwen` | `anthropic`).

| Cloud | Values overlay | `make` target | Runbook | Ingress | Managed DB | Storage class |
|---|---|---|---|---|---|---|
| **AWS EKS** | `values-aws.yaml.example` | `aws-deploy-kubeintellect` | [aws.md](aws.md) | `alb` | RDS PostgreSQL | `gp3` |
| **GCP GKE** | `values-gcp.yaml.example` | `gcp-deploy-kubeintellect` | [gcp.md](gcp.md) | `gce` | Cloud SQL | `premium-rwo` |
| **Azure AKS** | `values-aks.yaml.example` / `values-cloud.yaml.example` | `aks-deploy-kubeintellect` | this page (Option B) | `nginx`/`agic` | Azure DB for PostgreSQL | `managed-premium` |
| **Alibaba ACK** | `values-alibaba.yaml.example` | `alibaba-deploy-kubeintellect` | [alibaba.md](alibaba.md) | `nginx`/`alb` | ApsaraDB RDS | `alicloud-disk-essd` |
| **Alibaba ECS + k3s** (cheapest) | `values-ecs-k3s.yaml.example` | `scripts/alibaba_ecs_k3s_bootstrap.sh` | [alibaba.md](alibaba.md#lighter-alternative-single-ecs-k3s-the-coupon-friendly-path) | none (SSH tunnel) | in-cluster | `local-path` |
| **Any k8s / on-prem** | `values-cloud.yaml.example` | `aks-deploy-kubeintellect` | this page (Option B) | `nginx` | your choice | cluster default |

All overlays pull the same image (`ghcr.io/mskazemi/kubeintellect`, mirrorable to
ECR / Artifact Registry / ACR) and use the same chart; only registry, ingress class,
storage class, and managed-DB wiring differ. The chart's `job-db-init` applies the
schema on install, so no manual `psql` step is needed.

---

## Option A — Azure VM (Kind on VM)

Run a Kind cluster on a Linux VM and expose it via nginx + TLS.

### 1. Clone the repo

```bash
git clone https://github.com/mskazemi/kubeintellect
cd kubeintellect
```

### 2. Create the Kind cluster

The Kind cluster is shared infra, created from the **repo root**:

```bash
make kind-cluster-create-vm    # auto-installs kind/kubectl/helm; creates single-node cluster
```

### 3. Configure

```bash
cp deploy/helm/kubeintellect/values-production.yaml.example \
   deploy/helm/kubeintellect/values-production.yaml
```

Edit `values-production.yaml`:
```yaml
ingress:
  hosts:
    - api.your-domain.com      # internet access — your public domain
    - api.kubeintellect.local  # local VM access (add to /etc/hosts on clients)

config:
  llmProvider: azure          # or: openai
  prometheusUrl: ""           # add after monitoring install
  lokiUrl: ""
```

!!! note "Dual hostname setup"
    The VM nginx reverse proxy passes the original `Host` header through to the Kind cluster, so both hostnames route to the same pod. Add `api.kubeintellect.local` to `/etc/hosts` on any machine that needs local access without going through DNS.

```bash
cp .env.example .env
```

Edit `.env`:
```bash
LLM_PROVIDER=azure
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
POSTGRES_PASSWORD=changeme
KUBEINTELLECT_ADMIN_KEYS=ki-admin-<run: openssl rand -hex 10>
```

### 4. Build and deploy

The app build/deploy targets are per-version — run them from `v4`:

```bash
cd v4
make kind-build-kubeintellect    # build Docker image + load into Kind
make vm-deploy-kubeintellect     # helm install KubeIntellect
```

### 5. Install monitoring (optional)

Shared infra (monitoring + Langfuse) is installed from the **repo root**, while the app is deployed from `v4`:

```bash
# from the repo root:
make monitoring-install    # Prometheus + Grafana + Loki → monitoring namespace
make langfuse-provision     # auto-create the shared Langfuse project + API token
make langfuse-install       # Langfuse LLM tracing → monitoring namespace
```

Then update `values-production.yaml`:
```yaml
config:
  prometheusUrl: http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090
  lokiUrl: http://loki.monitoring.svc.cluster.local:3100
```

Redeploy (from `v4`): `make vm-deploy-kubeintellect`

### 6. Set up TLS

```bash
bash scripts/vm/setup-nginx.sh    # configure nginx reverse proxy
bash scripts/vm/setup-tls.sh      # get Let's Encrypt cert
```

### 7. Verify

```bash
curl https://api.your-domain.com/healthz           # → {"status":"ok"}  (internet)
curl http://api.kubeintellect.local/healthz         # → {"status":"ok"}  (local, if /etc/hosts set)
```

---

## Option B — AKS / EKS / GKE

### 1. Clone the repo

```bash
git clone https://github.com/mskazemi/kubeintellect
cd kubeintellect
```

### 2. Configure

```bash
cp deploy/helm/kubeintellect/values-cloud.yaml.example \
   deploy/helm/kubeintellect/values-cloud.yaml
```

Edit `values-cloud.yaml`:
```yaml
ingress:
  hosts:
    - api.your-domain.com    # ← your public domain
  className: nginx          # or: alb (EKS), gce (GKE)

config:
  llmProvider: azure        # or: openai
  prometheusUrl: https://prometheus.company.com
  lokiUrl: https://loki.company.com

postgres:
  external:
    url: postgresql://user:pass@host:5432/dbname    # use managed DB in production
```

```bash
cp .env.example .env
# Edit .env: API keys, POSTGRES_PASSWORD, KUBEINTELLECT_ADMIN_KEYS
```

### 3. Deploy

The app deploy target is per-version — run it from `v4`:

```bash
cd v4
make aks-deploy-kubeintellect    # helm upgrade --install
```

### 4. Verify

```bash
curl https://api.your-domain.com/healthz    # → {"status":"ok"}
```

---

## Automated deploys (GitHub Actions)

Push to `main` → image is built and pushed to GHCR automatically.

Manual deploy button in GitHub Actions (`workflow_dispatch`) triggers SSH deploy to VM. The deploy job is gated by:

- `environment: production` — requires the **production** environment to be configured under **Settings → Environments**, with **Required reviewers** enabled. Without an approving reviewer, the deploy job stays queued.
- `github.ref == 'refs/heads/main'` — the deploy job only runs when dispatched against `main`. Dispatches from feature branches build images but never deploy.

Required secrets in GitHub repo settings:
- `VM_HOST` — VM IP or hostname
- `VM_USER` — SSH user
- `VM_SSH_KEY` — private key for SSH access

See `.github/workflows/deploy.yml`.

---

## Upgrade

The app build/deploy targets are per-version — run them from `v4`:

```bash
cd v4
make kind-build-kubeintellect    # rebuild image (if building locally on VM)
make vm-deploy-kubeintellect     # helm upgrade --install (idempotent)
```

Or push to `main` and click Deploy in GitHub Actions.

---

## Connect

```bash
pipx install kube-q
KUBE_Q_API_KEY=<your-admin-key> kq --url https://api.your-domain.com
```
