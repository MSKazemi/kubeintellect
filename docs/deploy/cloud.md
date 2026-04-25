---
description: >-
  Deploy KubeIntellect to AKS, EKS, GKE, or any Kubernetes cluster using Helm — production-ready with RBAC, secrets, ingress, and HPA.
---

# Deploy: Cloud / VM (Helm)

Deploy KubeIntellect to any Kubernetes cluster — AKS, EKS, GKE, or Kind on a VM.

**Requirements:** `kubectl` pointing at your cluster, `helm`, Docker (to build the image).

---

## Option A — Azure VM (Kind on VM)

Run a Kind cluster on a Linux VM and expose it via nginx + TLS.

### 1. Clone the repo

```bash
git clone https://github.com/mskazemi/kubeintellect
cd kubeintellect
```

### 2. Create the Kind cluster

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
  host: api.your-domain.com   # your VM's domain or IP

config:
  llmProvider: azure          # or: openai
  prometheusUrl: ""           # add after monitoring install
  lokiUrl: ""
```

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

```bash
make kind-build-kubeintellect    # build Docker image + load into Kind
make vm-deploy-kubeintellect     # helm install KubeIntellect
```

### 5. Install monitoring (optional)

```bash
make monitoring-install    # Prometheus + Grafana + Loki → monitoring namespace
make langfuse-install      # Langfuse LLM tracing → monitoring namespace
```

Then update `values-production.yaml`:
```yaml
config:
  prometheusUrl: http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090
  lokiUrl: http://loki.monitoring.svc.cluster.local:3100
```

Redeploy: `make vm-deploy-kubeintellect`

### 6. Set up TLS

```bash
bash scripts/vm/setup-nginx.sh    # configure nginx reverse proxy
bash scripts/vm/setup-tls.sh      # get Let's Encrypt cert
```

### 7. Verify

```bash
curl https://api.your-domain.com/healthz    # → {"status":"ok"}
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
  host: api.your-domain.com
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

```bash
make aks-deploy-kubeintellect    # helm upgrade --install
```

### 4. Verify

```bash
curl https://api.your-domain.com/healthz    # → {"status":"ok"}
```

---

## Automated deploys (GitHub Actions)

Push to `main` → image is built and pushed to GHCR automatically.

Manual deploy button in GitHub Actions (`workflow_dispatch`) triggers SSH deploy to VM.

Required secrets in GitHub repo settings:
- `VM_HOST` — VM IP or hostname
- `VM_USER` — SSH user
- `VM_SSH_KEY` — private key for SSH access

See `.github/workflows/deploy.yml`.

---

## Upgrade

```bash
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
