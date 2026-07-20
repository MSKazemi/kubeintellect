---
description: >-
  Step-by-step guide to deploying KubeIntellect on Alibaba Cloud ACK with Qwen
  Cloud / DashScope as the LLM backend and ApsaraDB RDS for PostgreSQL as the
  durable memory substrate.
---
# Deploy to Alibaba Cloud (ACK) with Qwen Cloud

This guide deploys KubeIntellect to **Alibaba Cloud Container Service for
Kubernetes (ACK)**, using **Qwen models on Qwen Cloud / DashScope** purely as the
LLM backend. Architecture split:

- **Compute / deployment** → Alibaba Cloud ACK (managed Kubernetes)
- **LLM** → Qwen Cloud / DashScope (`qwen-max` + `qwen-plus`), via the
  OpenAI-compatible endpoint — KubeIntellect talks to it through its normal
  `openai` provider path (no Alibaba-specific SDK).

> Everything KubeIntellect learns (memory hierarchy, reflexion, **operator
> preferences**, flight recorder) persists in PostgreSQL, so we use a managed
> **ApsaraDB RDS for PostgreSQL** instance.

---

## 0. Prerequisites

- An Alibaba Cloud account with access to **ACK**, **ACR** (Container Registry),
  **ApsaraDB RDS**, and **Model Studio / DashScope**.
- Local tools: `kubectl`, `helm`, `docker`, and the KubeIntellect repo (`v4/`).
- (Optional) the `aliyun` CLI for scripting; the console works too.

---

## 1. Get a Qwen / DashScope API key

1. Open **Model Studio** (DashScope) in the Alibaba Cloud console and enable it.
2. Create an **API key**. Note which endpoint region you use:
   - International: `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`
   - Mainland China: `https://dashscope.aliyuncs.com/compatible-mode/v1`

## 2. Verify Qwen works (before any cluster)

Put the config in `v4/.env`:

```env
LLM_PROVIDER=openai
OPENAI_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
OPENAI_API_KEY=sk-your-dashscope-key
OPENAI_COORDINATOR_MODEL=qwen-max
OPENAI_SUBAGENT_MODEL=qwen-plus
```

Then run the connectivity check (exercises the real LLM factory, incl.
tool-calling that the RCA subagents need):

```bash
cd v4
set -a && source .env && set +a
uv run python scripts/verify_qwen.py
```

You want two green checks: **chat completion** and **tool calling**.

## 3. Create the ACK cluster

Create an **ACK managed cluster** (console → Container Service → Clusters). Use an
**amd64** node pool (the image is linux/amd64). Then fetch its kubeconfig:

```bash
# via console: Cluster → Connection Information → copy kubeconfig, or:
aliyun cs GET /k8s/<cluster-id>/user_config | ...   # see ACK docs
export KUBECONFIG=~/.kube/ack-kubeintellect
kubectl get nodes         # confirm access
```

Install an ingress controller if the cluster doesn't have one (ACK offers
`ack-ingress-nginx` or the ALB Ingress Controller as add-ons).

## 4. Mirror the image to ACR

Pulling GHCR from China-region nodes is slow/unreliable, so mirror the image to
**ACR**:

```bash
# 1. create an ACR namespace + repo `kubeintellect` in the console
# 2. log in and mirror:
docker login registry-intl.<region>.aliyuncs.com
docker pull ghcr.io/mskazemi/kubeintellect:dev-latest
docker tag  ghcr.io/mskazemi/kubeintellect:dev-latest \
            registry-intl.<region>.aliyuncs.com/<namespace>/kubeintellect:dev-latest
docker push registry-intl.<region>.aliyuncs.com/<namespace>/kubeintellect:dev-latest
```

If the ACR repo is private, create a pull secret and reference it (or make the
repo public for the hackathon demo).

## 5. Provision ApsaraDB RDS for PostgreSQL

1. Create an **ApsaraDB RDS for PostgreSQL 16** instance in the same VPC as the
   ACK cluster.
2. Create a database `kubeintellect` and an account.
3. Allow the ACK cluster's security group / CIDR to reach the RDS port (5432).
4. Build the DSN: `postgresql://<user>:<password>@<host>.pg.rds.aliyuncs.com:5432/kubeintellect`

Initialize the schema once (from a machine that can reach RDS):

```bash
psql "postgresql://<user>:<password>@<host>:5432/kubeintellect" \
  -f packages/kubeintellect-server/app/db/schema.sql
```

## 6. Configure values + secrets

```bash
cp deploy/helm/kubeintellect/values-alibaba.yaml.example \
   deploy/helm/kubeintellect/values-alibaba.yaml
```

Edit `values-alibaba.yaml`: set `image.repository` (your ACR path), the ingress
`hosts`, and (if not using `.env`) the RDS `postgres.external.url`.

Add the rest to `v4/.env` (never commit it):

```env
# LLM (from step 2) — already set
DATABASE_URL=postgresql://<user>:<password>@<host>:5432/kubeintellect
KUBEINTELLECT_ADMIN_KEYS=<pick-a-strong-token>
```

## 7. Deploy

```bash
cd v4
make alibaba-deploy-kubeintellect        # NAMESPACE defaults to 'kubeintellect'
```

The target validates that `LLM_PROVIDER=openai`, `OPENAI_API_KEY`, and
`OPENAI_BASE_URL` are set, then runs `helm upgrade --install` with
`values.yaml` + `values-alibaba.yaml`.

## 8. Verify + capture proof (for the submission)

```bash
kubectl -n kubeintellect get pods,ingress,svc
kubectl -n kubeintellect exec deploy/kubeintellect -- curl -s localhost:8000/healthz
# then hit the ingress:
curl -H "Authorization: Bearer <admin-key>" https://api.your-domain.com/healthz
```

For the Devpost "proof of Alibaba Cloud deployment": commit the (secret-free)
`values-alibaba.yaml` and this runbook, and screenshot the ACK console showing
the running workload. The committed `values-alibaba.yaml` + `Makefile` target are
the "code file that demonstrates use of Alibaba Cloud services" the rules ask for.

---

## Lighter alternative — single ECS + k3s (the coupon-friendly path)

Full managed ACK carries a cluster-management fee **plus** ApsaraDB RDS **plus** a
Server Load Balancer — the expensive parts. For a hackathon demo funded by a small
credit/coupon, a single **Alibaba ECS** running **single-node k3s** with in-cluster
Postgres satisfies "deployed on Alibaba Cloud" at a fraction of the cost. This repo
ships a ready-made lightweight overlay + one-shot bootstrap for it:

- `deploy/helm/kubeintellect/values-ecs-k3s.yaml.example` — in-cluster Postgres on
  k3s `local-path`, `ClusterIP` (no SLB), trimmed resources for a 2 vCPU / 4 GB box.
- `scripts/alibaba_ecs_k3s_bootstrap.sh` — installs k3s + helm and deploys in one go.

### Cost guardrails (so you never pay out of pocket)

- **Skip the pricey managed services**: no ACK, no ApsaraDB RDS, no SLB — all the
  costly line items. Only the **ECS** itself is billed.
- **Buy it as a 1-month *subscription* with auto-renew OFF** (not open-ended
  pay-as-you-go): the price is fixed and known at checkout, a **cash coupon covers
  it so the payable shows $0**, and it **cannot accrue anything past the term** —
  it just stops. That is a hard cap, not an alert.
- **Verify the checkout shows `$0` payable** (coupon applied) *before* confirming.
  If it shows a nonzero amount, stop.
- Set a **Budget alert** (Billing → *Budgets*) below your coupon as a backstop.
- **Stop / release** the ECS right after recording the demo.
- Region: an **international region (e.g. Singapore `ap-southeast-1`)** reaches GHCR
  directly, so you can skip the ACR mirror entirely on this path.

### Steps

```bash
# 1. Create a small amd64 ECS (2 vCPU / 4 GB), Ubuntu or Alibaba Linux, public IP,
#    as a 1-month Subscription with auto-renew OFF. Confirm coupon → $0 payable.
# 2. SSH in, clone the repo, cd v4/, and put your config in .env
#    (LLM_PROVIDER=qwen, DASHSCOPE_API_KEY=..., POSTGRES_PASSWORD=..., KUBEINTELLECT_ADMIN_KEYS=...)
# 3. One command — installs k3s+helm, deploys, health-checks:
scripts/alibaba_ecs_k3s_bootstrap.sh
```

The script prints how to reach the API over an SSH tunnel (no public port opened)
and reminds you to release the ECS when done. ACK (above) remains the more
production-grade story if you later want it.
