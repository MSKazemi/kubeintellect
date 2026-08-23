---
description: >-
  Operating KubeIntellect in production — scaling, API-key rotation, database
  backup/restore and retention, the audit log, and upgrades.
---

# Operations

Day-2 guidance for running KubeIntellect as a shared service. For first-time
setup see [Install](quickstart.md) and [Deploy](deploy/cloud.md); for the
security model see [Security](security.md).

---

## Scaling

KubeIntellect is a stateless API in front of LangGraph; conversation state lives
in the database, not in process memory. That makes it horizontally scalable —
**provided every replica shares one database.**

- **Single host:** one `kubeintellect serve` process handles concurrent sessions
  (each request runs as its own async task with an isolated graph thread).
- **Kubernetes:** raise `replicaCount` in
  `deploy/helm/kubeintellect/values.yaml`. The cloud values default to
  `replicaCount: 2`.
- **Shared database is mandatory for >1 replica.** Use an external PostgreSQL
  (`postgres.external.enabled: true` + `DATABASE_URL`) so all replicas read the
  same checkpoints, memory, and audit log. SQLite is single-host only.

LLM latency and provider rate limits — not CPU — are the usual bottleneck. The
coordinator and subagents use separate models (`gpt-4o` / `gpt-4o-mini` by
default); size your provider quota for the parallel subagent fan-out.

---

## Rotating API keys

Keys are plain comma-separated bearer tokens per role
(`KUBEINTELLECT_ADMIN_KEYS`, `KUBEINTELLECT_OPERATOR_KEYS`,
`KUBEINTELLECT_READONLY_KEYS`). Rotate without downtime by **adding the new key
alongside the old, then removing the old** once clients have moved:

```bash
# 1. Add the new key (both valid during the cutover):
kubeintellect set KUBEINTELLECT_ADMIN_KEYS=ki-admin-OLD,ki-admin-NEW

# 2. Move clients to ki-admin-NEW (update ~/.kube-q/.env / KUBE_Q_API_KEY).

# 3. Drop the old key:
kubeintellect set KUBEINTELLECT_ADMIN_KEYS=ki-admin-NEW
```

`kubeintellect set` restarts the systemd service automatically so the change
takes effect immediately. In Kubernetes, update the keys in the Helm
`secrets.*` values and `helm upgrade`.

Generate strong keys with `openssl rand -hex 20`. For time-boxed, read-only
access (e.g. demos) prefer **HMAC demo keys** over static keys — see
[Security](security.md) and [`POST /v1/auth/demo-keys`](api-reference.md#post-v1authdemo-keys).

---

## Database

When running on PostgreSQL, KubeIntellect stores:

| Table | Contents |
|---|---|
| `user_prefs`, `session_notes` | Pinned per-user context and notes. |
| `rca_outcomes` | Verified root-cause outcomes (the reflexion ledger). |
| `failure_patterns` | Promoted, recurring, cluster-scoped patterns. |
| `request_log` | Audit log — one row per API request. |
| `runbooks` | Stored runbook entries. |
| checkpoint tables | LangGraph conversation state. |

### Backup & restore

Standard PostgreSQL tooling — nothing KubeIntellect-specific:

```bash
# Backup
pg_dump "$DATABASE_URL" > kubeintellect-$(date +%F).sql

# Restore — keep both flags; see the warning below
psql -v ON_ERROR_STOP=1 --single-transaction "$DATABASE_URL" -f kubeintellect-2026-01-01.sql
```

!!! warning "A restore without `ON_ERROR_STOP=1` can report success and restore nothing"
    psql's default is to print an error, continue to the next statement, and **exit 0**. A restore
    is exactly where that matters: you are running it during an incident, and a half-restored
    database that reported success is discovered later, by its consequences.
    `--single-transaction` makes the restore atomic — you get all of it or none of it.

In SQLite mode, the entire database is a single file — copy
`~/.kubeintellect/kubeintellect.db` while the server is stopped.

### Retention / purge

The reflexion data grows over time. A built-in SQL function prunes it:
`reflexion_purge(retain_outcomes_days, retain_patterns_days)` removes old
outcomes and decayed low-confidence patterns. Run it on a schedule:

```bash
make db-purge      # purges with defaults: 90-day outcomes, 30-day low-confidence patterns
```

Or call it directly and wire it into a cron / Kubernetes `CronJob`:

```sql
SELECT reflexion_purge(90, 30);
```

Tune the windows with `REFLEXION_PATTERN_DECAY_DAYS` (how long an unseen pattern
keeps being recalled) and the retention arguments above. See
[Reflexion Subsystem](reflexion.md) for the data model.

---

## Audit log

Every API request is recorded fire-and-forget in `request_log` with the request
id, session, user, role, path, status code, and duration. Query it for usage and
review:

```sql
-- Recent activity
SELECT created_at, user_id, user_role, path, status_code, duration_ms
FROM request_log ORDER BY created_at DESC LIMIT 50;

-- Who is using which role
SELECT user_role, count(*) FROM request_log GROUP BY user_role;
```

The audit pool is disabled in SQLite mode. Every request also carries an
`X-Request-ID` (generated if the client doesn't supply one) that ties API logs to
server logs — set `LOG_FORMAT=json` to ship structured logs to Loki/Grafana
(see [Observability access](#observability-access)).

**Decision + memory tamper-evidence.** Agent decisions are hash-chained in the flight
recorder (`kq replay <session>` breaks under tampering). With the experimental
`MEMORY_SECURITY_HARDENING` flag on, memory-mutating events are additionally hash-chained
per cluster in `memory_audit`, so a silent edit/delete/reorder of learned memory is
detectable — see [Security](security.md) and [Memory](memory.md).

---

## Observability access

The observability stack — Prometheus + Grafana + Loki, plus Langfuse for LLM
tracing — is **one shared installation for all KubeIntellect versions**, managed
from the **repo-root `Makefile`** (not per-version):

```bash
make monitoring-install     # Prometheus + Grafana + Loki (+ Promtail)
make monitoring-uninstall   # remove the monitoring stack

make langfuse-provision     # create the shared Langfuse project + token (run once)
make langfuse-install       # install Langfuse (run langfuse-provision first)
make langfuse-clean         # uninstall Langfuse + wipe all trace data (irreversible)

make hosts-entry            # add the dev hostnames below to /etc/hosts
```

`make hosts-entry` registers `api.kubeintellect.local`, `langfuse.local`,
`prometheus.local`, and `loki.local` in `/etc/hosts`.

### Where to look

| Tool | Access | Credentials |
|---|---|---|
| **Langfuse** (LLM traces, cost, tokens) | http://langfuse.local — full web UI | `admin@kubeintellect.local` / `langfuse-admin` |
| **Prometheus** (metrics) | http://prometheus.local — web UI | — |
| **Grafana** (dashboards, log/metric explore) | No ingress — port-forward, then http://localhost:3000 | `admin` / `admin` |
| **Loki** (logs) | **No web UI** — view through Grafana (see below) | — |

Grafana has the Prometheus and Loki datasources already wired. Reach it with:

```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
# then open http://localhost:3000
```

> **Loki has no website.** `loki.local` exists but returns **404 at the root —
> that is expected**: Loki is a log *API*, not a UI. To read logs, open Grafana →
> **Explore** → the **Loki** datasource and run a query such as
> `{namespace="monitoring"}`.

**Log scope.** Loki collects in-cluster **pod** logs. If KubeIntellect runs as a
local host process rather than a pod, its own stdout is not in Loki — Loki's role
there is to let the agent query *cluster* logs.

**Per-version cost and tokens.** All versions push traces to **one shared
Langfuse project**; break cost down per version by filtering on the trace tag
`version:vN` (e.g. `version:v4`).

---

## Upgrades

**pip install:**

```bash
pip install --upgrade kubeintellect
kubeintellect db-init      # apply any schema additions (no-op in SQLite)
kubeintellect service restart   # if running as a service
```

**Kubernetes (Helm):** bump `image.tag` and upgrade. The chart runs a
`job-db-init` to apply the schema.

```bash
helm upgrade --install kubeintellect deploy/helm/kubeintellect \
  -f deploy/helm/kubeintellect/values.yaml \
  -f deploy/helm/kubeintellect/values-cloud.yaml \
  --set image.tag=<new-tag>
```

The schema uses additive migrations; back up the database before a major upgrade
(above). Watch the rollout with `kubectl rollout status deploy/kubeintellect -n
kubeintellect` — or ask KubeIntellect itself.

### Confirming the migration actually applied

The `job-db-init` Job is the schema's only enforcement point, so check it rather than assuming:

```bash
kubectl get job -n kubeintellect -l app.kubernetes.io/name=kubeintellect
kubectl logs -n kubeintellect job/kubeintellect-db-init
```

The Job runs `psql` with `ON_ERROR_STOP=1 --single-transaction`, so it is **all-or-nothing**: it
either applies the whole schema and completes, or applies none of it and is marked `Failed`. A
`Failed` Job means the schema is unchanged and safe to retry once the cause is fixed — most often
that the database role lacks `CREATE` on `public`, which is common on managed instances.

!!! note "Before 2026-08-20 this Job could not fail"
    It ran `psql -f` without those flags. psql's default is to report an error, continue, and exit
    0 — so a migration that applied nothing still produced a `Succeeded` Job and a `deployed`
    release. If you are upgrading from an older chart, confirm the schema is current by re-running
    the upgrade: the Job is idempotent, and now it tells you the truth.

---

## Related

- [Security](security.md) — roles, key formats, blocked resources.
- [Reflexion Subsystem](reflexion.md) — the learning loop and its storage.
- [Configuration Reference](configuration.md) — every tunable.
- [Deploy: Cloud / VM](deploy/cloud.md) — production topologies.
