# Scripts

All operational scripts. The primary entry point for everything is the `Makefile` — these scripts are invoked by Makefile targets and are documented there. This index maps scripts to their purpose for contributors who need to read or modify them directly.

## `dev/` — Local development

| Script | Invoked by | Purpose |
|--------|-----------|---------|
| `generate-kind-secrets.sh` | `make kind-generate-secrets` | Reads `.env`, generates `charts/kubeintellect/values-kind-secrets.yaml` (gitignored) with real credentials for Kind deployments. Auto-called by all `kind-*` deploy targets. |
| `test-watch.sh` | `make test-watch` | Runs pytest in watch mode — re-runs on every `.py` file change. |
| `log-watcher.sh` | `make log-watch` | Streams pod logs and auto-analyzes them with Claude. Run in a side terminal. |
| `log-pipeline.conf` | `log-watcher.sh` | Fluent Bit config used by log-watcher. |

## `kind/` — Kind cluster management

| Script | Invoked by | Purpose |
|--------|-----------|---------|
| `create-kind-cluster.sh` | `make kind-cluster-create` | Creates the Kind cluster from a template (`infrastructure/kind/kind-2node.yaml.template`), resolving `${PROJECT_ROOT}` to the actual checkout path for hostPath mounts. |
| `cleanup-kind-cluster.sh` | `make kind-cluster-cleanup` | Destroys the Kind cluster. |
| `create-dev-user.sh` | `make kind-dev-create-user` | Creates (or skips if exists) the LibreChat dev user in MongoDB. Reads `EMAIL` and `PASSWORD` from env. Idempotent. |

## `ops/` — Operational tooling

| Script | Invoked by | Purpose |
|--------|-----------|---------|
| `setup-secrets-infra.sh` | `make azure-set-up-environment` | Provisions Azure Key Vault, Managed Identity, External Secrets Operator, and all secrets. Single source of truth for Azure secret setup. Requires `SUBSCRIPTION` and `TENANT_ID` env vars. |
| `export_chat_logs.sh` | Manual / GDPR | GDPR data export and deletion for a single LibreChat user. See `docs/gdpr.md`. |
| `lint-helm-values.sh` | `make lint-helm-values` | Checks that every app in `values-kind.yaml` is also present in `values-kind-dev.yaml` (Helm overlay completeness check). |
| `cleanup-kubeintellect.sh` | Manual | Removes the kubeintellect namespace and all resources. |
| `map_ingress_ip.sh` | Manual | Maps the ingress-nginx service IP to `/etc/hosts` entries for local access. |
| `pv_set_retain.sh` | Manual | Patches all PVs in the kubeintellect namespace to `reclaimPolicy: Retain` (prevents data loss on namespace delete). |
| `logo.sh` | Internal | Prints the KubeIntellect banner. |

## `azure/` — Azure AKS deployment

| Script | Invoked by | Purpose |
|--------|-----------|---------|
| `azure-deploy-kubeintellect.sh` | `make azure-kubeintellect-deploy` | Full AKS deploy script. |
| `setup-certificates.sh` | `make azure-install-cert-manager` | Installs cert-manager into the AKS cluster (prerequisite for TLS). |
| `fetch-secrets.sh` | Manual | Pulls secrets from Azure Key Vault into a local `.env` file. Useful after rotating secrets in the vault. |
| `access-librechat-remote.sh` | Manual | Sets up remote access to LibreChat on AKS. |
| `configure-ingress-access.sh` | `make configure-ingress` | Configures ingress-nginx for remote access. |

## `restore/` — Backup and restore

| Script | Invoked by | Purpose |
|--------|-----------|---------|
| `mongo-restore.sh` | `make mongo-restore [FILE=...]` | Restores a MongoDB backup into the running cluster. Auto-selects the latest backup if `FILE` is not specified. |
| `postgres-restore.sh` | `make postgres-restore [FILE=...]` | Restores a PostgreSQL `pg_dump` backup. Auto-selects the latest. |
| `pvc-restore.sh` | `make pvc-restore PVC=<n> FILE=<f>` | Restores any PVC from a `tar.gz` backup archive. |

## `migrations/` — Database schema migrations

| Script | Purpose |
|--------|---------|
| `001_tool_registry.sql` | Creates the `tool_registry` PostgreSQL table (run automatically on first deploy via init container). |
| `002_import_registry_json.py` | One-time migration: imports a legacy `registry.json` file into the `tool_registry` table. Run with `make migrate-registry-json`. |
