# Installation & Deployment

For **local development** (running the app on your machine with Kind), see [`docs/development.md`](development.md).
This document covers cluster deployment only.

---

## Prerequisites

| Tool | Purpose |
|------|---------|
| `kubectl` | Cluster access |
| `helm` 3.x | Chart deployment |
| `az` CLI | Azure deployment |
| `terraform` | Azure infra provisioning |
| `kind` | Local cluster (Kind only) |

---

## Azure (AKS)

### 1. Install Azure CLI

```bash
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
az --version
az login
```

### 2. Set up infrastructure

```bash
make azure-set-up-environment    # install terraform, kubectl, etc.
make azure-validate-environment  # check prerequisites
make azure-cluster-create        # create AKS cluster (interactive)
```

### 3. Set up secrets (required before cert-manager and Helm deploy)

All secrets live in Azure Key Vault. ESO syncs them to Kubernetes on deploy.
See `docs/secret-management.md` for the full setup.

```bash
cp .env.example .env
# Fill in at minimum: AZURE_OPENAI_API_KEY, POSTGRES_PASSWORD, KUBEINTELLECT_API_KEY, GHCR_PAT

# SUBSCRIPTION and TENANT_ID must be passed as env vars — do not hardcode in scripts.
SUBSCRIPTION="<your-azure-subscription-id>" \
TENANT_ID="<your-azure-tenant-id>" \
  ./scripts/ops/setup-secrets-infra.sh
```

This provisions Azure Key Vault, the Managed Identity, ESO, and all secrets —
including the MongoDB root credentials (`MONGO-ROOT-PASSWORD`, `MONGO-URI`) that
are required because MongoDB now runs with authentication enabled.

> **Order matters:** secrets must be provisioned before the Helm deploy.
> The Helm chart creates `ExternalSecret` resources that reference Key Vault on startup.
> If ESO or the secrets are missing, pods will fail to start.

### 4. Install cert-manager (required before Helm deploy)

cert-manager must be present **before** the Helm chart is applied — the chart creates a `ClusterIssuer` resource that requires cert-manager CRDs to exist first.

```bash
make azure-install-cert-manager
```

This installs cert-manager `v1.14.5` into the `cert-manager` namespace and waits for the webhook to be ready. Safe to re-run on a cluster that already has it.

### 5. Create DNS A records (one-time, per cluster)

> **DNS must be set and propagated BEFORE the Helm deploy.** cert-manager uses HTTP-01
> ACME challenges that require an incoming request to hit your pods. If DNS is not set,
> the challenge fails and cert issuance is blocked. If you deploy first and DNS propagates
> later, delete the failed `CertificateRequest` objects and cert-manager will retry.

After the cluster is up, get the Azure Load Balancer public IP assigned to ingress-nginx:

```bash
kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

> Wait until the `EXTERNAL-IP` field is populated (not `<pending>`) before proceeding.

Create these `A` records in your DNS provider (Azure DNS, Cloudflare, etc.):

| Hostname | Type | Value |
|----------|------|-------|
| `chat.kubeintellect.com` | A | `<LB IP>` |
| `api.kubeintellect.com` | A | `<LB IP>` |
| `prometheus.kubeintellect.com` | A | `<LB IP>` |
| `grafana.kubeintellect.com` | A | `<LB IP>` |

Verify propagation before deploying: `dig chat.kubeintellect.com`

> **Let's Encrypt rate limit:** Let's Encrypt allows a maximum of **5 duplicate certificates per registered domain per week**. Do not delete and recreate the cluster repeatedly — the TLS `Secret`s survive `helm upgrade` as long as the namespace is not deleted. See `docs/runbook.md` → TLS & Certificates for recovery steps.

### 6. Deploy

```bash
make azure-kubeintellect-deploy
```

The chart deploys the `letsencrypt-prod` `ClusterIssuer` and all three ingresses with TLS. cert-manager requests certificates automatically via HTTP-01 ACME challenge. Certificates are typically issued within 1–2 minutes.

### 7. Access

| Service | URL |
|---------|-----|
| LibreChat UI | https://chat.kubeintellect.com |
| KubeIntellect API | https://api.kubeintellect.com |
| Prometheus | https://prometheus.kubeintellect.com |
| Grafana | https://grafana.kubeintellect.com |

No `/etc/hosts` editing required. Run the post-deploy smoke check:

```bash
# All pods running
kubectl get pods -n kubeintellect

# All certificates issued
kubectl get certificates -n kubeintellect

# All ExternalSecrets synced (READY=True)
kubectl get externalsecret -n kubeintellect

# Verify MongoDB auth is active (should return pong, not an auth error)
kubectl exec -n kubeintellect deploy/mongodb -- \
  mongosh -u mongouser \
  -p "$(kubectl get secret mongodb-secret -n kubeintellect -o jsonpath='{.data.password}' | base64 -d)" \
  --eval "db.adminCommand('ping')"
```

### Tear down

```bash
make azure-cluster-cleanup
```

> **Before tearing down:** If you plan to recreate the cluster soon, note that deleting the namespace removes the TLS `Secret`s and cert-manager will re-issue certificates on next deploy. This counts against the Let's Encrypt rate limit (5/week). If you're near the limit, back up the cert Secrets first:
> ```bash
> kubectl get secret chat-kubeintellect-tls api-kubeintellect-tls monitoring-kubeintellect-tls \
>   -n kubeintellect -o yaml > backups/tls-secrets-$(date +%Y%m%d).yaml
> ```

---

## Kind (Local Cluster)

See `docs/development.md` for the full Kind-based dev workflow including hot-reload setup.

### Quick start

```bash
make kind-kubeintellect-clean-deploy   # create cluster + deploy all services
make kind-dev-create-user              # create dev LibreChat user
make kind-dev-deploy                   # switch to hot-reload dev mode
make port-forward-librechat            # UI → localhost:3080
```

### Tear down

```bash
make kind-cluster-cleanup
```

---

## After Deployment

### Restart after code changes

```bash
make restart
```

### Check health

```bash
curl http://localhost:8000/healthz    # liveness
curl http://localhost:8000/health     # readiness (checks K8s + DB)
```

### View logs

```bash
kubectl logs -n kubeintellect -l app=kubeintellect-core -f
```

---

## Rotating Secrets (Azure Key Vault → ESO → pods)

Secrets live in Azure Key Vault and are synced to Kubernetes by External Secrets Operator (ESO). Default sync interval: 1 hour. To rotate a secret immediately:

**1. Update `.env` with the new value:**
```bash
AZURE_OPENAI_API_KEY=<new-key>   # or whichever secret changed
```

**2. Push the updated value to Key Vault:**
```bash
SUBSCRIPTION="<your-subscription-id>" TENANT_ID="<your-tenant-id>" \
  ./scripts/ops/setup-secrets-infra.sh
```

**3. Force immediate ESO sync (skips the 1-hour wait):**
```bash
kubectl annotate externalsecret -n kubeintellect --all \
  force-sync=$(date +%s) --overwrite
```

**4. Restart pods to load the new secret:**
```bash
kubectl rollout restart deployment kubeintellect-core -n kubeintellect
kubectl rollout status deployment kubeintellect-core -n kubeintellect
```

**Verify:**
```bash
# READY=True + SYNCED=True = K8s Secret is up to date
kubectl get externalsecret -n kubeintellect

# Confirm the pod has the new value
kubectl exec -n kubeintellect deploy/kubeintellect-core -- printenv AZURE_OPENAI_API_KEY
```
