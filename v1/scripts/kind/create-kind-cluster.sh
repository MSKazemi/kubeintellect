#!/bin/bash
set -euo pipefail

# ------------- Color definitions -------------
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[1;36m'
RED='\033[0;31m'
NC='\033[0m'
CHECK="${GREEN}✅${NC}"
INFO="${CYAN}ℹ️${NC}"

# Config
CLUSTER_NAME="${KIND_CLUSTER_NAME:-testbed}"
KUBECONFIG_FILE="$HOME/.kube/config"

./scripts/ops/logo.sh || true

echo -e "${INFO} Checking prerequisites..."

# Install kind if missing
if ! command -v kind &> /dev/null; then
  echo -e "${INFO} Installing kind..."
  ARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')
  curl -Lo ./kind "https://kind.sigs.k8s.io/dl/v0.23.0/kind-$(uname -s | tr '[:upper:]' '[:lower:]')-${ARCH}"
  chmod +x ./kind
  sudo mv ./kind /usr/local/bin/kind
  echo -e "${CHECK} Kind installed."
fi

# Install kubectl if missing
if ! command -v kubectl &> /dev/null; then
  echo -e "${INFO} Installing kubectl..."
  curl -LO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/$(uname -s | tr '[:upper:]' '[:lower:]')/amd64/kubectl"
  chmod +x kubectl
  sudo mv kubectl /usr/local/bin/
  echo -e "${CHECK} Kubectl installed."
fi

# Check if cluster already exists
if kind get clusters | grep -q "^${CLUSTER_NAME}$"; then
  echo -e "${YELLOW}Cluster \"${CLUSTER_NAME}\" already exists.${NC}"
  read -r -p "❓ Do you want to delete and recreate it? [y/N]: " answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    echo -e "${INFO} Deleting cluster \"${CLUSTER_NAME}\"..."
    kind delete cluster --name "$CLUSTER_NAME"
  else
    echo -e "${INFO} Skipping cluster creation."
    exit 0
  fi
fi

# Resolve the Kind cluster config from the template, substituting the actual project root.
# This makes the hostPath mounts portable — no hardcoded paths in version control.
PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
KIND_CONFIG_RESOLVED="$(mktemp /tmp/kind-2node-XXXXXX.yaml)"
PROJECT_ROOT="$PROJECT_ROOT" envsubst < ./infrastructure/kind/kind-2node.yaml.template > "$KIND_CONFIG_RESOLVED"
echo -e "${INFO} Project root: ${PROJECT_ROOT}"
echo -e "${INFO} Kind config:  ${KIND_CONFIG_RESOLVED}"

# Create the Kind cluster
echo -e "${INFO} Creating Kind cluster \"${CLUSTER_NAME}\"..."
kind create cluster --name "$CLUSTER_NAME" --config "$KIND_CONFIG_RESOLVED" --wait 60s
rm -f "$KIND_CONFIG_RESOLVED"

# Configure kubeconfig — merge into existing config instead of overwriting
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
# Kind writes 0.0.0.0 as the server address but the TLS cert only covers 127.0.0.1.
# Patch the kubeconfig so kubectl can connect without a cert error.
kubectl config set-cluster "kind-${CLUSTER_NAME}" --server="https://127.0.0.1:6443" --kubeconfig "$KUBECONFIG_FILE"

echo -e "${CHECK} Kind cluster \"${CLUSTER_NAME}\" created and configured."

# Show cluster info
echo -e "${INFO} Cluster Info:"
kubectl cluster-info --context "kind-${CLUSTER_NAME}"
kubectl get nodes -o wide

# Wait for all nodes to be Ready before proceeding (CNI may still be applying)
echo -e "${INFO} Waiting for all nodes to be Ready..."
if ! kubectl wait --for=condition=Ready nodes --all --timeout=120s; then
  echo -e "${RED}Warning: not all nodes became Ready within 120s. Proceeding anyway.${NC}"
fi

# Label first node for ingress
FIRST_NODE="$(kubectl get nodes -o name | head -1 | cut -d/ -f2)"
kubectl label node "$FIRST_NODE" ingress-ready=true --overwrite

# Add and update ingress-nginx Helm repository
echo -e "${INFO} Adding ingress-nginx Helm repository..."
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx || true
helm repo update ingress-nginx

helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  -n ingress-nginx --create-namespace \
  --values ./infrastructure/kind/ingress-values.yaml \
  --set controller.hostPort.enabled=true \
  --set controller.daemonset.useHostPort=true \
  --set controller.service.type=ClusterIP

# Wait for ingress-nginx controller rollout
echo -e "${INFO} Waiting for ingress-nginx controller rollout..."
if ! kubectl -n ingress-nginx rollout status deployment/ingress-nginx-controller --timeout=180s; then
  echo -e "${RED}Warning: ingress-nginx controller rollout timed out.${NC}"
fi

echo -e "${INFO} Waiting for admission jobs to complete..."
kubectl -n ingress-nginx wait --for=condition=complete job/ingress-nginx-admission-create --timeout=180s || true
kubectl -n ingress-nginx wait --for=condition=complete job/ingress-nginx-admission-patch --timeout=180s || true

echo -e "${INFO} Waiting for admission webhook endpoints to be ready..."
READY=false
for i in $(seq 1 36); do
  if kubectl -n ingress-nginx get endpoints ingress-nginx-controller-admission \
      -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null | grep -qE '.'; then
    echo -e "${CHECK} ingress-nginx admission endpoints are ready."
    READY=true
    break
  fi
  sleep 5
done

if [[ "$READY" != "true" ]]; then
  echo -e "${RED}Warning: admission webhook endpoints did not become ready in time. Ingress may not work correctly.${NC}"
fi
