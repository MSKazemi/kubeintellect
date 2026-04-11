#!/bin/bash
# Fetch secrets from Azure Key Vault into local .env file
# Usage: ./scripts/dev/fetch-secrets.sh [--vault <vault-name>]
set -euo pipefail

KV_NAME="${AZURE_KV_NAME:-}"
ENV_FILE=".env"

# Allow override via flag
while [[ $# -gt 0 ]]; do
  case $1 in
    --vault) KV_NAME="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$KV_NAME" ]]; then
  echo "Error: Key Vault name not set. Export AZURE_KV_NAME or pass --vault <name>"
  exit 1
fi

fetch() {
  local secret_name="$1"
  local env_key="$2"
  local value
  value=$(az keyvault secret show --vault-name "$KV_NAME" --name "$secret_name" --query value -o tsv 2>/dev/null) || {
    echo "  [SKIP] $secret_name not found in Key Vault"
    return
  }
  # Update existing key or append
  if grep -q "^${env_key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${env_key}=.*|${env_key}=${value}|" "$ENV_FILE"
  else
    echo "${env_key}=${value}" >> "$ENV_FILE"
  fi
  echo "  [OK]   $env_key"
}

echo "Fetching secrets from Key Vault: $KV_NAME"
echo "Writing to: $ENV_FILE"
echo ""

touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

fetch "AZURE-OPENAI-API-KEY"  "AZURE_OPENAI_API_KEY"
fetch "LANGCHAIN-API-KEY"     "LANGCHAIN_API_KEY"
fetch "POSTGRES-PASSWORD"     "POSTGRES_PASSWORD"
fetch "JWT-SECRET"            "JWT_SECRET"
fetch "JWT-REFRESH-SECRET"    "JWT_REFRESH_SECRET"
fetch "CREDS-KEY"             "CREDS_KEY"
fetch "CREDS-IV"              "CREDS_IV"
fetch "MEILI-MASTER-KEY"      "MEILI_MASTER_KEY"
fetch "KUBEINTELLECT-API-KEY" "KUBEINTELLECT_API_KEY"
fetch "GHCR-PAT"              "GHCR_PAT"

echo ""
echo "Done. Secrets written to $ENV_FILE"
