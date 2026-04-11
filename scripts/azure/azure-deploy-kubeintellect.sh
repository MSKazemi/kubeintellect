#!/bin/bash
set -e
# ------------- Color definitions -------------
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[1;34m'
CYAN='\033[1;36m'
RED='\033[0;31m'
NC='\033[0m' # No Color
BOLD='\033[1m'
CHECK="${GREEN}✅${NC}"
CROSS="${RED}❌${NC}"
INFO="${CYAN}ℹ️${NC}"

./scripts/ops/logo.sh

NAMESPACE=kubeintellect

# Create namespace (if not exists)
echo -ne "${INFO} Creating namespace '${YELLOW}$NAMESPACE${NC}' ... "
if kubectl get namespace "$NAMESPACE" &> /dev/null; then
    echo -e "${YELLOW}already exists${NC} ${CHECK}"
else
    kubectl create namespace "$NAMESPACE" && echo -e "${CHECK}"
fi




helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update

helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  -n ingress-nginx --create-namespace \
  --set controller.service.type=LoadBalancer \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/azure-load-balancer-health-probe-request-path"=/healthz




# Apply configs & secrets
apply_yaml() {
    local yaml=$1
    local msg=$2
    # Capture stderr output
    local output
    if output=$(kubectl apply -f "$yaml" -n $NAMESPACE 2>&1 > /dev/null); then
        echo -e "${CHECK} $msg applied"
    else
        echo -e "${CROSS} Failed to apply $msg\n${RED}$output${NC}"
    fi
}





apply_yaml deployments/postgresql.yaml                          "PostgreSQL Deployment"
apply_yaml deployments/postgresql-db-migrate.yaml               "PostgreSQL DB Migration Job for Checkpoints"
apply_yaml deployments/env-kubeintellect-core-configmaps.yaml   "Core ConfigMap"
apply_yaml deployments/env-kubeintellect-core-secret.yaml       "Core Secret"
apply_yaml deployments/env-kubeintellect-chat-configmaps.yaml   "Chat ConfigMap"
apply_yaml deployments/env-kubeintellect-chat-secret.yaml       "Chat Secret"
apply_yaml deployments/rbac.yaml                                "RBAC"
apply_yaml deployments/kubeintellect-core-prod.yaml             "Core Deployment"
apply_yaml deployments/kubeintellect-chat-prod.yaml             "Chat Deployment"



# Map Ingress IP
echo -e "${INFO} Mapping ingress external IP to local hosts ..."
if scripts/ops/map_ingress_ip.sh; then
    echo -e "${CHECK} Ingress IP mapped"
else
    echo -e "${CROSS} Ingress mapping failed"
fi



# Print status
echo -e "${INFO} Cluster resources in namespace '${YELLOW}$NAMESPACE${NC}':"
sleep 10
kubectl get all -n $NAMESPACE | tee /dev/tty | tail -n +2 | awk '{print "   " $0}'

echo -e "\n${GREEN}${BOLD}KubeIntellect deployment complete!${NC}"

