#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# KubeIntellect — single Alibaba ECS + k3s bootstrap (the coupon-friendly path)
#
# Run this ON a fresh amd64 Alibaba ECS (Ubuntu / Alibaba Linux), from the repo's
# v4/ directory, after putting your config in v4/.env. It:
#   1. installs single-node k3s (with its local-path storage) + helm
#   2. deploys KubeIntellect via Helm using values-ecs-k3s.yaml (in-cluster
#      Postgres, no RDS/SLB), with secrets injected from .env
#   3. waits for rollout + verifies /healthz
# The chart's job-db-init applies the DB schema automatically — no manual psql.
#
# Cost note: this provisions NO paid Alibaba resource by itself (k3s is just
# software on the ECS you already bought). The only spend is the ECS itself.
# Keep the ECS small and release/stop it after the demo to preserve your coupon.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")/.."   # → v4/
CHART=deploy/helm/kubeintellect
VALUES_EXAMPLE="$CHART/values-ecs-k3s.yaml.example"
VALUES="$CHART/values-ecs-k3s.yaml"
NAMESPACE="${NAMESPACE:-kubeintellect}"

echo "==> Pre-flight checks"
test -f .env || { echo "ERROR: v4/.env not found. Copy .env.example → .env and fill it in."; exit 1; }
if [ ! -f "$VALUES" ]; then
  echo "    values-ecs-k3s.yaml missing → copying from example (defaults are fine)."
  cp "$VALUES_EXAMPLE" "$VALUES"
fi

# shellcheck disable=SC1091
set -a && source .env && set +a

LLM_PROVIDER="${LLM_PROVIDER:-qwen}"
if [ "$LLM_PROVIDER" != "qwen" ] && [ "$LLM_PROVIDER" != "openai" ]; then
  echo "ERROR: this path expects LLM_PROVIDER=qwen (or openai); got '$LLM_PROVIDER'"; exit 1
fi
# DASHSCOPE_API_KEY / QWEN_API_KEY are accepted aliases for the LLM key.
OPENAI_API_KEY="${OPENAI_API_KEY:-${DASHSCOPE_API_KEY:-${QWEN_API_KEY:-}}}"
: "${OPENAI_API_KEY:?ERROR: set OPENAI_API_KEY (or DASHSCOPE_API_KEY) in .env — your DashScope key}"
: "${POSTGRES_PASSWORD:?ERROR: set POSTGRES_PASSWORD in .env}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://dashscope-intl.aliyuncs.com/compatible-mode/v1}"

echo "==> Installing k3s (single node) if absent"
if ! command -v k3s >/dev/null 2>&1; then
  curl -sfL https://get.k3s.io | sh -
fi
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
sudo chmod 644 "$KUBECONFIG" 2>/dev/null || true
kubectl get nodes

echo "==> Installing helm if absent"
if ! command -v helm >/dev/null 2>&1; then
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi

echo "==> Deploying KubeIntellect (Qwen LLM, in-cluster Postgres)"
helm upgrade --install kubeintellect "$CHART" \
  -f "$CHART/values.yaml" \
  -f "$VALUES" \
  --namespace "$NAMESPACE" --create-namespace \
  --set postgres.password="$POSTGRES_PASSWORD" \
  --set-string config.llmProvider="$LLM_PROVIDER" \
  --set-string secrets.openaiApiKey="$OPENAI_API_KEY" \
  --set-string secrets.openaiBaseUrl="$OPENAI_BASE_URL" \
  --set-string secrets.openaiCoordinatorModel="${OPENAI_COORDINATOR_MODEL:-qwen-max}" \
  --set-string secrets.openaiSubagentModel="${OPENAI_SUBAGENT_MODEL:-qwen-plus}" \
  --set-string secrets.adminApiKeys="${KUBEINTELLECT_ADMIN_KEYS:-}" \
  --set-string secrets.operatorApiKeys="${KUBEINTELLECT_OPERATOR_KEYS:-}" \
  --set-string secrets.readonlyApiKeys="${KUBEINTELLECT_READONLY_KEYS:-}"

echo "==> Waiting for rollout"
kubectl -n "$NAMESPACE" rollout status deploy/kubeintellect --timeout=300s

echo "==> Health check"
kubectl -n "$NAMESPACE" exec deploy/kubeintellect -- curl -sf localhost:8000/healthz \
  && echo "  ✓ /healthz OK" || { echo "  ✗ /healthz failed — check: kubectl -n $NAMESPACE logs deploy/kubeintellect"; exit 1; }

cat <<EOF

────────────────────────────────────────────────────────────────────────────
✓ KubeIntellect is running on this ECS via k3s (Qwen/DashScope LLM).

Reach it (no public port opened — tunnel over SSH from your laptop):
  # on the ECS:
  export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
  kubectl -n $NAMESPACE port-forward svc/kubeintellect 8000:8000
  # then from your laptop:
  ssh -L 8000:localhost:8000 <ecs-user>@<ecs-ip>
  KUBE_Q_API_KEY=<your-admin-key> kq --host http://localhost:8000

Seed the demo cluster (5 broken workloads) for the MemoryAgent money-shot:
  kubectl create namespace demo-rca   # then apply your demo manifests / kubeintellect init

Proof for Devpost: screenshot the ECS in the Alibaba console + \`kubectl get pods\`,
and commit (secret-free) values-ecs-k3s.yaml + this script as the deployment code.

💸 Coupon reminder: STOP or RELEASE this ECS when you're done recording so it
   stops drawing on your \$40 coupon. (Billing → Instances → Stop/Release.)
────────────────────────────────────────────────────────────────────────────
EOF
