---
description: >-
  Guide to deploying KubeIntellect on Google Kubernetes Engine (GKE) with the LLM
  provider decoupled from the cloud (Azure OpenAI, OpenAI, Qwen, or Anthropic) and
  Cloud SQL for PostgreSQL as the state store.
---
# Deploy to Google Cloud GKE

Deploy KubeIntellect to **Google Kubernetes Engine (GKE)**. The LLM provider is
decoupled from the cloud — use **Azure OpenAI, OpenAI, Qwen (DashScope), or
Anthropic** all the same way.

- **Compute / deployment** → GKE
- **LLM** → your choice (`azure` | `openai` | `qwen` | `anthropic`)
- **State** (memory hierarchy, reflexion, operator preferences, flight recorder)
  → PostgreSQL, ideally **Cloud SQL for PostgreSQL**.

---

## 0. Prerequisites

- A GKE cluster (Standard or Autopilot) with an **amd64** node pool (e.g. `e2`/`n2`;
  the image is `linux/amd64`, not `t2a`/arm64).
- Local tools: `kubectl` (via `gcloud container clusters get-credentials`), `helm`,
  `gcloud`, `docker`.

## 1. Pick + verify the LLM

Put your provider's config in `v4/.env` (see `.env.example`). For example, Anthropic:

```env
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

Verify before touching the cluster:

```bash
cd v4 && set -a && source .env && set +a
uv run kq --query "get all pods"     # any grounded query confirms the LLM factory
```

## 2. (Optional) Mirror the image to Artifact Registry

GKE can pull GHCR directly. To keep pulls in-region, mirror to **Artifact Registry**
— see the commented recipe at the top of
`deploy/helm/kubeintellect/values-gcp.yaml.example`, then set `image.repository`.

## 3. Provision Cloud SQL for PostgreSQL (recommended)

1. Create a **Cloud SQL for PostgreSQL 16** instance; enable **private IP** (or use
   the Cloud SQL Auth Proxy sidecar).
2. Create a `kubeintellect` database and a user.
3. Put the DSN in `v4/.env`:
   `DATABASE_URL=postgresql://<user>:<password>@<private-ip>:5432/kubeintellect`

The chart's `job-db-init` applies the schema automatically on install. (In-cluster
Postgres on a `premium-rwo` PD is the fallback — see the values file.)

## 4. Configure values

```bash
cp deploy/helm/kubeintellect/values-gcp.yaml.example \
   deploy/helm/kubeintellect/values-gcp.yaml
```

Edit `values-gcp.yaml`: set `image.repository`, the ingress `hosts` (and a GKE
managed-certificate / static-IP annotation for TLS), and — if not using `.env` —
`postgres.external.url`.

## 5. Deploy

```bash
cd v4
make gcp-deploy-kubeintellect      # NAMESPACE defaults to 'kubeintellect'
```

The target validates the chosen provider's key, then runs `helm upgrade --install`
with `values.yaml` + `values-gcp.yaml`.

## 6. Verify + capture proof

```bash
kubectl -n kubeintellect get pods,ingress,svc
kubectl -n kubeintellect exec deploy/kubeintellect -- curl -s localhost:8000/healthz
```

For a submission's "cloud deployment" proof: commit the (secret-free)
`values-gcp.yaml` + this runbook, and screenshot the GKE console / `kubectl` output.

See also: [aws.md](aws.md) · [alibaba.md](alibaba.md) · [cloud.md](cloud.md) (provider matrix).
