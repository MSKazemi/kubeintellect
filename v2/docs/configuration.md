---
description: >-
  Complete reference for all KubeIntellect environment variables — LLM provider, authentication, database, kubectl settings, and observability.
---

# Configuration Reference

All KubeIntellect settings are environment variables. They can be set in:

- `~/.kubeintellect/.env` — written by `kubeintellect init`; used for pip installs
- `.env` in the project directory — takes precedence, used for dev overrides
- Helm `values.yaml` (in-cluster deploy) — maps to a Kubernetes Secret and ConfigMap
- Shell environment — highest priority, overrides all files

**Quickest way to change a value:**

```bash
kubeintellect set KEY=VALUE          # updates ~/.kubeintellect/.env; restarts service if active
kubeintellect set A=1 B=2 C=3        # multiple values at once
```

---

## pip install — complete `.env` template {#pip-install-template}

Skip the interactive wizard and configure manually. Copy the block below, save it to
`~/.kubeintellect/.env`, fill in the values marked `← change this`, and run
`kubeintellect serve`.

```bash
# ~/.kubeintellect/.env
# KubeIntellect local configuration
# Update a value at any time: kubeintellect set KEY=VALUE


# ═══════════════════════════════════════════════════════
# REQUIRED — fill in exactly one LLM provider
# ═══════════════════════════════════════════════════════

LLM_PROVIDER=openai                     # openai  or  azure

# ── Option A: OpenAI ─────────────────────────────────────────────────────────
OPENAI_API_KEY=sk-...                   # ← your key (platform.openai.com/api-keys)
OPENAI_COORDINATOR_MODEL=gpt-4o
OPENAI_SUBAGENT_MODEL=gpt-4o-mini

# ── Option B: Azure OpenAI ───────────────────────────────────────────────────
# Comment out the OpenAI lines above and uncomment these:
#
# LLM_PROVIDER=azure
# AZURE_OPENAI_API_KEY=                 # ← your key
# AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/  # ← your endpoint
# AZURE_COORDINATOR_DEPLOYMENT=gpt-4o
# AZURE_SUBAGENT_DEPLOYMENT=gpt-4o-mini
# AZURE_OPENAI_API_VERSION=2024-02-01


# ═══════════════════════════════════════════════════════
# AUTHENTICATION  (optional — recommended for any non-localhost use)
# ═══════════════════════════════════════════════════════
#
# Leave all three empty for open-access mode (safe on localhost only).
# Generate a key:  openssl rand -hex 20
# kube-q uses:     KUBE_Q_API_KEY=<key>  in  ~/.kube-q/.env

KUBEINTELLECT_SUPERADMIN_KEYS=          # all ops + infra-ns writes (HITL still applies)
KUBEINTELLECT_ADMIN_KEYS=               # e.g. ki-admin-a1b2c3d4e5
KUBEINTELLECT_OPERATOR_KEYS=
KUBEINTELLECT_READONLY_KEYS=

# Optional — HMAC-signed demo keys. Set both to enable.
# AUTH_BACKEND=hmac
# DEMO_KEY_HMAC_SECRET=                 # openssl rand -hex 32


# ═══════════════════════════════════════════════════════
# KUBERNETES
# ═══════════════════════════════════════════════════════

KUBECONFIG_PATH=~/.kube/config          # change if your kubeconfig is elsewhere


# ═══════════════════════════════════════════════════════
# DATABASE  (no change needed for local use)
# ═══════════════════════════════════════════════════════
#
# SQLite is used automatically — no setup needed.
# To switch to PostgreSQL, uncomment and fill in:
# DATABASE_URL=postgresql://user:password@host:5432/kubeintellect


# ═══════════════════════════════════════════════════════
# OBSERVABILITY  (optional)
# ═══════════════════════════════════════════════════════

PROMETHEUS_URL=                         # e.g. http://prometheus.company.com
LOKI_URL=                               # e.g. http://loki.company.com


# ═══════════════════════════════════════════════════════
# APP SETTINGS  (defaults are fine)
# ═══════════════════════════════════════════════════════

LOG_LEVEL=INFO
LOG_FORMAT=text
```

---

## LLM provider

| Variable | Default | Values | Description |
|---|---|---|---|
| `LLM_PROVIDER` | `azure` | `openai` \| `azure` | Which LLM backend to use |

**OpenAI:**

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | — | Your OpenAI API key |
| `OPENAI_COORDINATOR_MODEL` | `gpt-4o` | Model for the coordinator agent |
| `OPENAI_SUBAGENT_MODEL` | `gpt-4o-mini` | Model for domain subagents |

**Azure OpenAI:**

| Variable | Default | Description |
|---|---|---|
| `AZURE_OPENAI_API_KEY` | — | Azure OpenAI API key |
| `AZURE_OPENAI_ENDPOINT` | — | Resource endpoint — must include protocol: `https://....openai.azure.com/` |
| `AZURE_OPENAI_API_VERSION` | `2024-10-01-preview` | API version. The default enables automatic prefix caching, which materially reduces cost on long coordinator prompts. |
| `AZURE_COORDINATOR_DEPLOYMENT` | `gpt-4o` | Deployment name for coordinator |
| `AZURE_SUBAGENT_DEPLOYMENT` | `gpt-4o-mini` | Deployment name for subagents |

---

## Database

KubeIntellect supports PostgreSQL (production) and SQLite (local/no-Docker).

`kubeintellect serve` auto-detects which to use — manual configuration is only needed to override the defaults.

### PostgreSQL

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | — | Full DSN — overrides all `POSTGRES_*` vars |
| `POSTGRES_HOST` | `localhost` | DB host (`postgres` when using docker-compose) |
| `POSTGRES_PORT` | `5432` | DB port |
| `POSTGRES_DB` | `kubeintellect` | Database name |
| `POSTGRES_USER` | `kubeintellect` | DB user |
| `POSTGRES_PASSWORD` | `password` | **Change this** |
| `POSTGRES_POOL_MIN_CONN` | `1` | Connection pool minimum |
| `POSTGRES_POOL_MAX_CONN` | `10` | Connection pool maximum |

### SQLite (local fallback)

| Variable | Default | Description |
|---|---|---|
| `USE_SQLITE` | `false` | Force SQLite mode (auto-set by `kubeintellect serve` if no postgres) |
| `SQLITE_PATH` | `~/.kubeintellect/kubeintellect.db` | Path to SQLite database file |

SQLite is included in `pip install kubeintellect` — no extra needed. State persists to disk across restarts.
Not used in Helm deployments (`DATABASE_URL` is always set there).

---

## Kubernetes access

KubeIntellect uses `kubectl` to interact with your cluster. `kubectl` must be installed and on PATH.

| Variable | Default | Description |
|---|---|---|
| `KUBECONFIG_PATH` | `~/.kube/config` | Path to kubeconfig file |
| `KUBECTL_TIMEOUT_SECONDS` | `30` | Timeout for read operations |
| `KUBECTL_DESTRUCTIVE_TIMEOUT_SECONDS` | `300` | Timeout for write operations |
| `KUBECTL_BLOCKED_NAMESPACES` | see below | Namespaces the agent will never touch |
| `KUBECTL_BLOCKED_RESOURCES` | `secret,secrets,...` | Resource types always blocked |

**Laptop/pip mode**: uses your local `~/.kube/config` — no in-cluster setup needed.
**In-cluster (Helm) mode**: uses the mounted ServiceAccount — `KUBECONFIG_PATH` is ignored.

Default blocked namespaces (safety fence):
```
kubeintellect, monitoring, kube-system, kube-public, kube-node-lease, ingress-nginx, cert-manager
```

---

## Authentication (RBAC)

Auth is optional. If no keys are set in any of the four lists *and* no
`DEMO_KEY_HMAC_SECRET` is configured, all requests are accepted (open access).

| Variable | Default | Description |
|---|---|---|
| `KUBEINTELLECT_SUPERADMIN_KEYS` | `""` | Comma-separated bearer tokens — admin capabilities + writes to infrastructure namespaces (still HITL-gated) |
| `KUBEINTELLECT_ADMIN_KEYS` | `""` | High + medium risk ops, always HITL-gated; infra-namespace writes blocked |
| `KUBEINTELLECT_OPERATOR_KEYS` | `""` | Medium-risk ops only (`patch`, `apply`, `scale`, `exec`, `create`, `run`); high-risk verbs (`delete`, `drain`, `replace`, `taint`) blocked |
| `KUBEINTELLECT_READONLY_KEYS` | `""` | Read-only; all writes rejected before reaching the agent |

Generate a key: `openssl rand -hex 20`

Clients set the key via `Authorization: Bearer <key>` header, or `KUBE_Q_API_KEY` env var in kube-q.

### HMAC-signed demo keys (`AUTH_BACKEND=hmac`)

For public demo / browser-terminal deployments where you want anonymous
read-only access without restarting the server to add new keys, set
`AUTH_BACKEND=hmac` and `DEMO_KEY_HMAC_SECRET=<random>`. Keys of the form
`ki-ro-<base64url(email:exp_unix)>.<hmac_sha256_hex[:32]>` are then validated by
signature + expiry — no list lookup. Static `*_KEYS` continue to work; HMAC is
only consulted for tokens that look like `ki-ro-*`.

| Variable | Default | Description |
|---|---|---|
| `AUTH_BACKEND` | `static` | `static` (only the lists above) or `hmac` (also accept signed demo keys) |
| `DEMO_KEY_HMAC_SECRET` | — | Secret used to sign and verify demo keys. Generate with `openssl rand -hex 32`. Rotate to invalidate all outstanding demo keys instantly. |

---

## kube-q CLI (set on the client machine)

| Variable | Default | Description |
|---|---|---|
| `KUBE_Q_URL` | `http://localhost:8000` | Backend URL |
| `KUBE_Q_API_KEY` | — | Bearer token (must match one of the `*_KEYS` above) |

```bash
mkdir -p ~/.kube-q
echo "KUBE_Q_URL=http://localhost:8000" >> ~/.kube-q/.env
echo "KUBE_Q_API_KEY=ki-admin-xxx"     >> ~/.kube-q/.env
```

---

## Observability (optional)

| Variable | Default | Description |
|---|---|---|
| `PROMETHEUS_URL` | `""` | Prometheus endpoint; enables PromQL queries |
| `LOKI_URL` | `""` | Loki endpoint; enables LogQL queries |
| `GRAFANA_URL` | `""` | Grafana endpoint; shown in `kubeintellect status` |

`kubeintellect init` sets these automatically when you choose to install the observability stack (NodePort URLs on a Kind cluster). For in-cluster deployments they are set via Helm `values.yaml`.

If empty, kubectl-based queries still work — only metrics and log queries are unavailable.

---

## LLM tracing with Langfuse (optional)

| Variable | Default | Description |
|---|---|---|
| `LANGFUSE_ENABLED` | `false` | Enable Langfuse tracing |
| `LANGFUSE_PUBLIC_KEY` | — | Langfuse project public key |
| `LANGFUSE_SECRET_KEY` | — | Langfuse project secret key |
| `LANGFUSE_HOST` | `http://langfuse-web...` | Langfuse server URL |

Install the tracing extra:
```bash
pip install 'kubeintellect[tracing]'
```

---

## App settings

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FORMAT` | `text` | `text` or `json` |
| `DEBUG` | `false` | Enable FastAPI debug mode |
| `ALLOWED_ORIGINS` | `http://localhost:3080` | CORS allowed origins (comma-separated) |

---

## Agent behavior flags

Five additive behaviors shape how the KubeIntellect coordinator
investigates. Each is feature-flagged so you can disable without redeploying.

| Variable | Default | Values | Description |
|---|---|---|---|
| `KUBECTL_ERROR_HINTS_ENABLED` | `true` | `true` \| `false` | Append a one-line diagnostic hint to non-zero kubectl errors (e.g. NotFound → "verify namespace and name"). Original error preserved verbatim. |
| `SNAPSHOT_SUFFICIENCY_MODE` | `lenient` | `off` \| `lenient` \| `strict` | Bias the coordinator toward answering list-shaped questions from the pre-fetched snapshot when the cluster is healthy. `off` disables the bias entirely. `strict` = aggressive bias (opt-in). Always falls back to fresh data for logs, metrics, history, named resources, post-mutation, or freshness keywords. |
| `SNAPSHOT_FRESHNESS_SECONDS` | `30` | integer | Snapshot age beyond which the coordinator must re-fetch regardless of mode. |
| `INVESTIGATION_PLAN_ENABLED` | `true` | `true` \| `false` | Coordinator emits an `INVESTIGATION_PLAN:` block for queries needing 3+ tool calls; surfaced via SSE `PlanEvent`. |
| `PLAYBOOKS_ENABLED` | `true` | `true` \| `false` | When a snapshot matches a known failure pattern (CrashLoopBackOff, OOMKilled, ImagePullBackOff, …), inject the matching playbook(s) from `app/agent/playbooks/*.yaml` into the coordinator's system prompt. |

---

## Reflexion flags {#reflexion-flags}

Controls the self-improvement subsystem that records outcomes and promotes
recurring, verified fix patterns into the prompt. Full design rationale in
[Reflexion Subsystem](reflexion.md).

| Variable | Default | Description |
|---|---|---|
| `REFLEXION_ENABLED` | `true` | Master switch — disables all reflexion writes and reads |
| `REFLEXION_MIN_CONFIDENCE` | `0.7` | Threshold below which outcomes are not loaded into prompts |
| `REFLEXION_VERIFY_RESOLUTION` | `true` | After a mutation, re-snapshot the cluster to verify the fix actually resolved the issue (R4). Adds ~150ms per mutation turn |
| `REFLEXION_REDACT_SECRETS` | `true` | Strip credentials/tokens/internal hostnames from stored manifests before write (R8) |
| `REFLEXION_PATTERN_COOLDOWN_HOURS` | `1` | Minimum gap between `occurrence_count` increments for the same `(pattern, cluster)` (R6) — prevents test-rig bursts from poisoning patterns |
| `REFLEXION_PATTERN_DECAY_DAYS` | `30` | Read-side filter — patterns with `last_seen_at` older than this are not injected |

**Retention.** Run `make db-purge` (or schedule it as a CronJob) to invoke the
SQL function `reflexion_purge(retain_outcomes_days, retain_patterns_days)`.
Defaults: 90 days for `rca_outcomes`, 30 days for unverified `failure_patterns`.
Verified patterns (`confidence >= 0.9`) are never purged by retention — only
by demotion.
