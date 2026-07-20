#!/bin/bash
# =============================================================================
# KubeIntellect — Full Secret Infrastructure Setup
# Run once to set up Azure Key Vault + ESO + sync to Kubernetes.
# Idempotent: safe to re-run.
# =============================================================================
set -euo pipefail

# Logging helpers defined first so they can be used in the CONFIG section below.
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
info() { echo -e "${CYAN}[..] $1${NC}"; }
warn() { echo -e "${YELLOW}[!!] $1${NC}"; }
die()  { echo -e "${RED}[ERR] $1${NC}"; exit 1; }

# ----------------------------- CONFIG ----------------------------------------
# Override any of these via environment variables before running, e.g.:
#   SUBSCRIPTION=<id> TENANT_ID=<id> ./scripts/ops/setup-secrets-infra.sh
# Do NOT commit real subscription/tenant IDs to this file.
SUBSCRIPTION="${SUBSCRIPTION:-}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-kubeintellect}"
AKS_CLUSTER="${AKS_CLUSTER:-aks-kubeintellect}"
LOCATION="${LOCATION:-westeurope}"
NAMESPACE="${NAMESPACE:-kubeintellect}"
TENANT_ID="${TENANT_ID:-}"
KV_NAME="${KV_NAME:-kubeintellect-kv}"

[[ -z "$SUBSCRIPTION" ]] && die "Set SUBSCRIPTION env var (Azure subscription ID) before running"
[[ -z "$TENANT_ID" ]]    && die "Set TENANT_ID env var (Azure tenant ID) before running"
MI_NAME="kubeintellect-eso-mi"
CHART_PATH="./charts/kubeintellect"
VALUES_FILE="charts/kubeintellect/values-azure.yaml"

# Load secrets from .env (never committed to git)
# NOTE: We use `source` here, NOT `kubectl create secret --from-env-file`.
# `--from-env-file` does NOT strip shell quotes — a line like KEY="value" would
# store the literal string "value" (with surrounding double-quotes) as the secret
# value, silently breaking every env var that uses KEY="value" quoting in .env.
# `source` lets the shell parse the file correctly, then we pass each variable's
# already-unquoted value explicitly to `az keyvault secret set --value "$VAR"`.
SECRETS_FILE="$(git rev-parse --show-toplevel)/.env"
[[ ! -f "$SECRETS_FILE" ]] && die ".env not found at $SECRETS_FILE — copy .env.example and fill in values first"
set -a; source "$SECRETS_FILE"; set +a
# -----------------------------------------------------------------------------

# Validate placeholders are filled
for var in AZURE_OPENAI_API_KEY LANGCHAIN_API_KEY POSTGRES_PASSWORD KUBEINTELLECT_API_KEY GHCR_PAT; do
  [[ "${!var}" == PASTE_* ]] && die "Fill in $var before running this script"
done

# ----------------------------- STEP 1: Azure login ---------------------------
info "Setting subscription..."
az account set --subscription "$SUBSCRIPTION"
ok "Subscription set"

# ----------------------------- STEP 2: Key Vault -----------------------------
info "Creating Key Vault '$KV_NAME'..."
if az keyvault show --name "$KV_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  warn "Key Vault already exists, skipping"
else
  az keyvault create \
    --name "$KV_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --enable-rbac-authorization true \
    --output none
  ok "Key Vault created"
fi

# Grant current user Secrets Officer
info "Granting current user Key Vault Secrets Officer..."
MY_OID=$(az ad signed-in-user show --query id -o tsv)
KV_SCOPE="/subscriptions/${SUBSCRIPTION}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.KeyVault/vaults/${KV_NAME}"
az role assignment create \
  --role "Key Vault Secrets Officer" \
  --assignee-object-id "$MY_OID" \
  --assignee-principal-type User \
  --scope "$KV_SCOPE" \
  --output none 2>/dev/null || warn "Role assignment already exists"
ok "RBAC granted — waiting 20s for propagation..."
sleep 20

# ----------------------------- STEP 3: Store secrets -------------------------
info "Storing secrets in Key Vault..."
set_secret() {
  az keyvault secret set --vault-name "$KV_NAME" --name "$1" --value "$2" --output none \
    && echo -e "  ${GREEN}[OK]${NC} $1" \
    || echo -e "  ${RED}[FAIL]${NC} $1"
}

set_secret "AZURE-OPENAI-API-KEY"  "$AZURE_OPENAI_API_KEY"
set_secret "LANGCHAIN-API-KEY"     "$LANGCHAIN_API_KEY"
set_secret "POSTGRES-PASSWORD"     "$POSTGRES_PASSWORD"
set_secret "KUBEINTELLECT-API-KEY" "$KUBEINTELLECT_API_KEY"
set_secret "GHCR-USERNAME"         "$GHCR_USERNAME"
set_secret "GHCR-PAT"              "$GHCR_PAT"
set_secret "JWT-SECRET"            "$(openssl rand -hex 32)"
set_secret "JWT-REFRESH-SECRET"    "$(openssl rand -hex 32)"
set_secret "CREDS-KEY"             "$(openssl rand -hex 32)"
set_secret "CREDS-IV"              "$(openssl rand -hex 16)"
set_secret "MEILI-MASTER-KEY"      "$(openssl rand -base64 32)"

# MongoDB auth secrets (required since --noauth was removed)
MONGO_ROOT_PASS="$(openssl rand -base64 32)"
set_secret "MONGO-ROOT-PASSWORD" "$MONGO_ROOT_PASS"
# Full URI used by LibreChat and GDPR retention job
set_secret "MONGO-URI" "mongodb://mongouser:${MONGO_ROOT_PASS}@mongodb:27017/LibreChat?authSource=admin"
ok "MongoDB auth secrets stored"

# ----------------------------- STEP 4: Workload Identity ---------------------
info "Enabling Workload Identity on AKS..."
WI_ENABLED=$(az aks show --resource-group "$RESOURCE_GROUP" --name "$AKS_CLUSTER" \
  --query "securityProfile.workloadIdentity.enabled" -o tsv)
if [[ "$WI_ENABLED" == "true" ]]; then
  warn "Workload Identity already enabled, skipping"
else
  az aks update \
    --resource-group "$RESOURCE_GROUP" \
    --name "$AKS_CLUSTER" \
    --enable-oidc-issuer \
    --enable-workload-identity \
    --output none
  ok "Workload Identity enabled"
fi

OIDC_ISSUER=$(az aks show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$AKS_CLUSTER" \
  --query "oidcIssuerProfile.issuerUrl" -o tsv)
ok "OIDC Issuer: $OIDC_ISSUER"

# ----------------------------- STEP 5: Managed Identity ----------------------
info "Creating Managed Identity '$MI_NAME'..."
if az identity show --name "$MI_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  warn "Managed Identity already exists, skipping"
else
  az identity create --name "$MI_NAME" --resource-group "$RESOURCE_GROUP" --output none
  ok "Managed Identity created"
fi

MI_CLIENT_ID=$(az identity show --name "$MI_NAME" --resource-group "$RESOURCE_GROUP" --query clientId -o tsv)
MI_PRINCIPAL_ID=$(az identity show --name "$MI_NAME" --resource-group "$RESOURCE_GROUP" --query principalId -o tsv)

# Grant MI access to KV
info "Granting Managed Identity 'Key Vault Secrets User'..."
az role assignment create \
  --role "Key Vault Secrets User" \
  --assignee-object-id "$MI_PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --scope "$KV_SCOPE" \
  --output none 2>/dev/null || warn "Role assignment already exists"
ok "KV access granted"

# Federate MI to K8s service account
info "Creating federated credential..."
az identity federated-credential create \
  --name "kubeintellect-eso-fedcred" \
  --identity-name "$MI_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --issuer "$OIDC_ISSUER" \
  --subject "system:serviceaccount:${NAMESPACE}:kubeintellect-eso-sa" \
  --audience "api://AzureADTokenExchange" \
  --output none 2>/dev/null || warn "Federated credential already exists"
ok "Federated credential created"

# ----------------------------- STEP 6: Install ESO ---------------------------
info "Installing External Secrets Operator..."
helm repo add external-secrets https://charts.external-secrets.io 2>/dev/null || true
helm repo update
helm upgrade --install external-secrets external-secrets/external-secrets \
  -n external-secrets \
  --create-namespace \
  --set installCRDs=true \
  --wait \
  --output none
ok "ESO installed"

info "Waiting for ESO CRDs to be established..."
kubectl wait --for condition=established --timeout=60s \
  crd/externalsecrets.external-secrets.io \
  crd/secretstores.external-secrets.io
ok "CRDs ready"

# ----------------------------- STEP 7: Deploy ESO resources ------------------
info "Applying SecretStore and ExternalSecrets..."
helm template kubeintellect "$CHART_PATH" \
  -n "$NAMESPACE" \
  -f "$VALUES_FILE" \
  --set azureKeyVault.clientId="$MI_CLIENT_ID" \
  --set azureKeyVault.tenantId="$TENANT_ID" \
  -s templates/secret-store.yaml \
  -s templates/external-secrets.yaml | kubectl apply -f -
ok "ESO resources applied"

# ----------------------------- STEP 8: Verify --------------------------------
info "Waiting 20s for secrets to sync..."
sleep 20

info "Forcing sync..."
for es in kubeintellect-core-secret postgres-secret librechat-secret ghcr-creds; do
  kubectl annotate externalsecret "$es" -n "$NAMESPACE" force-sync=$(date +%s) --overwrite --output none 2>/dev/null || true
done
sleep 10

echo ""
echo "========================================="
echo " SecretStore status"
echo "========================================="
kubectl get secretstore -n "$NAMESPACE"

echo ""
echo "========================================="
echo " ExternalSecret sync status"
echo "========================================="
kubectl get externalsecret -n "$NAMESPACE"

echo ""
# Check all ready
NOT_READY=$(kubectl get externalsecret -n "$NAMESPACE" --no-headers | grep -v "True" | wc -l)
if [[ "$NOT_READY" -eq 0 ]]; then
  ok "All secrets synced successfully from Azure Key Vault!"
else
  warn "$NOT_READY ExternalSecret(s) not ready yet — check with:"
  echo "  kubectl describe externalsecret -n $NAMESPACE"
fi
