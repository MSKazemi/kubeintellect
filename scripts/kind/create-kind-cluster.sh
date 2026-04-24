#!/bin/bash
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[1;36m'
RED='\033[0;31m'
NC='\033[0m'
CHECK="${GREEN}✓${NC}"
INFO="${CYAN}→${NC}"

CLUSTER_NAME="${KIND_CLUSTER_NAME:-testbed}"
KUBECONFIG_FILE="$HOME/.kube/config"

echo -e "${INFO} Checking prerequisites..."

if ! command -v kind &> /dev/null; then
  echo -e "${INFO} Installing kind..."
  ARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')
  curl -Lo ./kind "https://kind.sigs.k8s.io/dl/v0.23.0/kind-$(uname -s | tr '[:upper:]' '[:lower:]')-${ARCH}"
  chmod +x ./kind
  sudo mv ./kind /usr/local/bin/kind
  echo -e "${CHECK} kind installed."
fi

if ! command -v kubectl &> /dev/null; then
  echo -e "${INFO} Installing kubectl..."
  curl -LO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/$(uname -s | tr '[:upper:]' '[:lower:]')/amd64/kubectl"
  chmod +x kubectl
  sudo mv kubectl /usr/local/bin/
  echo -e "${CHECK} kubectl installed."
fi

if ! command -v helm &> /dev/null; then
  echo -e "${INFO} Installing helm..."
  curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
  echo -e "${CHECK} helm installed."
fi

if kind get clusters | grep -q "^${CLUSTER_NAME}$"; then
  echo -e "${YELLOW}Cluster \"${CLUSTER_NAME}\" already exists.${NC}"
  read -r -p "Delete and recreate? [y/N]: " answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    echo -e "${INFO} Deleting cluster \"${CLUSTER_NAME}\"..."
    kind delete cluster --name "$CLUSTER_NAME"
  else
    echo -e "${INFO} Skipping cluster creation."
    exit 0
  fi
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "${PROJECT_ROOT}/.local-data/postgres"

KIND_CONFIG_RESOLVED="$(mktemp /tmp/kind-config-XXXXXX.yaml)"
PROJECT_ROOT="$PROJECT_ROOT" envsubst < "${PROJECT_ROOT}/deploy/kind/kind-config.yaml" > "$KIND_CONFIG_RESOLVED"
echo -e "${INFO} Project root: ${PROJECT_ROOT}"

echo -e "${INFO} Creating Kind cluster \"${CLUSTER_NAME}\"..."
kind create cluster --name "$CLUSTER_NAME" --config "$KIND_CONFIG_RESOLVED" --wait 60s
rm -f "$KIND_CONFIG_RESOLVED"

# Merge kubeconfig
echo -e "${INFO} Merging kubeconfig into ${KUBECONFIG_FILE}..."
KIND_KUBECONFIG="$(mktemp /tmp/kind-kubeconfig-XXXXXX)"
kind get kubeconfig --name "$CLUSTER_NAME" > "$KIND_KUBECONFIG"
if [[ -f "$KUBECONFIG_FILE" ]]; then
  KUBECONFIG="${KUBECONFIG_FILE}:${KIND_KUBECONFIG}" kubectl config view --flatten > "${KUBECONFIG_FILE}.tmp"
  mv "${KUBECONFIG_FILE}.tmp" "$KUBECONFIG_FILE"
else
  mkdir -p "$(dirname "$KUBECONFIG_FILE")"
  cp "$KIND_KUBECONFIG" "$KUBECONFIG_FILE"
fi
rm -f "$KIND_KUBECONFIG"
kubectl config set-cluster "kind-${CLUSTER_NAME}" --server="https://127.0.0.1:6443" --kubeconfig "$KUBECONFIG_FILE"

echo -e "${CHECK} Kind cluster \"${CLUSTER_NAME}\" created."

echo -e "${INFO} Waiting for nodes to be Ready..."
kubectl wait --for=condition=Ready nodes --all --timeout=120s || \
  echo -e "${RED}Warning: not all nodes became Ready within 120s${NC}"

# Label control-plane node for ingress-nginx (must be ready before tolerations apply)
CONTROL_PLANE_NODE="$(kubectl get nodes --selector='node-role.kubernetes.io/control-plane' -o name | head -1 | cut -d/ -f2)"
kubectl label node "$CONTROL_PLANE_NODE" ingress-ready=true --overwrite
echo -e "${CHECK} Labeled ${CONTROL_PLANE_NODE} as ingress-ready."

# Install ingress-nginx — pinned to control-plane so it binds host ports 80/443
echo -e "${INFO} Installing ingress-nginx..."
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx || true
helm repo update ingress-nginx
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  -n ingress-nginx --create-namespace \
  --values "${PROJECT_ROOT}/deploy/kind/ingress-values.yaml" \
  --set controller.hostPort.enabled=true \
  --set controller.daemonset.useHostPort=true \
  --set controller.service.type=ClusterIP

echo -e "${INFO} Waiting for ingress-nginx controller rollout..."
kubectl -n ingress-nginx rollout status deployment/ingress-nginx-controller --timeout=180s || \
  echo -e "${RED}Warning: ingress-nginx controller rollout timed out.${NC}"

kubectl -n ingress-nginx wait --for=condition=complete job/ingress-nginx-admission-create --timeout=180s || true
kubectl -n ingress-nginx wait --for=condition=complete job/ingress-nginx-admission-patch  --timeout=180s || true

# Wait for admission webhook endpoints
READY=false
for i in $(seq 1 36); do
  if kubectl -n ingress-nginx get endpoints ingress-nginx-controller-admission \
      -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null | grep -qE '.'; then
    echo -e "${CHECK} ingress-nginx admission endpoints ready."
    READY=true
    break
  fi
  sleep 5
done
[[ "$READY" != "true" ]] && echo -e "${RED}Warning: admission webhook endpoints did not become ready.${NC}"

kubectl cluster-info --context "kind-${CLUSTER_NAME}"
kubectl get nodes -o wide

echo ""
echo -e "${CHECK} Done. Next steps:"
echo -e "  make kind-build    # build image and load into cluster"
echo -e "  make kind-deploy   # helm install kubeintellect"
echo -e ""
echo -e "  Add to /etc/hosts:"
echo -e "    127.0.0.1 api.kubeintellect.local"
echo -e "  Then access: http://api.kubeintellect.local"
