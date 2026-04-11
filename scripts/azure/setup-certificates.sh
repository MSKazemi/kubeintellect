#!/bin/bash
# =============================================================================
# KubeIntellect — Install cert-manager
# =============================================================================
# Installs cert-manager into the cluster if not already present, then waits
# for the webhook to become ready. The ClusterIssuer and ingress TLS config
# are managed declaratively via the Helm chart (values-azure.yaml).
#
# Run this BEFORE `make azure-kubeintellect-deploy` so the cert-manager CRDs
# exist when Helm applies the ClusterIssuer resource.
#
# Usage:
#   bash scripts/azure/setup-certificates.sh
# =============================================================================

set -euo pipefail

CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.14.5}"
CERT_MANAGER_NAMESPACE="cert-manager"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }

# ------------------------------------------------------------------
# Check if cert-manager is already installed
# ------------------------------------------------------------------
if kubectl get namespace "$CERT_MANAGER_NAMESPACE" &>/dev/null && \
   kubectl get deployment cert-manager -n "$CERT_MANAGER_NAMESPACE" &>/dev/null; then
    log_info "cert-manager is already installed — skipping install."
else
    log_info "Installing cert-manager ${CERT_MANAGER_VERSION}..."
    kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml"
fi

# ------------------------------------------------------------------
# Wait for cert-manager webhook to be ready (CRDs usable after this)
# ------------------------------------------------------------------
log_info "Waiting for cert-manager webhook deployment to be ready..."
kubectl rollout status deployment/cert-manager-webhook \
    -n "$CERT_MANAGER_NAMESPACE" \
    --timeout=120s

log_info "cert-manager is ready."
log_info "You can now run: make azure-kubeintellect-deploy"
