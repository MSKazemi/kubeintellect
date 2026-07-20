#!/bin/bash

# ----- Load Banner Logo -----
./k8s/production/logo.sh

# ----- Colors and Icons -----
GREEN='\033[0;32m'
RED='\033[0;31m'
CYAN='\033[1;36m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color
CHECK="${GREEN}✅${NC}"
CROSS="${RED}❌${NC}"
INFO="${CYAN}ℹ️${NC}"

# ----- Prompt for Namespace -----
if [ -n "$1" ]; then
  NAMESPACE="$1"
else
  read -rp "$(echo -e "${CYAN}Enter the Kubernetes namespace to delete:${NC} ")" NAMESPACE
fi

# Clean any accidental whitespace
NAMESPACE="$(echo -n "$NAMESPACE" | xargs)"

if [ -z "$NAMESPACE" ]; then
  echo -e "${CROSS} No namespace entered. Aborting. (Nothing has been deleted.)"
  exit 1
fi

# ----- Confirm Deletion -----
echo -e "${YELLOW}You are about to delete namespace: '${NAMESPACE}'${NC}"
read -rp "$(echo -e "${CYAN}Are you sure? Type 'yes' to confirm: ${NC}")" CONFIRM

if [[ "$CONFIRM" != "yes" ]]; then
  echo -e "${CROSS} Operation cancelled by user. Namespace not deleted."
  exit 1
fi

# ----- Delete Namespace -----
echo -e "${INFO} Deleting namespace: '${NAMESPACE}' ..."
if kubectl delete namespace "$NAMESPACE"; then
  echo -e "${CHECK} Namespace '${NAMESPACE}' deleted successfully!"
else
  echo -e "${CROSS} Failed to delete namespace '${NAMESPACE}'."
  exit 1
fi

# ----- Goodbye Message -----
echo -e "${NC}${GREEN}Goodbye!${NC}"
echo -e "${NC}${CYAN}Namespace cleanup complete. Have a great day! 🚀${NC}"
