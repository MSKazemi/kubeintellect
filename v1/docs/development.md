# Development Guide

KubeIntellect is tested against a real Kubernetes cluster. The local testbed is a [Kind](https://kind.sigs.k8s.io/) cluster — it runs a full K8s API server so the agents execute real operations (pod listing, log fetching, exec, apply, etc.).

---

## Prerequisites

| Tool | Purpose |
|------|---------|
| `docker` | Build images and run Kind nodes |
| `kind` | Local Kubernetes cluster (installed by `scripts/kind/create-kind-cluster.sh` if missing) |
| `kubectl` | Cluster interaction |
| `helm` | Chart deployment |
| `uv` | Python package management |

---

## First-Time Setup

### 1. Configure credentials

`.env` is the single source of truth. All Kind secrets are generated from it automatically.

```bash
cp .env.example .env
```

Then edit `.env` and set at minimum:

```bash
# Pick one LLM provider — Azure OpenAI is the default
AZURE_OPENAI_API_KEY=<your-key>

# DEV_USER_EMAIL / DEV_USER_PASSWORD — the LibreChat login created on first deploy
DEV_USER_EMAIL=admin@kubeintellect.local   # default
DEV_USER_PASSWORD=changeme                 # change this
```

**Azure OpenAI users only — one extra step:**
Edit `charts/kubeintellect/values-kind.yaml` and set your endpoint (this is a ConfigMap value, not a secret, which is why it lives here rather than in `.env`):

```yaml
# charts/kubeintellect/values-kind.yaml  ~line 474
AZURE_OPENAI_ENDPOINT: "https://<your-resource-name>.openai.azure.com/"
```

> **Other LLM providers (OpenAI, Anthropic, Gemini, Ollama):** Set `LLM_PROVIDER=openai` (or your provider) in `.env` along with the provider's API key. No changes to `values-kind.yaml` needed.

**How secrets reach the cluster:**

```
.env
 └─► make kind-generate-secrets
       └─► charts/kubeintellect/values-kind-secrets.yaml  (gitignored)
             └─► helm upgrade -f values-kind.yaml -f values-kind-secrets.yaml
                   └─► K8s Secrets in the kubeintellect namespace
```

`make kind-generate-secrets` is called automatically by every `make kind-*` deploy target — you do not need to run it manually. It:
- Reads `AZURE_OPENAI_API_KEY`, `LANGCHAIN_API_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` from `.env`
- Generates random values for LibreChat (`JWT_SECRET`, `JWT_REFRESH_SECRET`, `CREDS_KEY`, `CREDS_IV`, `MEILI_MASTER_KEY`) and Langfuse (`ENCRYPTION_KEY`)
- Skips generation if `values-kind-secrets.yaml` already exists (use `FORCE=--force` to rotate)

To rotate secrets manually:
```bash
make kind-generate-secrets FORCE=--force   # regenerates all random values
make kind-dev-restart                       # applies updated secrets
```

### 2. Bootstrap the cluster (first time only)

```bash
make kind-kubeintellect-clean-deploy   # Step 1: create cluster + all infra (production image)
make kind-dev-deploy                   # Step 2: build local image + enable hot-reload
```

**Step 1** (`kind-kubeintellect-clean-deploy`) does:
1. Deletes any existing `testbed` Kind cluster
2. Creates `.local-data/postgres` (hostPath PV for Postgres) if missing
3. Adds `kubeintellect.chat.local`, `kubeintellect.api.local`, `langfuse.local`, `prometheus.local`, `grafana.local` to `/etc/hosts` if missing
4. Creates a fresh 1 control-plane + 3 worker cluster with ingress-nginx
5. Installs the full observability stack (Prometheus + Grafana + Loki + event-exporter)
6. Deploys all services via Helm using the **production GHCR image** (not the local dev image)
7. Creates the dev LibreChat account automatically

**Step 2** (`kind-dev-deploy`) switches the running deployment to dev mode:
- Builds the `kubeintellect:dev` image from the local Dockerfile and loads it into Kind
- Mounts your project directory into the pod at `/app` via hostPath
- Enables `uvicorn --reload` so Python file edits are picked up in ~2 seconds

> **Important:** After `kind-kubeintellect-clean-deploy` alone, the pod runs the GHCR image with code baked in — your local edits are not reflected. You must run `make kind-dev-deploy` to enable hot-reload.

Takes ~5–8 minutes total. After both steps complete, access via ingress:

- **LibreChat UI:** http://kubeintellect.chat.local  (login with `DEV_USER_EMAIL` / `DEV_USER_PASSWORD` from `.env`)
- **API:** http://kubeintellect.api.local

> **Note:** `make port-forward-librechat` (localhost:3080) does not work on this setup — use the ingress URLs above instead.

### Optional: Langfuse LLM observability

Langfuse is not deployed by the base clean-deploy (it adds ~2 min and ~4 PVCs). Enable it separately after the cluster is up:

```bash
make kind-langfuse-deploy   # deploys Langfuse stack → http://langfuse.local
```

**No manual setup required.** The Langfuse API keys, admin user, org, and project are seeded automatically from the generated `values-kind-secrets.yaml`. KubeIntellect is pre-wired with matching keys — it connects without any copy-paste step.

Default login: `admin@kubeintellect.local` / `langfuse-admin`

To also enable Langfuse tracing in the KubeIntellect app, set in `.env`:

```bash
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...   # printed by make kind-generate-secrets if auto-generated
LANGFUSE_SECRET_KEY=sk-lf-...   # same
```

Then `make kind-dev-restart` to apply. See `docs/observability.md` for full details.

---

## Values Files: `values-kind.yaml` vs `values-kind-dev.yaml`

| | `values-kind.yaml` | `values-kind-dev.yaml` |
|---|---|---|
| **Purpose** | Production-like deploy on Kind | Local development with hot-reload |
| **Image (core)** | `ghcr.io/mskazemi/kubeintellect-release:latest` | `kubeintellect:dev` (locally built) |
| **Image pull** | From GHCR — needs `ghcr-creds` PAT | `Never` — image must be pre-loaded via `make kind-build` |
| **Code source** | Baked into the image | Host filesystem mounted into the pod at `/app` |
| **Hot-reload** | No | Yes — uvicorn `--reload` picks up `.py` changes in ~2s |
| **Used by** | `make kind-kubeintellect-deploy` | `make kind-dev-deploy` (layered on top of `values-kind.yaml`) |

`values-kind-dev.yaml` is an **overlay** — it only overrides the `apps:` array. Everything else (PVCs, services, ingresses, secrets, configmaps) always comes from `values-kind.yaml`. Both `-f` flags are passed together when deploying in dev mode.

**Credentials:** `values-kind.yaml` contains only placeholder values for secrets — never real credentials. Real credentials come from `.env` and are injected via `values-kind-secrets.yaml` (gitignored, generated by `make kind-generate-secrets`). See the credential flow in [First-Time Setup](#first-time-setup) above.

---

## Client Interfaces

Three ways to interact with the running system:

### LibreChat UI
`http://kubeintellect.chat.local` (or `http://localhost:3080` via port-forward) — the default chat frontend.

### CLI — kube-q

**[kube-q](https://github.com/MSKazemi/kube_q)** is the standalone terminal client. Install it once from PyPI and point it at the running API:

```bash
pip install kube-q

kq --url http://kubeintellect.api.local          # interactive REPL via Kind ingress
kq --url http://localhost:8000 "list all pods"   # single-query mode via port-forward
```

See [github.com/MSKazemi/kube_q](https://github.com/MSKazemi/kube_q) for full CLI docs, options, and Homebrew install.

**Contributing to this repo only:** If you need to run the CLI from the local source checkout (without a PyPI install), the entry point is:

```bash
uv run kubeintellect --url http://kubeintellect.api.local   # interactive REPL
make cli                                                     # same, via Makefile shortcut
```

### MCP Server
Exposes KubeIntellect as an [MCP](https://modelcontextprotocol.io) server (stdio transport) for Claude Desktop, VS Code, and other MCP-compatible AI clients.

```bash
# Run the MCP server (stdio — launched by the MCP client automatically)
uv run python -m app.mcp.server

# Inspect available tools
uv run mcp dev app/mcp/server.py
```

**Claude Desktop config** (`~/.config/claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "kubeintellect": {
      "command": "uv",
      "args": ["run", "python", "-m", "app.mcp.server"],
      "cwd": "/path/to/KubeIntellect",
      "env": {
        "KUBEINTELLECT_API_URL": "http://localhost:8000",
        "KUBEINTELLECT_API_KEY": ""
      }
    }
  }
}
```

> The MCP server's direct K8s tools (list, describe, logs, scale, …) call the Python functions in `app/agents/tools/tools_lib/` without going through the HTTP API, so they work even if the KubeIntellect API pod is unreachable. `kubeintellect_query` and `kubeintellect_approve` require the API to be running (port-forward or ingress).

---

## Daily Dev Workflow

### Setup (once per cluster)

```bash
make kind-kubeintellect-clean-deploy   # Step 1: create cluster, deploy all services (production image)
make kind-dev-deploy                   # Step 2: build kubeintellect:dev, switch to dev mode (hot-reload)
```

After step 2, the host source tree is live inside the pod at `/app` via hostPath mount — no image rebuild is needed for Python changes. The dev user is created automatically by step 1.

---

### Dev loop by scenario

| Change type | Action |
|-------------|--------|
| **Python file** | **Nothing** — uvicorn `--reload` picks it up in ~2s |
| **Secret / API key** | Edit `.env`, then `make kind-generate-secrets FORCE=--force` + `make kind-dev-restart` |
| **Non-secret config** | Edit `charts/kubeintellect/values-kind.yaml`, then `make kind-dev-restart` |
| **New Python dependency** | `uv add <package>` → `make kind-dev-deploy` (only time image rebuild is needed) |
| **Full cluster reset** | `make kind-kubeintellect-clean-deploy` → `make kind-dev-deploy` |

Watch logs to confirm a reload happened:

```bash
kubectl logs -f -n kubeintellect deploy/kubeintellect-core
```

#### Python code change (most common)
**Nothing to do.** File changes on disk are immediately visible inside the pod via the hostPath mount. uvicorn `--reload` detects the change and restarts in ~2 seconds.

#### Config or environment variable change
Edit the relevant value in `charts/kubeintellect/values-kind.yaml`, then:

```bash
make kind-dev-restart   # re-applies dev values + restarts pod, no image rebuild
```

#### New Python dependency added
```bash
uv add <package>        # updates pyproject.toml + uv.lock
make kind-dev-deploy    # rebuilds image (installs new dep), loads into Kind, redeploys
```

#### Full cluster reset
```bash
make kind-kubeintellect-clean-deploy   # destroys cluster, recreates everything fresh
make kind-dev-deploy                   # switch back to dev mode
```

---

## How Hot-Reload Works

```
Host filesystem (<project root>)
    ↓ Kind node hostPath mount (/mnt/code/KubeIntellect)   ← set up by create-kind-cluster.sh
    ↓ Pod hostPath volume (/app)                            ← set up by values-kind-dev.yaml
    ↓ uvicorn --reload watches /app/**/*.py
    → server reloads in ~2s on any .py change
```

This chain is set up by:
- `scripts/kind/create-kind-cluster.sh` — generates `kind-2node.yaml` from the template with the actual `$(pwd)` project root, then passes it to `kind create cluster`. The hostPath mounts are **portable** — they resolve to wherever the repo is cloned.
- `infrastructure/kind/kind-2node.yaml.template` — template with `${PROJECT_ROOT}` placeholders for the hostPath mounts
- `charts/kubeintellect/values-kind-dev.yaml` — mounts `/mnt/code/KubeIntellect` into the pod at `/app`
- `Dockerfile` CMD — `uvicorn ... --reload`

**Important:** Hot-reload only works with `make kind-dev-deploy`. Production deploys (`make kind-kubeintellect-deploy`) bake code into the image — edits on disk are not reflected.

---

## Which Restart Target Should I Use?

| Situation | Command |
|-----------|---------|
| Python file changed | Nothing — uvicorn `--reload` handles it |
| Config / env var changed in `values-kind.yaml` | `make kind-dev-restart` |
| Pod is stuck / wedged | `make kind-core-restart` (or `make restart`) |
| New Python package added (`uv add`) | `make kind-dev-deploy` |
| Just came back from a host reboot | `make kind-start` (start the cluster), then `make kind-dev-restart` |
| Cluster is broken / needs full reset | `make kind-kubeintellect-clean-deploy` then `make kind-dev-deploy` |

> **`make restart` vs `make kind-dev-restart`:** `restart` (`kind-core-restart`) only restarts the pod — it does **not** re-apply Helm values. Use it only to kick a wedged pod. `kind-dev-restart` re-applies the full dev Helm values first, then restarts — use it when you changed config.

---

## Makefile Reference (Kind)

| Target | When to use |
|--------|-------------|
| `make kind-kubeintellect-clean-deploy` | **Step 1** — Full clean slate: recreate cluster, deploy all services (production GHCR image) |
| `make kind-dev-deploy` | **Step 2** — Build `kubeintellect:dev` image, switch to dev mode (hot-reload). Re-run only when deps change. |
| `make kind-dev-restart` | Re-apply dev Helm values + restart pod. Use after config/env changes. No image rebuild. |
| `make kind-core-restart` / `make restart` | Kick a wedged pod. Does NOT re-apply Helm values. |
| `make kind-kubeintellect-deploy` | Deploy to an existing cluster (production image, no cluster recreate) |
| `make kind-cluster-create` | Create cluster only (no app deploy) |
| `make kind-cluster-cleanup` | Destroy the cluster |
| `make kind-start` | Start cluster Docker containers after a host reboot |
| `make kind-stop` | Stop cluster Docker containers |
| `make kind-build` | Build `kubeintellect:dev` and load into Kind (called by `kind-dev-deploy`) |
| `make kind-dev-create-user` | Create dev user in LibreChat — idempotent, called automatically by deploys |
| `make kubeintellect-restart` | Restart core + librechat + postgres pods |
| `make kind-langfuse-deploy` | Add Langfuse LLM tracing → http://langfuse.local |
| `make port-forward-langfuse` | Forward Langfuse → localhost:3000 (fallback if ingress is down) |
| `make port-forward-librechat` | Forward LibreChat UI → localhost:3080 |
| `make port-forward-api` | Forward API → localhost:8000 |

---

## Production Deployment (Azure AKS)

Production runs on Azure AKS. The image is hosted on GHCR (`ghcr.io/mskazemi/kubeintellect-release:latest`). There is no hot-reload — every code change requires a build, push, and rollout restart.

---

### TLS / DNS setup (one-time, per cluster)

All three services are served over HTTPS via Let's Encrypt. cert-manager handles certificate issuance automatically once the DNS records exist.

#### Step 1 — Create DNS A records

Point each hostname at the Azure Load Balancer public IP. Get the IP after the cluster and ingress-nginx are up:

```bash
kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

Then create these `A` records in your DNS provider (e.g. Azure DNS, Cloudflare):

| Hostname | Type | Value |
|----------|------|-------|
| `chat.kubeintellect.com` | A | `<LB IP>` |
| `api.kubeintellect.com` | A | `<LB IP>` |
| `prometheus.kubeintellect.com` | A | `<LB IP>` |
| `grafana.kubeintellect.com` | A | `<LB IP>` |

DNS propagation typically takes a few minutes. Verify with `dig chat.kubeintellect.com` before proceeding.

#### Step 2 — Install cert-manager (before Helm deploy)

cert-manager must be present in the cluster before the Helm chart is applied, because the chart creates a `ClusterIssuer` resource that requires cert-manager CRDs.

```bash
make azure-install-cert-manager
```

This installs cert-manager `v1.14.5` into the `cert-manager` namespace and waits for the webhook to be ready. It is idempotent — safe to run on a cluster that already has cert-manager.

#### Step 3 — Deploy the Helm chart

```bash
make azure-kubeintellect-deploy
```

The chart deploys the `letsencrypt-prod` `ClusterIssuer` and all three ingresses with TLS annotations. cert-manager will request certificates from Let's Encrypt automatically via HTTP-01 challenge.

#### Step 4 — Verify certificates

```bash
# Check all three certificates are Ready
kubectl get certificates -n kubeintellect

# Inspect a specific cert if it's slow or failing
kubectl describe certificate chat-kubeintellect-tls -n kubeintellect

# Check the ACME challenge (if cert is still Pending)
kubectl get challenges -n kubeintellect
```

Certificates are typically issued within 1–2 minutes after DNS propagates. Once `READY=True` you can open `https://chat.kubeintellect.com` in a browser — no `/etc/hosts` editing needed.

> **Note:** Let's Encrypt has a rate limit of 5 duplicate certificates per week per domain. Do not repeatedly reinstall the cluster in a short window — the existing cert Secret (`chat-kubeintellect-tls`, etc.) is reused across Helm upgrades as long as the namespace is not deleted.

---

### Deploy code changes

```bash
# 1. Build and push the image
docker build -t ghcr.io/mskazemi/kubeintellect-release:latest .
docker push ghcr.io/mskazemi/kubeintellect-release:latest

# 2. Restart the deployment to pull the new image
kubectl rollout restart deployment kubeintellect-core -n kubeintellect

# Wait for rollout to complete
kubectl rollout status deployment kubeintellect-core -n kubeintellect
```

Or, if you need to redeploy the full chart (e.g. config/values changed):

```bash
make azure-kubeintellect-deploy
```

### Deploy config/env changes only (no image rebuild)

Edit `charts/kubeintellect/values-azure.yaml`, then:

```bash
make azure-kubeintellect-deploy
kubectl rollout restart deployment kubeintellect-core -n kubeintellect
```

### Full production teardown and redeploy

```bash
# cert-manager only needs reinstalling if you're starting from a brand-new cluster
make azure-install-cert-manager
make azure-kubeintellect-fresh-deploy   # uninstalls + reinstalls Helm release
```

> **Warning:** `azure-kubeintellect-fresh-deploy` deletes the `kubeintellect` namespace, which removes the TLS `Secret`s. cert-manager will re-issue them automatically, but this counts against the Let's Encrypt rate limit.

### Check what's running

```bash
kubectl get pods -n kubeintellect
kubectl get deployment kubeintellect-core -n kubeintellect \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
```

### Stream logs

```bash
kubectl logs -f -n kubeintellect deploy/kubeintellect-core
```

### Rotate secrets

Secrets are sourced from `.env` and populated into Azure Key Vault via:

```bash
bash scripts/ops/setup-secrets-infra.sh
```

After rotating, redeploy so the pods pick up the new values:

```bash
make azure-kubeintellect-deploy
kubectl rollout restart deployment kubeintellect-core -n kubeintellect
```

---

## Restoring Data

If you have a MongoDB backup from a previous session:

```bash
make mongo-restore                        # restore latest backup in backups/
make mongo-restore FILE=backups/foo.gz    # restore specific file
```

If you have a runtime-tools PVC backup:

```bash
make pvc-restore PVC=kubeintellect-runtime-tools-pvc FILE=backups/runtime-tools-xxx.tar.gz
```

---

## Troubleshooting

**Pod stuck in `ImagePullBackOff`**
Two possible causes:
- *Dev deploy:* you're pulling `kubeintellect:dev` but haven't built it yet → run `make kind-build`.
- *Production deploy:* the GHCR PAT is expired or revoked (you'll see `403 Forbidden` in `kubectl describe pod`). Generate a new PAT with `read:packages` scope, set `GHCR_PAT=<new-pat>` in `.env`, then run `make kind-generate-secrets FORCE=--force && make kind-dev-restart`.

**Pod stuck in `Pending`** (after clean deploy)
Postgres hostPath PV requires `.local-data/postgres` to exist on the host. Run `mkdir -p .local-data/postgres` and recreate the cluster.

**`ModuleNotFoundError: No module named 'app'` in pod logs**
The `/app` hostPath mount is empty. Two possible causes:
- You ran `make kind-kubeintellect-clean-deploy` but never ran `make kind-dev-deploy`. The clean deploy uses the GHCR image with code baked in — the hostPath mount is not active. Run `make kind-dev-deploy`.
- The Kind cluster was created with a stale `kind-2node.yaml` that had a wrong `hostPath`. The path is now resolved dynamically from `$(pwd)` via `kind-2node.yaml.template` — delete and recreate the cluster with `make kind-kubeintellect-clean-deploy`.

**Changes not reflected after editing Python files**
Check if you're running in dev mode (`make kind-dev-deploy` was run). In production mode the code is baked into the image — edits on disk are ignored. Also check `kubectl logs -n kubeintellect deploy/kubeintellect-core` for reload errors.

**`helm upgrade` fails with "conflict with kubectl-client-side-apply"**
Happens when a resource was first created via client-side apply and Helm later tries to manage it with server-side apply. All Kind Makefile targets now use `--server-side --force-conflicts` to prevent this. If you hit it on a pre-existing cluster:
```bash
kubectl annotate secret kubeintellect-core-secret -n kubeintellect \
  meta.helm.sh/release-name=kubeintellect \
  meta.helm.sh/release-namespace=kubeintellect --overwrite
kubectl label secret kubeintellect-core-secret -n kubeintellect \
  app.kubernetes.io/managed-by=Helm --overwrite
```
Then re-run `make kind-dev-deploy`. On any fresh cluster created after this fix, the conflict will not occur.

**`helm upgrade` fails with timeout**
Some pods (especially librechat pulling from GHCR) can be slow on first pull. Retry with a longer timeout:
```bash
helm upgrade kubeintellect charts/kubeintellect -n kubeintellect \
  -f charts/kubeintellect/values-kind.yaml \
  -f charts/kubeintellect/values-kind-secrets.yaml \
  -f charts/kubeintellect/values-kind-dev.yaml \
  --timeout 10m --wait
```
