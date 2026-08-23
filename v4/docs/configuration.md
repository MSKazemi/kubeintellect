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

This reference covers the application's per-version `v4/.env` settings. A separate
repo-**root** `.env` holds shared infrastructure config used by the root Makefile
(e.g. `langfuse-install`); `make langfuse-provision` fans the `LANGFUSE_*` keys
from there into `v4/.env`.

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

LLM_PROVIDER=openai                     # openai | azure | qwen | anthropic

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
# AZURE_OPENAI_API_VERSION=2024-10-01-preview

# ── Option C: Alibaba Qwen Cloud / DashScope (OpenAI-compatible) ──────────────
# 'qwen' is a first-class provider: it auto-targets the DashScope endpoint and
# qwen-max / qwen-plus, so this is all you need.
# LLM_PROVIDER=qwen
# DASHSCOPE_API_KEY=sk-...              # your DashScope key (QWEN_API_KEY / OPENAI_API_KEY also accepted)
# Optional overrides (defaults shown):
# OPENAI_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1   # CN: dashscope.aliyuncs.com
# OPENAI_COORDINATOR_MODEL=qwen-max     # synthesis / large tier
# OPENAI_SUBAGENT_MODEL=qwen-plus       # parallel RCA subagents / small tier

# ── Option D: Anthropic / Claude (V4 Cortex) ─────────────────────────────────
# LLM_PROVIDER=anthropic
# ANTHROPIC_API_KEY=sk-ant-...
# ANTHROPIC_LARGE_MODEL=claude-sonnet-4-6
# ANTHROPIC_SMALL_MODEL=claude-haiku-4-5-20251001


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
| `LLM_PROVIDER` | `azure` | `openai` \| `azure` \| `qwen` \| `anthropic` | Which LLM backend to use. `qwen` is OpenAI-compatible via Alibaba DashScope (set `OPENAI_BASE_URL`); `anthropic` is used only by the V4 cortex layer. |

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
| `CLUSTER_ID` | *(derived)* | Explicit cluster identity — see below |
| `KUBECTL_TIMEOUT_SECONDS` | `30` | Timeout for read operations |
| `KUBECTL_DESTRUCTIVE_TIMEOUT_SECONDS` | `300` | Timeout for write operations |
| `KUBECTL_BLOCKED_NAMESPACES` | see below | Namespaces the agent will never touch |
| `KUBECTL_BLOCKED_RESOURCES` | `secret,secrets,...` | Resource types always blocked |

> **A namespace entry that cannot match protects nothing, and used to do so in silence.**
> Case is folded — `Kube-System` and `kube-system` are the same entry — because Kubernetes
> namespace names are always lowercase, so folding can only add protection. What folding cannot
> repair is reported: a glob (`kube-*`, which `AUTONOMY_A3_ALLOWLIST` supports and this setting
> does **not**), a slash, or anything else that is not an RFC 1123 label is logged at startup as
> `guard_config_unenforceable` and listed under `unenforceable_guard_config` in
> [`GET /v1/v5/status`](api-reference.md#get-v1v5status) and `kq v5-status`. The server still
> starts — a typo must not take the agent offline, only become impossible to miss.
>
> The same reporting covers `AUTONOMY_NAMESPACE_LEVELS`, where a dropped entry fails **open**
> (the namespace keeps the permissive default the override existed to tighten), and
> `AUTONOMY_A3_ALLOWLIST`, where a dropped entry fails closed.
>
> **`AUTONOMY_NAMESPACE_LEVELS` does not take globs, and its neighbour does.** The lookup is an
> exact match on the lowercased namespace name, so `prod-*=A0` is stored, matched by nothing, and
> leaves every `prod-` namespace on the permissive default — while `AUTONOMY_A3_ALLOWLIST` in the
> next row *does* honour `prod-*`, which is exactly what makes the mistake easy to make. Write one
> entry per namespace: `prod-web=A0,prod-api=A0`. Until 2026-08-20 that entry was reported as
> fine; it is now reported like any other unenforceable guard setting, with a message that says
> where globs do work.

> **Resource entries are matched by spelling, and only the spellings that can be derived.**
> Case is folded (`ConfigMap` = `configmap`) and singular/plural are treated as one resource
> (`configmap` also blocks `configmaps`, `ingress` also blocks `ingresses`) — measured
> 2026-08-20, neither held: `KUBECTL_BLOCKED_RESOURCES="ConfigMap"`, the spelling Kubernetes
> itself uses for `kind:`, blocked **nothing**. What is *not* derived is kubectl's **short
> names**: they come from API discovery and cannot be computed from a string, so blocking
> `configmaps` does **not** block `kubectl get cm`. List the short name explicitly if you want
> it covered. An entry that is not a resource type kubectl would accept at all (a glob, a
> slash) is reported like any other unenforceable guard setting.

> Both variables **replace** their list rather than extending it — adding one entry drops the
> rest. For namespaces that is intentional (letting the agent investigate `monitoring` is a
> legitimate choice), so repeat every entry you still want blocked. For resources, the four
> credential types `secret`, `secrets`, `serviceaccount`, `serviceaccounts` are re-added
> unconditionally and **cannot be configured away**.

**Laptop/pip mode**: uses your local `~/.kube/config` — no in-cluster setup needed.
**In-cluster (Helm) mode**: uses the mounted ServiceAccount — `KUBECONFIG_PATH` is ignored.

### Cluster identity {#cluster-identity}

Memory, learned failure patterns, episodes and findings are all scoped by a cluster id, so a fix
learned on a Kind dev cluster is not offered for the same symptom on production EKS. The id is
derived automatically, in this order:

1. `CLUSTER_ID`, if you set it — always wins.
2. The current kubeconfig context name, plus a short hash of the API-server URL.
3. A hash of the API-server URL alone.
4. The `kube-system` namespace UID, the conventional cluster identifier.
5. Otherwise the literal `unknown`.

**Set `CLUSTER_ID` whenever more than one cluster writes to the same database.** Steps 2 and 3
read a kubeconfig *file*, which an in-cluster deployment does not have — it authenticates with the
pod's ServiceAccount — and step 4 needs cluster-scoped read on namespaces, which the chart's
default namespaced Role (`rbac.clusterAdmin: false`) does not grant. In that configuration
identity falls through to `unknown`, and `unknown` is **not** an inert placeholder: recall matches
`cluster_id IN (<current>, 'unknown')`, so every cluster sharing the database reads every other
cluster's `unknown` rows. One cluster per database is unaffected; two are not.

With Helm, set it once in your values:

```yaml
config:
  clusterId: prod-eks-eu-west-1
```

The server logs a warning naming both the fallback and its consequence when identity cannot be
resolved. `kubectl logs deploy/kubeintellect | grep cluster_id` shows it.

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
| `DEMO_KEY_DEFAULT_TTL_HOURS` | `168` | Lifetime of a minted demo key (7 days) when the mint request does not specify one. |
| `DEMO_KEY_MAX_TTL_HOURS` | `720` | Upper bound (30 days) on the lifetime a demo key may be minted with. |

---

## kube-q CLI (set on the client machine)

| Variable | Default | Description |
|---|---|---|
| `KUBE_Q_URL` | `https://api.kubeintellect.com` | Backend URL. With no config, `kq` targets the hosted API; set this (or run `kubeintellect init`) to point at your own server. |
| `KUBE_Q_API_KEY` | — | Bearer token (must match one of the `*_KEYS` above) |

For a local server, point `kq` at `http://localhost:8000`:

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
| `GRAFANA_URL` | `""` | Grafana endpoint. Used only by the CLI to print dashboard links in `kubeintellect status` — the server never queries Grafana, so it is not a server setting. |

`kubeintellect init` sets these automatically when you choose to install the observability stack (NodePort URLs on a Kind cluster). For in-cluster deployments `PROMETHEUS_URL`/`LOKI_URL` are set via Helm `values.yaml`.

If empty, kubectl-based queries still work — only metrics and log queries are unavailable.

---

## LLM tracing with Langfuse (optional)

| Variable | Default | Description |
|---|---|---|
| `LANGFUSE_ENABLED` | `false` | Enable Langfuse tracing (set to `true` by `make langfuse-provision`) |
| `LANGFUSE_PUBLIC_KEY` | — | Langfuse project public key — auto-filled by `make langfuse-provision` |
| `LANGFUSE_SECRET_KEY` | — | Langfuse project secret key — auto-filled by `make langfuse-provision` |
| `LANGFUSE_HOST` | — | Langfuse server URL. docker-compose: `http://localhost:3001`; in-cluster (Helm): `http://langfuse-web.monitoring.svc.cluster.local:3000` |
| `KI_VERSION` | `v4` | Per-version trace tag. Stamps every Langfuse trace with `version:<KI_VERSION>`; env-overridable. All KubeIntellect versions share one Langfuse project, so this tag is what lets cost/tokens be filtered per version. |

Provision the keys instead of copying them by hand. From the repo root (or
`cd v4 && make langfuse-provision`):

```bash
make langfuse-provision
```

This auto-fills `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` into the app's
`v4/.env`, and also writes `LANGFUSE_ENABLED=true`, `LANGFUSE_HOST`, and the
headless-init seed vars (`LANGFUSE_INIT_ORG_ID`, `LANGFUSE_INIT_PROJECT_ID`,
`LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_NAME` /
`LANGFUSE_INIT_USER_PASSWORD`) so a fresh Langfuse auto-creates the project on
first start. For Langfuse Cloud you can still paste your own keys.

All KubeIntellect versions share one Langfuse project; traces are distinguished
by the `version:vN` tag set from `KI_VERSION`.

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

### Agent loop budgets

Two runaway backstops on the agent's own loops. They are a **safety** control, not a
performance knob: an agent that fails to terminate keeps *acting*, and the coordinator is the
loop that holds the write-capable toolset.

LangGraph 1.x defaults `recursion_limit` to **10007** (~3,300 ReAct steps), which is not a
bound in any practical sense. KubeIntellect sets both limits explicitly instead. The defaults
are deliberately generous — set well above any observed real usage — so they only ever fire on
a genuine runaway.

**Exhausting either budget halts and escalates to you, with everything found so far.** It never
truncates silently and never fails the request with a bare error.

| Variable | Default | Values | Description |
|---|---|---|---|
| `AGENT_GRAPH_RECURSION_LIMIT` | `120` | integer > 0 | Whole-turn budget for the outer graph. A turn costs 3 steps plus 2 per coordinator↔investigation cycle, so `120` allows ~58 cycles. Exhaustion returns the partial turn plus an explanation. |
| `AGENT_COORDINATOR_RECURSION_LIMIT` | `150` | integer > 0 | Tool-call budget for the coordinator's inner ReAct loop. ~3 recursion units per step (LLM call + tool call + tool response), so `150` allows ~50 tool calls in one coordinator turn. Exhaustion ends the turn with an explanation. |

The read-only RCA subagents carry their own fixed limit of `50` (~16 tool calls), set in
`app/agent/nodes/subagent.py`.

Lower either value to see the escalation path in a test environment. Raise
`AGENT_COORDINATOR_RECURSION_LIMIT` if you have investigations that legitimately need more
than ~50 tool calls — but prefer narrowing the question first, since a loop that long usually
means the agent is retrying a failing tool.

> **v5 experimental flags.** The additive, default-off v5 slices are gated by ~60
> `KI_V5_*` / `CORTEX_V5_ENABLED` variables, documented separately in
> [v5-experimental-flags.md](v5-experimental-flags.md). With all of them unset the
> server is byte-identical to V4.

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

---

## V4 {#v4}

Settings for the V4 surface: the flight recorder, the sensorium (always-on
detectors), the memory hierarchy, the V4 reasoning graph, watchtower autonomy,
and the Anthropic model provider.

### Flight recorder, sensorium, memory, cortex

| Variable | Default | Description |
|---|---|---|
| `FLIGHT_RECORDER_ENABLED` | `true` | Append-only, hash-chained decision log of every non-token event, keyed by episode (= session). A recorder outage degrades auditability, never availability. Requires PostgreSQL. |
| `SENSORIUM_ENABLED` | `true` | Always-on `kubectl --watch` perception feeding the detector engine (known-failure detection with zero LLM tokens). Degrades gracefully when kubectl/RBAC is unavailable. |
| `MEMORY_HIERARCHY_ENABLED` | `true` | Episodic (L1) + temporal knowledge-graph (L2) memory with a consolidation worker. PostgreSQL-native. |
| `MEMORY_HYBRID_RETRIEVAL` | `false` | **Memory V5 (experimental, ADR-014).** Episode recall fuses the `pg_trgm` channel with a full-text `ts_rank` channel via Reciprocal Rank Fusion. Additive; falls back to the trigram baseline on any error. Index-accelerated by `idx_episodes_fts` (functional GIN, no table rewrite). |
| `MEMORY_BITEMPORAL_ENABLED` | `false` | **Memory V5 (experimental, ADR-013).** Adds a transaction-time axis to the temporal KG (`ingested_at`/`retracted_at`): event-time `valid_from`, point-in-time `as_of()` queries, retract-not-delete supersede, and an ingest-lag freshness signal. PostgreSQL 16-compatible (plain columns + indexes). |
| `MEMORY_KG_PPR` | `false` | **Memory V5 (experimental, ADR-014).** Multi-hop blast-radius: pulls a bounded ≤3-hop subgraph around seed entities via a recursive CTE, then ranks related entities with in-process Personalized PageRank (dependency-free). Off ⇒ `ppr_blast_radius()` returns empty. |
| `MEMORY_WRITE_RECONCILE` | `false` | **Memory V5 (experimental, ADR-015).** Mem0-style write reconciliation for the extracted-fact path: ADD/UPDATE/RETRACT/NOOP against existing memory behind a salience gate (dedup + supersede). RETRACT sets `retracted_at` (never hard-deletes); defaults to ADD when confidence is low. Off ⇒ plain `open_edge`. |
| `MEMORY_PROMOTION` | `false` | **Memory V5 (experimental, ADR-016).** The learning loop: the consolidation worker promotes verified, recurring episodes into `semantic_rules` (IF→THEN); rules that recur enough go `active` (prompt-injected) and are eligible to seed a detector *candidate* (human-review-gated). Off ⇒ no promotion. |
| `MEMORY_IMPORTANCE` | `false` | **Memory V5 (experimental, ADR-017).** Importance/surprise-weighted retention: each episode write is scored for `importance` (incident severity) and `surprise` (KG-novelty proxy) on new `episodes.importance`/`surprise` columns; recall ranks **recency × importance × relevance** (importance affects *ranking only*, never retention/audit), and a surprise gate drops redundant *low-value* auto-writes (unverified + report-only near-duplicates). Off ⇒ flat relevance+recency recall, all writes kept. |
| `MEMORY_PROSPECTIVE` | `false` | **Memory V5 (experimental, ADR-017).** First-class prospective memory: after an autonomous fix the watchtower records a "re-check condition C at/after T" (did the fix hold?) in a new `prospective_memory` table; the consolidation scheduler claims due re-checks (atomic `FOR UPDATE SKIP LOCKED`), fires each through the autonomy ladder (A0 namespaces never fire), and records the outcome. Off ⇒ no re-checks. |
| `MEMORY_SECURITY_HARDENING` | `false` | **Memory V5 (experimental, ADR-018).** Security-hardened write path against **MINJA query-only memory injection**: user-derived writes pass a write-admission guard of diverse, non-LLM validators — provenance/`trust` scoring, a persistent-instruction injection-signature check, a per-requester rate limiter, and a contradiction check vs high-confidence sensor facts; poisoned/low-trust writes are quarantined, and a `[0,1]` `trust` is stamped on each episode. Sensor-derived writes skip the guard; the guard fails *open*. Right-to-be-forgotten (`forget_subject`) is always available; RLS tenancy is scaffolded in `schema.sql` (enable with the `SET LOCAL ki.cluster_id` discipline). Off ⇒ all writes admitted. |
| `MEMORY_WRITE_RATE_PER_MIN` | `30` | Max user-derived memory writes per requester per minute when `MEMORY_SECURITY_HARDENING` is on. |
| `MEMORY_TRUST_FLOOR` | `0.35` | User-derived writes whose provenance trust is below this are quarantined (not persisted as trusted memory). |
| `MEMORY_SUMMARY_TREE` | `false` | **Memory V5 (experimental, spec R7).** RAPTOR/GraphRAG-style theme summaries: the consolidation worker builds one deterministic summary per `(cluster, playbook\|namespace)` signature into `memory_summaries`, so theme-level questions are answered without scanning every episode. Regeneration is tied to **KG change-rate** (rebuilt only when new episodes arrived or the cluster's KG edge count moved), never a fixed clock. Off ⇒ no summary tree. |
| `MEMORY_SUMMARY_MIN_CLUSTER` | `3` | Minimum episodes in a theme before it gets a summary (when `MEMORY_SUMMARY_TREE` is on). |
| `PREFERENCE_MEMORY_ENABLED` | `true` | Learned operator-preference layer: explicit (`kq preference set`, confidence 1.0) + behaviour-**inferred** preferences (e.g. `default_namespace` from RCA history), injected into the prompt. `/v1/preferences` API. Fail-open. |
| `PREFERENCE_DECAY_DAYS` | `60` | Inferred preferences not re-seen within this window decay and are purged by the consolidation worker (`preference_purge()`). |
| `PREFERENCE_MIN_CONFIDENCE` | `0.3` | Inferred preferences below this confidence are not injected. |
| `PREFERENCE_INFER_MIN_OCCURRENCE` | `3` | How many times a behaviour must recur before it's inferred as a preference. |
| `CORTEX_V4_ENABLED` | `false` | Enables the V4 reasoning graph (triage → gather loop → synthesize → remember). When `false`, the V2 graph is used. |
| `CORTEX_MAX_GATHER_ROUNDS` | `8` | Bound on gather-loop LLM↔tool iterations per turn. |

### Predictive detection (ADR-010)

| Variable | Default | Description |
|---|---|---|
| `PREDICTIVE_DETECTION_ENABLED` | `false` | Anticipatory detection: trend predicates project a range-PromQL metric toward its threshold (least-squares slope, zero tokens) and fire a `predicted` finding *before* the failure manifests. Predicted findings are capped at autonomy `A1` (never auto-fix). Fail-open — but **not silently**: if Prometheus cannot be queried, `GET /v1/findings` reports `predictive: blind` with the reason and `kq findings` withholds its all-clear line. |
| `PREDICTIVE_TREND_INTERVAL_SECONDS` | `60` | How often the trend-projection loop runs (range queries are expensive — separate from the 1s reactive tick). |

### Incident postmortems (ADR-011)

| Variable | Default | Description |
|---|---|---|
| `POSTMORTEM_ENABLED` | `true` | Read-only grounded postmortem view over the flight recorder (`GET /v1/episodes/{id}/postmortem`, `kq postmortem`). The deterministic seq-cited timeline is always available. |
| `POSTMORTEM_LLM_NARRATIVE` | `false` | Add an LLM narrative (the only token-spending part) constrained to the recorded events; falls back to the deterministic timeline on any failure. |

### Natural-language detector authoring (ADR-012)

| Variable | Default | Description |
|---|---|---|
| `NL_DETECTOR_AUTHORING_ENABLED` | `false` | Compile a plain-English failure into a detect block and stage it as a **shadow** detector (observes only, never reaches the watchtower) until a human promotes it. `POST /v1/detectors`, `kq detector`. |
| `DB_DETECTOR_REFRESH_SECONDS` | `120` | How often the engine reloads promoted (active) + shadow detectors from the database so promotions take effect without a restart. |

### Watchtower & autonomy ladder

| Variable | Default | Description |
|---|---|---|
| `WATCHTOWER_ENABLED` | `true` | Autonomous follow-up on detector findings, bounded by the autonomy ladder below. |
| `WATCHTOWER_ROLE` | `operator` | Role the watchtower acts with (same role model as [API keys](#authentication-rbac)). |
| `AUTONOMY_LEVEL` | `A1` | Default autonomy level: `A0` observe, `A1` investigate + report, `A2` propose, `A3` auto-fix (allowlist only). |
| `AUTONOMY_NAMESPACE_LEVELS` | `""` | Per-namespace overrides, e.g. `prod=A0,dev=A2`. **Exact match, no globs** — unlike the row below; an unmatchable entry is reported, not silently ignored. Protected namespaces are always pinned to `A0` for autonomous action. |
| `AUTONOMY_A3_ALLOWLIST` | `""` | Patterns eligible for `A3` auto-fix, as `<playbook>/<namespace-glob>` entries, e.g. `CrashLoopBackOff/dev-*`. |

### Anthropic provider

Optional model provider for the V4 reasoning graph (`LLM_PROVIDER=anthropic`).
Requires the `langchain-anthropic` package.

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `ANTHROPIC_LARGE_MODEL` | `claude-sonnet-4-6` | Model for the synthesis (final answer) tier |
| `ANTHROPIC_SMALL_MODEL` | `claude-haiku-4-5-20251001` | Model for the triage and gather-loop tiers |
