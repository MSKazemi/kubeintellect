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

# Restore
psql "$DATABASE_URL" < kubeintellect-2026-01-01.sql
```

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
server logs — set `LOG_FORMAT=json` to ship structured logs to Loki/Grafana.

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

---

## Related

- [Security](security.md) — roles, key formats, blocked resources.
- [Reflexion Subsystem](reflexion.md) — the learning loop and its storage.
- [Configuration Reference](configuration.md) — every tunable.
- [Deploy: Cloud / VM](deploy/cloud.md) — production topologies.
