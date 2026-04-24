# Secret Management — KubeIntellect

Single source of truth for all secrets: **Azure Key Vault**, synced to Kubernetes via **External Secrets Operator (ESO)**.

---

## Architecture

```
Developer / CI
     │
     ▼
Azure Key Vault (kubeintellect-kv)
     │
     │  ESO syncs every 1h
     ▼
Kubernetes Secrets  ──►  Pods (env vars)
  kubeintellect-core-secret
  postgres-secret
  librechat-secret   (includes MONGO_URI with credentials)
  mongodb-secret
  ghcr-creds
```

**No secrets in git. Ever.**

---

## What Lives Where

| Secret | Key Vault Name | K8s Secret | Used By |
|---|---|---|---|
| Azure OpenAI API Key | `AZURE-OPENAI-API-KEY` | `kubeintellect-core-secret` | kubeintellect-core pod |
| LangChain API Key | `LANGCHAIN-API-KEY` | `kubeintellect-core-secret` | kubeintellect-core pod |
| Postgres Password | `POSTGRES-PASSWORD` | `kubeintellect-core-secret`, `postgres-secret` | app + postgres pod |
| JWT Secret | `JWT-SECRET` | `librechat-secret` | LibreChat |
| JWT Refresh Secret | `JWT-REFRESH-SECRET` | `librechat-secret` | LibreChat |
| Credentials Key | `CREDS-KEY` | `librechat-secret` | LibreChat |
| Credentials IV | `CREDS-IV` | `librechat-secret` | LibreChat |
| Meilisearch Master Key | `MEILI-MASTER-KEY` | `librechat-secret` | LibreChat + Meilisearch |
| KubeIntellect API Key | `KUBEINTELLECT-API-KEY` | `librechat-secret` | LibreChat → KubeIntellect |
| **MongoDB URI** | **`MONGO-URI`** | **`librechat-secret`** | **LibreChat + GDPR CronJob** |
| **MongoDB Root Password** | **`MONGO-ROOT-PASSWORD`** | **`mongodb-secret`** | **MongoDB pod (auth init)** |
| GHCR Username | `GHCR-USERNAME` | `ghcr-creds` | Image pull |
| GHCR PAT | `GHCR-PAT` | `ghcr-creds` | Image pull |

> `MONGO-URI` format: `mongodb://mongouser:<password>@mongodb:27017/LibreChat?authSource=admin`
> — generated automatically by `setup-secrets-infra.sh` using the same random password as `MONGO-ROOT-PASSWORD`.

---

## Azure Resources Created

| Resource | Name | Purpose |
|---|---|---|
| Key Vault | `kubeintellect-kv` | Stores all secrets |
| Managed Identity | `kubeintellect-eso-mi` | ESO authenticates to KV as this identity |
| Federated Credential | `kubeintellect-eso-fedcred` | Links K8s service account → Managed Identity |
| K8s Service Account | `kubeintellect-eso-sa` | ESO uses this SA inside the cluster |

---

## First-Time Setup

### Prerequisites
- `az` CLI logged in
- `kubectl` connected to `aks-kubeintellect`
- `helm` installed

---

### Option A — Automatic (recommended)

**1. Fill in `.env`:**

```bash
cp .env.example .env
# Fill in the required values — at minimum:
```

```bash
AZURE_OPENAI_API_KEY="..."   # Azure Portal → OpenAI resource → Keys
LANGCHAIN_API_KEY="..."       # smith.langchain.com → Settings → API Keys
POSTGRES_PASSWORD="..."       # any strong password
KUBEINTELLECT_API_KEY="..."  # any strong random string (openssl rand -hex 32)
GHCR_PAT="..."               # GitHub PAT with read:packages scope only
```

**2. Run:**

```bash
# SUBSCRIPTION and TENANT_ID are required env vars — not hardcoded in the script.
SUBSCRIPTION="<your-azure-subscription-id>" \
TENANT_ID="<your-azure-tenant-id>" \
  ./scripts/ops/setup-secrets-infra.sh
```

The script does everything:

| Step | What it does |
|---|---|
| 1 | Creates Azure Key Vault with RBAC |
| 2 | Grants your user write access |
| 3 | Stores all 13 secrets (incl. MongoDB root password + authenticated URI) |
| 4 | Enables Workload Identity on AKS |
| 5 | Creates Managed Identity + grants it KV read access |
| 6 | Federates Managed Identity to K8s service account |
| 7 | Installs External Secrets Operator via Helm |
| 8 | Deploys SecretStore + ExternalSecrets to cluster |
| 9 | Verifies all secrets synced |

---

### Option B — Manual (step-by-step)

Use this if you need fine-grained control or the automated script fails partway through.

**Set variables once, reuse below — replace all `<...>` placeholders:**

```bash
export SUBSCRIPTION_ID="<your-azure-subscription-id>"
export RESOURCE_GROUP="<your-resource-group>"          # e.g. kubeintellect-rg
export AKS_CLUSTER="<your-aks-cluster-name>"           # e.g. kubeintellect-aks
export KV_NAME="<your-keyvault-name>"                  # e.g. kubeintellect-kv (globally unique)
export LOCATION="<azure-region>"                        # e.g. eastus
export NAMESPACE="kubeintellect"
```

**Step 1 — Create Azure Key Vault:**

```bash
az login
az account set --subscription "$SUBSCRIPTION_ID"

az keyvault create \
  --name "$KV_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --enable-rbac-authorization true
```

**Step 2 — Store all secrets:**

```bash
# KubeIntellect core
az keyvault secret set --vault-name "$KV_NAME" --name "AZURE-OPENAI-API-KEY" --value "PASTE_VALUE_HERE"
az keyvault secret set --vault-name "$KV_NAME" --name "LANGCHAIN-API-KEY"    --value "PASTE_VALUE_HERE"
az keyvault secret set --vault-name "$KV_NAME" --name "POSTGRES-PASSWORD"    --value "PASTE_VALUE_HERE"

# LibreChat — auto-generated, run as-is
az keyvault secret set --vault-name "$KV_NAME" --name "JWT-SECRET"            --value "$(openssl rand -hex 32)"
az keyvault secret set --vault-name "$KV_NAME" --name "JWT-REFRESH-SECRET"    --value "$(openssl rand -hex 32)"
az keyvault secret set --vault-name "$KV_NAME" --name "CREDS-KEY"             --value "$(openssl rand -hex 32)"
az keyvault secret set --vault-name "$KV_NAME" --name "CREDS-IV"              --value "$(openssl rand -hex 16)"
az keyvault secret set --vault-name "$KV_NAME" --name "MEILI-MASTER-KEY"      --value "$(openssl rand -base64 32)"
az keyvault secret set --vault-name "$KV_NAME" --name "KUBEINTELLECT-API-KEY" --value "PASTE_VALUE_HERE"

# MongoDB auth — auto-generated, run as-is
MONGO_ROOT_PASS="$(openssl rand -base64 32)"
az keyvault secret set --vault-name "$KV_NAME" --name "MONGO-ROOT-PASSWORD" --value "$MONGO_ROOT_PASS"
az keyvault secret set --vault-name "$KV_NAME" --name "MONGO-URI" \
  --value "mongodb://mongouser:${MONGO_ROOT_PASS}@mongodb:27017/LibreChat?authSource=admin"

# Docker registry (GitHub PAT with read:packages scope only)
az keyvault secret set --vault-name "$KV_NAME" --name "GHCR-USERNAME" --value "<YOUR_GITHUB_USERNAME>"
az keyvault secret set --vault-name "$KV_NAME" --name "GHCR-PAT"      --value "PASTE_VALUE_HERE"
```

**Step 3 — Enable Workload Identity on AKS:**

```bash
az aks update \
  --resource-group "$RESOURCE_GROUP" \
  --name "$AKS_CLUSTER" \
  --enable-oidc-issuer \
  --enable-workload-identity

export OIDC_ISSUER=$(az aks show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$AKS_CLUSTER" \
  --query "oidcIssuerProfile.issuerUrl" -o tsv)
```

**Step 4 — Create Managed Identity for ESO:**

```bash
export MI_NAME="kubeintellect-eso-mi"

az identity create --name "$MI_NAME" --resource-group "$RESOURCE_GROUP"

export MI_CLIENT_ID=$(az identity show --name "$MI_NAME" --resource-group "$RESOURCE_GROUP" --query clientId -o tsv)
export MI_PRINCIPAL_ID=$(az identity show --name "$MI_NAME" --resource-group "$RESOURCE_GROUP" --query principalId -o tsv)
export KV_SCOPE=$(az keyvault show --name "$KV_NAME" --query id -o tsv)

az role assignment create \
  --role "Key Vault Secrets User" \
  --assignee-object-id "$MI_PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --scope "$KV_SCOPE"
```

**Step 5 — Federate identity to K8s service account:**

```bash
az identity federated-credential create \
  --name "kubeintellect-eso-fedcred" \
  --identity-name "$MI_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --issuer "$OIDC_ISSUER" \
  --subject "system:serviceaccount:${NAMESPACE}:kubeintellect-eso-sa" \
  --audience "api://AzureADTokenExchange"
```

**Step 6 — Install External Secrets Operator:**

```bash
helm repo add external-secrets https://charts.external-secrets.io
helm repo update

helm upgrade --install external-secrets external-secrets/external-secrets \
  -n external-secrets \
  --create-namespace \
  --set installCRDs=true
```

**Step 7 — Deploy via Helm:**

```bash
helm upgrade --install kubeintellect ./charts/kubeintellect \
  -n "$NAMESPACE" \
  -f charts/kubeintellect/values-azure.yaml \
  --set azureKeyVault.url="https://${KV_NAME}.vault.azure.net/" \
  --set azureKeyVault.tenantId="$(az account show --query tenantId -o tsv)" \
  --set azureKeyVault.clientId="$MI_CLIENT_ID"
```

**Step 8 — Verify secrets are synced:**

```bash
kubectl get externalsecret -n "$NAMESPACE"
kubectl get secret kubeintellect-core-secret librechat-secret postgres-secret ghcr-creds -n "$NAMESPACE"
```

---

## Local Development

Fetch secrets from Key Vault into your local `.env` (gitignored):

```bash
export AZURE_KV_NAME="kubeintellect-kv"
./scripts/dev/fetch-secrets.sh
```

Or with a flag:

```bash
./scripts/dev/fetch-secrets.sh --vault kubeintellect-kv
```

---

## Rotating a Secret

```bash
# 1. Update in Key Vault
az keyvault secret set --vault-name "kubeintellect-kv" --name "AZURE-OPENAI-API-KEY" --value "<new-value>"

# 2. Force immediate re-sync (otherwise waits up to 1h)
kubectl annotate externalsecret kubeintellect-core-secret \
  -n kubeintellect force-sync=$(date +%s) --overwrite
```

---

## Checking Sync Status

```bash
# Are all secrets synced?
kubectl get externalsecret -n kubeintellect

# Inspect a specific one
kubectl describe externalsecret kubeintellect-core-secret -n kubeintellect

# Check the resulting K8s secrets exist
kubectl get secret -n kubeintellect
```

Expected output — all `READY: True`:
```
NAME                        STATUS         READY
ghcr-creds                  SecretSynced   True
kubeintellect-core-secret   SecretSynced   True
librechat-secret            SecretSynced   True
mongodb-secret              SecretSynced   True
postgres-secret             SecretSynced   True
```

---

## Helm Chart Integration

The chart (`charts/kubeintellect/`) uses these values in `values-azure.yaml`:

```yaml
azureKeyVault:
  url: "https://kubeintellect-kv.vault.azure.net/"
  clientId: "<managed-identity-client-id>"   # from: az identity show --query clientId
  tenantId: "<azure-tenant-id>"             # from: az account show --query tenantId

externalSecrets:
  enabled: true
  refreshInterval: "1h"
```

The relevant templates:
- `charts/kubeintellect/templates/secret-store.yaml` — SecretStore + ESO service account
- `charts/kubeintellect/templates/external-secrets.yaml` — 5 ExternalSecret resources (`kubeintellect-core-secret`, `postgres-secret`, `librechat-secret`, `mongodb-secret`, `ghcr-creds`)

The legacy `secrets:` block in `values-azure.yaml` is disabled (`enabled: false` on all entries) — it exists only as a reference for kind/local clusters without AKV.

---

## Full Infrastructure Overview

```
KubeIntellect Infrastructure
├── Terraform  (infrastructure/azure/)
│   ├── main.tf              — AKS cluster, resource group
│   ├── variables.tf         — cluster config, node pool, monitoring
│   ├── outputs.tf           — FQDN, kubeconfig, ingress IP
│   └── modules/
│       ├── ingress-nginx/   — Helm: ingress controller
│       └── kube-prometheus/ — Helm: Grafana + Prometheus
│
├── Helm Chart  (charts/kubeintellect/)
│   ├── values-azure.yaml    — Azure production values (no secrets)
│   ├── values-kind.yaml     — Local dev values
│   └── templates/
│       ├── secret-store.yaml      — ESO SecretStore (AKV)
│       ├── external-secrets.yaml  — 4 ExternalSecrets
│       ├── deployments.yaml       — 5 app deployments
│       ├── configmaps.yaml        — app config (non-secret)
│       ├── pvcs.yaml              — 5 persistent volumes
│       ├── ingress.yaml           — 3 ingress rules
│       ├── rbac.yaml              — roles + bindings
│       └── ...
│
└── Scripts
    ├── ops/
    │   ├── setup-secrets-infra.sh    ← full auto setup (run this)
    │   ├── setup-keyvault-secrets.sh ← populate KV secrets only
    │   └── cleanup-kubeintellect.sh
    ├── azure/
    │   └── azure-deploy-kubeintellect.sh
    ├── kind/
    │   ├── create-kind-cluster.sh
    │   └── cleanup-kind-cluster.sh
    └── dev/
        ├── fetch-secrets.sh      ← pull secrets from AKV into local .env 
        └── create-dev-user.sh  ← create LibreChat dev user
```

---

## Common Pitfall — `kubectl --from-env-file` Does Not Strip Shell Quotes

**Do not** populate Kubernetes secrets directly from `.env` using:

```bash
# WRONG — do not do this
kubectl create secret generic my-secret --from-env-file=.env
```

`kubectl --from-env-file` reads each line literally and does **not** strip shell quoting.
A line like `POSTGRES_PASSWORD="my-pass"` stores the value as `"my-pass"` (with the surrounding double-quotes), which silently breaks the app.

**Correct pattern — use `source` + explicit assignment:**

```bash
# Correct — shell strips quotes before passing the value
set -a; source .env; set +a
az keyvault secret set --vault-name "$KV_NAME" --name "POSTGRES-PASSWORD" --value "$POSTGRES_PASSWORD"
```

This is exactly what `scripts/ops/setup-secrets-infra.sh` does.
If you ever need a quote-free `.env` file for another tool, pre-process it first:

```bash
# Strip surrounding single/double quotes from values before passing to --from-env-file
sed 's/^\([^=]*\)=["'\'']\(.*\)["'\'']/\1=\2/' .env > .env.stripped
kubectl create secret generic my-secret --from-env-file=.env.stripped
rm .env.stripped
```

---

## Security Notes

- `values-azure.yaml` contains **no secrets** — only Key Vault URL, Managed Identity `clientId`, and `tenantId`. These are Azure resource identifiers that cannot be used to authenticate without the cluster's OIDC token.
- `.env` is gitignored — never commit it.
- GHCR PAT scope: `read:packages` only — no write access to the registry.
- Change the Postgres password from its default via Key Vault rotation before any production deployment.
