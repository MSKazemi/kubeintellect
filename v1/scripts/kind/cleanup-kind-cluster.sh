#!/bin/bash
set -euo pipefail

# ------------- Color definitions -------------
GREEN='\033[0;32m'
CYAN='\033[1;36m'
RED='\033[0;31m'
NC='\033[0m'
CHECK="${GREEN}✅${NC}"
INFO="${CYAN}ℹ️${NC}"

CLUSTER_NAME="${KIND_CLUSTER_NAME:-testbed}"

./scripts/ops/logo.sh || true

if ! kind get clusters | grep -q "^${CLUSTER_NAME}$"; then
  echo -e "${INFO} Cluster \"${CLUSTER_NAME}\" does not exist. Nothing to delete."
  exit 0
fi

echo -e "${INFO} Deleting Kind cluster \"${CLUSTER_NAME}\"..."
kind delete cluster --name "$CLUSTER_NAME"
echo -e "${CHECK} Cluster \"${CLUSTER_NAME}\" deleted."
