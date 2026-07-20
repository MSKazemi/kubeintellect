---
description: >-
  Running KubeIntellect v3 in production — the systemd service, scaling,
  API-key rotation, database backup and connection pooling, the audit log,
  blocked namespaces/resources, monitoring, and upgrades.
---

# Operations Guide

Day-2 guidance for running KubeIntellect v3 as a shared, long-lived service.

---

## Running as a service

On a host (pip install), the recommended way to keep the server running is the
built-in **systemd user service** (`app/cli.py`):

```bash
kubeintellect service install      # write unit, enable, start now
kubeintellect service status       # systemctl --user status kubeintellect
kubeintellect service logs         # journalctl --user -u kubeintellect -f
kubeintellect service stop         # stop without uninstalling
kubeintellect service uninstall    # remove entirely
```

The unit (`~/.config/systemd/user/kubeintellect.service`) runs
`kubeintellect serve` with `Restart=on-failure` and loads `~/.kubeintellect/.env`
via `EnvironmentFile`. Because it's a *user* service, enable lingering if you
want it to survive logout:

```bash
sudo loginctl enable-linger "$USER"
```

`kubeintellect set` restarts the service automatically when it is active, so
config changes take effect immediately.

For in-cluster deployments, run the container behind a Deployment + Service — see
[Deploy](deploy/cloud.md).

---

## Scaling

The server is **stateless** — all conversation and interrupt state lives in the
database (the LangGraph checkpointer), not in process memory. That means you can
run multiple replicas behind a load balancer, provided they share one database.

| Deployment | Database | Replicas |
|---|---|---|
| Single host | SQLite or PostgreSQL | 1 |
| Kubernetes / HA | **PostgreSQL required** | 2+ |

SQLite mode is single-host only (the checkpointer file can't be shared safely). A
horizontally-scaled deployment must set `DATABASE_URL` to a shared PostgreSQL.

The practical bottleneck is **LLM latency and rate limits**, not CPU — an
investigation spends most of its wall-clock time waiting on model calls. Scale by
raising your provider quota and adding replicas, not by over-provisioning CPU.

---

## Rotating API keys

Keys are comma-separated per role, so you can rotate with zero downtime by adding
the new key before removing the old one:

```bash
# 1. Add the new key alongside the old one
kubeintellect set KUBEINTELLECT_ADMIN_KEYS=ki-admin-old,ki-admin-new
# (service restarts automatically if running)

# 2. Distribute the new key, then revoke the old one
kubeintellect set KUBEINTELLECT_ADMIN_KEYS=ki-admin-new
```

The same applies to `KUBEINTELLECT_OPERATOR_KEYS`, `KUBEINTELLECT_READONLY_KEYS`,
and `KUBEINTELLECT_SUPERADMIN_KEYS`. For time-boxed read-only access without a
restart, use HMAC demo keys (`AUTH_BACKEND=hmac` + `DEMO_KEY_HMAC_SECRET`) — see
[Security](security.md#1-api-authentication).

Generate a key: `openssl rand -hex 20`.

---

## Database

In PostgreSQL mode, KubeIntellect stores everything in one database — the same DB
holds the LangGraph checkpoints, the [memory](memory.md) tables, and the audit
log (`app/db/schema.sql`):

| Table | Contents |
|---|---|
| `user_prefs` | Per-user preferences (memory topic `user_prefs`). |
| `session_notes` | Per-session notes (memory topic `session_notes`). |
| `rca_outcomes` | Recorded root-cause outcomes (memory topic `past_rca`). |
| `failure_patterns` | Promoted, recurring failure patterns (memory topic `failure_hints`). |
| `request_log` | Audit log — one row per API request. |
| `runbooks` | Stored runbook entries. |
| checkpoint tables | LangGraph conversation + interrupt state (created by the checkpointer's `setup()`). |

Initialize the schema once with `kubeintellect db-init` (a no-op in SQLite mode —
the schema is created on first start).

### Connection pooling

| Variable | Default | Notes |
|---|---|---|
| `POSTGRES_POOL_MIN_CONN` | `1` | Minimum pooled connections. |
| `POSTGRES_POOL_MAX_CONN` | `10` | Maximum pooled connections. |

The audit pool is separate and small (min 1 / max 3, `app/db/audit.py`). Size the
main pool to your replica count and expected concurrency; each in-flight
investigation holds at most a few connections.

### Backup & restore

Standard PostgreSQL tooling:

```bash
pg_dump "$DATABASE_URL" > kubeintellect-backup.sql
psql    "$DATABASE_URL" < kubeintellect-backup.sql
```

In SQLite mode the entire state is one file — copy it while the server is
stopped:

```bash
cp ~/.kubeintellect/kubeintellect.db ~/kubeintellect-backup.db
```

!!! note "No built-in purge function in v3"
    v3 does not ship an automated retention/purge routine. The memory tables are
    small and self-limiting — `failure_hints` only surfaces high-confidence,
    recurring patterns, and `past_rca` recall is capped to the last few outcomes.
    If you want to prune old rows on a schedule, run your own `DELETE … WHERE
    created_at < now() - interval '90 days'` against `rca_outcomes` /
    `request_log` via cron or a Kubernetes CronJob.

---

## Audit log

Every API request is recorded to `request_log` (`app/db/audit.py`): request id,
session id, user id, role, path, method, status, and timestamp. Writes are
**fire-and-forget** — a database hiccup never blocks or fails a request, and the
subsystem is disabled entirely in SQLite mode.

Query usage, e.g. requests per role over the last day:

```sql
SELECT user_role, count(*)
FROM request_log
WHERE created_at > now() - interval '1 day'
GROUP BY user_role;
```

The `request_id` (returned to logs and set via `request_id_var`) ties an audit
row to the structured server logs. Set `LOG_FORMAT=json` to ship logs to
Loki/Grafana or another aggregator.

---

## Blocked namespaces & resources

Two safety fences constrain what the agent may touch, regardless of role
(`app/core/config.py`, enforced in `app/tools/kubectl_tool.py`):

```bash
# Write verbs refused on these namespaces (reads still allowed; superadmin excepted)
KUBECTL_BLOCKED_NAMESPACES=kubeintellect,monitoring,kube-system,kube-public,kube-node-lease,ingress-nginx,cert-manager

# These resource types refused for every verb, every namespace, every role
KUBECTL_BLOCKED_RESOURCES=secret,secrets,serviceaccount,serviceaccounts
```

Override per-deployment via env vars (or Helm `config.blockedNamespaces` /
`config.blockedResources`). Narrow them only if you understand the trade-off —
the resource block is the control that keeps cluster credentials unreadable. See
[Security](security.md#5-secret-protection-why-users-cant-steal-the-api-key).

---

## Monitoring

- **Health probe:** `GET /healthz` (and `/v1/healthz`) returns `{"status":"ok"}` —
  wire it to your load balancer / Kubernetes liveness+readiness probes.
- **Connectivity dashboard:** `kubeintellect status` checks the LLM, database,
  kubectl, kubeconfig, auth, and observability endpoints in one shot.
- **LLM tracing:** enable Langfuse (`LANGFUSE_ENABLED=true` + host/keys) to trace
  every coordinator and subagent call — cost, tokens, and latency per session.
  Traces are tagged `version:<KI_VERSION>` so a shared Langfuse project can break
  cost down per KubeIntellect version.
- **Event replay:** `GET /v1/events/replay/{session_id}` re-emits a session's
  event history for post-mortem review.

---

## Upgrades

**pip install:**

```bash
pip install --upgrade kubeintellect
kubeintellect db-init          # apply any additive schema changes (PostgreSQL)
kubeintellect service stop && kubeintellect service start   # if running as a service
```

**Kubernetes (Helm):** bump the chart/image version and `helm upgrade`; the
db-init job applies schema changes. Schema migrations are additive (`CREATE TABLE
IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`), so upgrades are safe — but take a
database backup before a major upgrade.

---

## Related

- [Security](security.md) — roles, HITL, and secret protection.
- [Memory](memory.md) — the cross-session memory tables.
- [Configuration](configuration.md) — every environment variable.
- [Deploy: Cloud / VM](deploy/cloud.md) — in-cluster deployment.
