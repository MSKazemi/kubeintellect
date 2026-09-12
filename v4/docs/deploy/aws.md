---
description: >-
  Guide to deploying KubeIntellect on Amazon EKS with the LLM provider decoupled
  from the cloud (Azure OpenAI, OpenAI, Qwen, or Anthropic) and Amazon RDS for
  PostgreSQL as the state store.
---
# Deploy to AWS EKS

Deploy KubeIntellect to **Amazon Elastic Kubernetes Service (EKS)**. The LLM
provider is decoupled from the cloud — use **Azure OpenAI, OpenAI, Qwen
(DashScope), or Anthropic** all the same way.

- **Compute / deployment** → AWS EKS
- **LLM** → your choice (`azure` | `openai` | `qwen` | `anthropic`)
- **State** (memory hierarchy, reflexion, operator preferences, flight recorder)
  → PostgreSQL, ideally **Amazon RDS for PostgreSQL**.

---

## 0. Prerequisites

- An EKS cluster with an **amd64** node group (the image is `linux/amd64`).
- The **AWS Load Balancer Controller** add-on if you want ALB ingress.
- Local tools: `kubectl` (pointed at the cluster), `helm`, `aws` CLI, `docker`.

## 1. Pick + verify the LLM

Put your provider's config in `v4/.env` (see `.env.example`). For example, Qwen:

```env
LLM_PROVIDER=qwen
DASHSCOPE_API_KEY=sk-your-dashscope-key
```

### Amazon Bedrock

Bedrock exposes an **OpenAI-compatible Chat Completions API**, so it needs no
KubeIntellect-specific provider — point the `openai` path at it and supply a Bedrock API
key as the bearer token:

```env
LLM_PROVIDER=openai
OPENAI_BASE_URL=https://bedrock-runtime.<region>.amazonaws.com/openai/v1
OPENAI_API_KEY=<your Bedrock API key>
OPENAI_COORDINATOR_MODEL=openai.gpt-oss-120b
OPENAI_SUBAGENT_MODEL=openai.gpt-oss-20b
```

Three things to get right:

- **Model ids are Bedrock ids**, not bare OpenAI names — `openai.gpt-oss-120b`,
  `anthropic.claude-...`. A bare `gpt-4o` will not resolve.
- **Claude on-demand needs a cross-Region inference profile id**, not the bare model id:
  the base id with a geography prefix (`us.`, `eu.`, `apac.`, `jp.`, `au.`, `global.`).
  Without it the call fails with *"Invocation with on-demand throughput isn't supported"*.
  Because cross-Region inference is supported on `bedrock-runtime` and **not** on
  `bedrock-mantle`, that also means using `bedrock-runtime` here. Per-token pricing is
  identical between the two endpoints, so nothing is lost.
- **The model must support client-side tool use**, and it must be enabled for your account.
  The subagents cannot investigate without function calling.

Model ids move, so list what your account actually has rather than copying a version string:

```bash
curl -s "$OPENAI_BASE_URL/models" -H "Authorization: Bearer $OPENAI_API_KEY" \
  | python3 -c 'import json,sys; [print(m["id"]) for m in json.load(sys.stdin)["data"]]'
```

Verify before touching the cluster (exercises the real factory incl. tool-calling):

```bash
cd v4 && set -a && source .env && set +a
uv run python scripts/verify_llm.py      # any provider — OpenAI, Bedrock, Azure, Qwen, Ollama
uv run python scripts/verify_qwen.py     # the older DashScope-specific check
```

## 2. (Optional) Mirror the image to ECR

EKS can pull GHCR directly. To keep pulls in-region, mirror to **ECR** — see the
commented recipe at the top of `deploy/helm/kubeintellect/values-aws.yaml.example`,
then set `image.repository` to the ECR path.

## 3. Provision Amazon RDS for PostgreSQL (recommended)

1. Create an **RDS for PostgreSQL 16** instance reachable from the EKS VPC.
2. Create a `kubeintellect` database; allow the node security group on port 5432.
3. Put the DSN in `v4/.env`:
   `DATABASE_URL=postgresql://<user>:<password>@<host>.rds.amazonaws.com:5432/kubeintellect`

The chart's `job-db-init` applies the schema automatically on install. (In-cluster
Postgres on an EBS `gp3` volume is the fallback — see the values file.)

## 4. Configure values

```bash
cp deploy/helm/kubeintellect/values-aws.yaml.example \
   deploy/helm/kubeintellect/values-aws.yaml
```

Edit `values-aws.yaml`: set `image.repository`, the ingress `hosts` (and ACM cert
ARN annotation for TLS), and — if not using `.env` — `postgres.external.url`.

## 5. Deploy

```bash
cd v4
make aws-deploy-kubeintellect      # NAMESPACE defaults to 'kubeintellect'
```

The target validates the chosen provider's key, then runs `helm upgrade --install`
with `values.yaml` + `values-aws.yaml`.

## 6. Verify + capture proof

```bash
kubectl -n kubeintellect get pods,ingress,svc
kubectl -n kubeintellect exec deploy/kubeintellect -- curl -s localhost:8000/healthz
```

For a submission's "cloud deployment" proof: commit the (secret-free)
`values-aws.yaml` + this runbook, and screenshot the EKS console / `kubectl` output.

See also: [gcp.md](gcp.md) · [alibaba.md](alibaba.md) · [cloud.md](cloud.md) (provider matrix).
