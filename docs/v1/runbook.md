# KubeIntellect — Operations & Troubleshooting

This document covers deployment procedures, known issues, and operational runbooks for KubeIntellect.

---

## Deployment

### Working kubectl secret creation

```bash
kubectl delete secret ghcr-creds -n kubeintellect
kubectl create secret docker-registry ghcr-creds \
  --docker-server=ghcr.io \
  --docker-username=<YOUR_GITHUB_USERNAME> \
  --docker-password='<YOUR_GHCR_PAT>' \
  --docker-email=<YOUR_EMAIL> \
  -n kubeintellect \
  --dry-run=client -o yaml > docker-secret.yaml
kubectl apply -f docker-secret.yaml
```

### App pods crash on first deploy (database startup race)

**Symptom:** `kubeintellect-core` pods crash immediately after first deploy.
**Cause:** Pods started before PostgreSQL and the migration job were ready. The app connects to the database at startup and fails if it is not yet available.
**Quick fix:** Delete the ReplicaSet — the Deployment will create a new one after the database is ready:

```bash
kubectl delete rs -n kubeintellect -l app=kubeintellect-core
```

**Permanent fix:** Apply the following initContainer patch to gate app startup on database readiness:

```bash
kubectl -n kubeintellect patch deploy kubeintellect-core --type='json' -p='[
  {
    "op":"add",
    "path":"/spec/template/spec/initContainers",
    "value":[{
      "name":"wait-for-db-and-migration",
      "image":"postgres:15",
      "env":[
        {"name":"PGHOST","value":"postgres.kubeintellect.svc.cluster.local"},
        {"name":"PGPORT","value":"5432"},
        {"name":"PGDATABASE","value":"kubeintellectdb"},
        {"name":"PGUSER","value":"kubeuser"},
        {"name":"PGPASSWORD","valueFrom":{"secretKeyRef":{"name":"postgres-secret","key":"password"}}}
      ],
      "command":["/bin/sh","-lc"],
      "args":["until pg_isready -h \"$PGHOST\" -p \"$PGPORT\" -U \"$PGUSER\" -d \"$PGDATABASE\" -t 2 >/dev/null; do sleep 2; done; until psql -Atqc \"SELECT to_regclass('"'"'public.workflow_checkpoints'"'"') IS NOT NULL;\" | grep -q t; do sleep 2; done; echo DB ready."]
    }]
  }
]'
```

### Helm hook for migration job

Add the following annotations to `job-migrate.yaml` to ensure the migration job runs before app pods start:

```yaml
annotations:
  "helm.sh/hook": pre-install,pre-upgrade
  "helm.sh/hook-weight": "-10"
  "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
```

---

## TLS & Certificates (Azure only)

### Check certificate status

```bash
# All certificates in the kubeintellect namespace
kubectl get certificates -n kubeintellect

# Full details on a specific cert (shows ACME challenge progress)
kubectl describe certificate chat-kubeintellect-tls -n kubeintellect

# Check if ACME HTTP-01 challenge is stuck
kubectl get challenges -n kubeintellect
kubectl describe challenge -n kubeintellect <challenge-name>

# Check cert-manager logs if something is wrong
kubectl logs -n cert-manager deploy/cert-manager | tail -50
```

### Certificate stuck in `False` / Pending

**Most common cause:** DNS A record not propagated yet.

```bash
# Verify DNS resolves to the correct LB IP
dig chat.kubeintellect.com
# Should return the Azure LB IP — if NXDOMAIN or wrong IP, fix DNS first
```

**Other causes to rule out:**
- `kubectl describe challenge` — look for `type: HTTP-01` and check the solver pod is running
- The ACME HTTP-01 challenge requires port 80 to be reachable from the public internet. If the Azure LB security group blocks port 80, the challenge will fail. Port 80 must be open even if you want HTTPS-only (cert-manager uses it for challenge verification).
- After fixing DNS or firewall, delete the failed `CertificateRequest` to force a retry:
  ```bash
  kubectl delete certificaterequest -n kubeintellect --all
  ```

### Let's Encrypt rate limits

Let's Encrypt enforces a limit of **5 duplicate certificates per registered domain per week**. `*.kubeintellect.com` counts as one registered domain — issuing certs for `chat.`, `api.`, `prometheus.`, and `grafana.` subdomains in a single deploy costs **4** of those 5 slots.

**This becomes a problem when:**
- You delete the `kubeintellect` namespace and redeploy multiple times in the same week — each deploy re-requests all 4 certs
- You repeatedly run `make azure-kubeintellect-fresh-deploy` during testing

**How to check if you've hit the limit:**
```bash
kubectl describe certificate chat-kubeintellect-tls -n kubeintellect
# Look for: "429 urn:ietf:params:acme:error:rateLimited"
```

**Recovery options:**

1. **Wait** — the rate limit window resets 7 days from the first issuance. Check the reset time at https://letsdebug.net (enter the domain).

2. **Preserve cert Secrets across redeploys** — back up before deleting the namespace:
   ```bash
   kubectl get secret chat-kubeintellect-tls api-kubeintellect-tls monitoring-kubeintellect-tls \
     -n kubeintellect -o yaml > backups/tls-secrets-$(date +%Y%m%d).yaml
   ```
   Restore after redeploy:
   ```bash
   kubectl apply -f backups/tls-secrets-YYYYMMDD.yaml
   ```
   cert-manager will see the Secrets already exist and skip re-issuance.

3. **Use staging for testing** — if you're iterating on the cluster setup, switch `values-azure.yaml` `certManager.email` temporarily and point the `ClusterIssuer` server to the staging URL (`https://acme-staging-v02.api.letsencrypt.org/directory`) — staging has much higher limits and issues untrusted certs. Switch back to prod when done.

### Manually renew a certificate

cert-manager auto-renews certificates 30 days before expiry. To force immediate renewal:

```bash
kubectl annotate certificate chat-kubeintellect-tls -n kubeintellect \
  cert-manager.io/issueTemporary="true" --overwrite
# Then remove the annotation — this triggers a re-issue
kubectl annotate certificate chat-kubeintellect-tls -n kubeintellect \
  cert-manager.io/issueTemporary-
```

Or delete the `CertificateRequest` and cert-manager will recreate it:

```bash
kubectl delete certificaterequest -n kubeintellect \
  $(kubectl get certificaterequest -n kubeintellect -o name | grep chat)
```

### ClusterIssuer not ready after deploy

```bash
kubectl describe clusterissuer letsencrypt-prod
```

If `Status: False` with `ACME account not registered`:
- cert-manager is trying to register an ACME account with Let's Encrypt; this requires outbound HTTPS from the cluster to `acme-v02.api.letsencrypt.org`
- Check that the AKS node pool has outbound internet access (NAT gateway or public IPs on nodes)

If `cert-manager CRD not found` error during `helm upgrade`:
- cert-manager was not installed before Helm deploy — run `make azure-install-cert-manager` then re-run `make azure-kubeintellect-deploy`

---

## Kind Cluster

### Multi-node Kind + Ingress problem

**Symptom:** Works with 1 node, fails with multiple nodes.
**Root cause:** Host ports 80/443 are only published on the control-plane container. If ingress-nginx lands on a worker node, traffic never reaches it.

**Fix — recreate cluster with extraPortMappings + pin ingress:**

```yaml
# kind-cluster.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
  extraPortMappings:
  - containerPort: 80
    hostPort: 80
  - containerPort: 443
    hostPort: 443
- role: worker
- role: worker
```

```bash
kind delete cluster --name kind
kind create cluster --config kind-cluster.yaml --name kind
kubectl label node kind-control-plane ingress-ready=true
kubectl -n ingress-nginx patch deploy ingress-nginx-controller -p '{
  "spec":{"template":{"spec":{
    "nodeSelector":{"ingress-ready":"true"},
    "tolerations":[
      {"key":"node-role.kubernetes.io/control-plane","operator":"Exists","effect":"NoSchedule"}
    ]
  }}}}'
```

**Quick alternative (no cluster rebuild):** port-forward

```bash
kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 8080:80
```

---

## Debugging

### App logs look fine but Kubernetes queries fail in chat

```bash
kubectl logs -n kubeintellect kubeintellect-core-<pod-id>
```

Usually means the Postgres migration hasn't run. Apply `job-migrate.yaml` manually:

```bash
kubectl apply -f charts/kubeintellect/templates/job-migrate.yaml
```

### CoreDNS 502 Bad Gateway in LibreChat

**Root cause:** CoreDNS misconfiguration causes service DNS resolution to fail.
**Full guide:** `docs/hitl.md` has the investigation steps.
**Quick check:**

```bash
kubectl -n kubeintellect exec -it deploy/librechat -- nslookup mongodb.kubeintellect.svc.cluster.local
```

---

## Database

### workflow_checkpoints schema

The schema is created automatically at startup by `app/utils/postgres_checkpointer.py`. Composite primary key on `(user_id, thread_id)` allows multiple conversation threads per user.

### Seed failure patterns (one-time, after first deploy)

The `failure_patterns` table is auto-created on startup but starts empty. Run the seed script once to populate the 30 canonical Kubernetes failure patterns:

```bash
# Local dev
uv run python scripts/seed_failure_patterns.py

# Against a remote DB (set POSTGRES_* env vars first)
POSTGRES_HOST=<host> POSTGRES_USER=kubeuser POSTGRES_PASSWORD=<pw> \
  uv run python scripts/seed_failure_patterns.py
```

The script is idempotent — safe to re-run; existing patterns are skipped.

### Check if migration ran

```bash
kubectl exec -n kubeintellect deploy/postgres -- \
  psql -U kubeuser -d kubeintellectdb -c "\dt workflow_checkpoints"
```

### Backup

> **When you don't need a backup:** `helm upgrade` keeps the PVC intact. Only backup before deleting the namespace, migrating clusters, or for an offline copy.

On-demand dump (creates `backups/kubeintellect-pg-YYYYMMDD-HHMMSS.dump`):

```bash
make postgres-backup
```

Manual equivalent:

```bash
kubectl exec -n kubeintellect deploy/postgres -- \
  env PGPASSWORD=$(kubectl get secret -n kubeintellect postgres-secret \
    -o jsonpath='{.data.password}' | base64 -d) \
  pg_dump -U kubeuser -d kubeintellectdb --format=custom --compress=9 \
  > backups/kubeintellect-pg-$(date +%Y%m%d-%H%M%S).dump
```

A daily automated `pg_dump` also runs via the `postgres-backup` CronJob (enabled in Azure/production values). Backups land on the `postgres-backup-pvc` at `/backups/` and are retained for 30 days.

### Restore

```bash
make postgres-restore                          # auto-selects latest backups/*.dump
make postgres-restore FILE=backups/my.dump     # specific file
```

Manual equivalent:

```bash
kubectl exec -i -n kubeintellect deploy/postgres -- \
  env PGPASSWORD=$(kubectl get secret -n kubeintellect postgres-secret \
    -o jsonpath='{.data.password}' | base64 -d) \
  pg_restore -U kubeuser -d kubeintellectdb --clean --if-exists \
  < backups/kubeintellect-pg-YYYYMMDD-HHMMSS.dump
```

To restore from a backup stored on the `postgres-backup-pvc` (from the CronJob):

```bash
# list available CronJob backups
kubectl exec -n kubeintellect deploy/postgres -- ls /backups/

# restore directly from PVC
kubectl exec -i -n kubeintellect deploy/postgres -- \
  env PGPASSWORD=$(kubectl get secret -n kubeintellect postgres-secret \
    -o jsonpath='{.data.password}' | base64 -d) \
  pg_restore -U kubeuser -d kubeintellectdb --clean --if-exists \
  /backups/pg-kubeintellectdb-YYYYMMDD-HHMMSS.dump
```

---

## MongoDB (LibreChat)

> **When you don't need a backup:** If you keep the same namespace, keep the MongoDB PVC, and just run `helm upgrade` — data is already persisted. Only backup when deleting the namespace, moving clusters, or wanting an offline copy.

### Backup

```bash
kubectl exec -n kubeintellect deploy/mongodb -- \
  mongodump --host mongodb.kubeintellect.svc.cluster.local --port 27017 \
  --db LibreChat --archive --gzip \
  > backups/librechat-$(date +%Y%m%d-%H%M%S).gz
```

Or via Makefile: `make mongo-backup`

If `mongodump` is missing from the image:

```bash
kubectl run mongo-backup -n kubeintellect \
  --rm -i --restart=Never --image=mongo:6 --command -- \
  bash -c 'mongodump --host mongodb.kubeintellect.svc.cluster.local --port 27017 --db LibreChat --archive --gzip' \
  > backups/librechat-$(date +%Y%m%d-%H%M%S).gz
```

### Restore

```bash
cat backups/librechat-YYYYMMDD-HHMMSS.gz | \
kubectl exec -i -n kubeintellect deploy/mongodb -- \
  mongorestore --host mongodb.kubeintellect.svc.cluster.local --port 27017 \
  --db LibreChat --drop --archive --gzip
```

Or via Makefile: `make mongo-restore FILE=backups/librechat-....gz`

If `mongorestore` is missing from the image:

```bash
kubectl run mongo-restore -n kubeintellect \
  --restart=Never --image=mongo:6 --command -- \
  bash -c 'mongorestore --host mongodb.kubeintellect.svc.cluster.local --port 27017 --db LibreChat --drop --archive --gzip' \
  < backups/librechat-YYYYMMDD-HHMMSS.gz
```

---

## OTel Noise Suppression

**What is suppressed:** `ValueError: Token was created in a different Context` logged by the `opentelemetry.*` or `langfuse.*` logger namespaces.

**Why it is benign:** When an async generator yields across asyncio task-context boundaries (e.g. during LangGraph streaming), OTel's context-variable cleanup raises this error after the span has already been recorded successfully. The span data is intact; only the internal token reset fails.

**How it is handled:** Two complementary mechanisms — (1) `_OtelContextCleanupFilter` in `app/utils/logger_config.py` drops matching log records at the handler level; (2) `safe_otel_ctx` / `async_safe_otel_ctx` in `app/utils/otel_guard.py` catch the error at call-sites in application code. Both use a conjunction predicate (logger namespace **and** exact message text) so no other `ValueError` is silently swallowed.

**To disable suppression for debugging:** Remove the `file_handler.addFilter(_otel_filter)` and `console_handler.addFilter(_otel_filter)` lines in `setup_logging()` (`app/utils/logger_config.py`). The raw errors will then appear in the log stream. Re-enable after debugging to avoid log noise.

**Permanent fix:** The structural root cause is that `execute_workflow_stream` crosses asyncio task-context boundaries. See the `TODO(root-cause)` comment in `app/orchestration/workflow.py`.

---

## Tool Registry (Runtime Tools)

- **Metadata** (name, description, status, file path, PR info) is stored in the PostgreSQL `tool_registry` table, managed by `ToolRegistryService`. Safe for concurrent multi-pod access.
- **Code files** (`gen_<id>.py`) are stored on the PVC (`kubeintellect-runtime-tools`). The PVC is written once per tool at generation time.
- Tools are loaded at startup by reading `list_tools(status="enabled")` from Postgres, then loading the corresponding `.py` files from PVC.
- Backup: `make backup` backs up both the PVC (code files) and PostgreSQL (metadata). A PVC restore alone is not sufficient — the Postgres backup must also be restored.
- Conflict checking uses exact name match (`UNIQUE` constraint on `tool_registry.name`).
