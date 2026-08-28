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

#### Prove the restore was complete

A dump tells you a backup was taken. It does not tell you a restore brought everything back — and
for two tables the difference is dangerous. `decision_log` and `memory_audit` are hash chains, and
a restore that silently drops their **newest** rows breaks no link: the surviving rows hash
correctly, chain verification returns intact, and the postmortem prints its intact-chain banner
over a record that is quietly short. That is what `decision_log_head` and `memory_chain_head` are
for, and they only work if something compares them.

So take a **manifest** with every dump, and verify against it after every restore:

```bash
# At backup time, beside the pg_dump
kubeintellect backup-manifest --out kubeintellect-$(date +%F).manifest.json --note nightly
pg_dump "$DATABASE_URL" > kubeintellect-$(date +%F).sql

# After the restore — exits 1 if anything is missing, so it is safe to run in a rehearsal
kubeintellect verify-restore kubeintellect-2026-01-01.manifest.json
```

The manifest records the schema version and DDL fingerprint, exact row counts for every table
whose loss is a data-loss event, and how far each hash chain got. `verify-restore` re-measures and
reports **every** discrepancy, not the first — mid-incident, a list of three things to fix beats
one error and a re-run. A short table, a table the restore never created and a truncated chain are
three different messages, because they need three different responses.

#### RPO and RTO

These are **your** numbers, set by the schedule you choose — KubeIntellect does not take backups
for you and does not ship a scheduled backup in the Helm chart. What follows is the reference
deployment, so the figures are concrete rather than aspirational:

| | Reference value | What sets it |
|---|---|---|
| **RPO** (data you can lose) | **24 h** | A nightly `pg_dump`. Hourly gets you 1 h; continuous WAL archiving (`pg_basebackup` + `archive_command`) gets you minutes, and is the right answer if losing a day of incident history is not acceptable. |
| **RTO** (time to be serving again) | **minutes** | `psql --single-transaction` restore time, which is dominated by database size, plus a rollout. The server needs no warm-up: memory, the recorder and the sensorium all reattach on their own. |
| **Verified?** | Only if you run it | `kubeintellect verify-restore` is the check. A backup nobody has ever restored is an untested assumption, not a recovery plan — rehearse it on a scratch database on the same cadence you claim the RPO on. |

!!! warning "What is not automated"
    There is no scheduled backup in the chart, no off-site copy, and no automated restore
    rehearsal. Those are deliberate gaps, not oversights — where the dump goes and who may read it
    is a decision only the operator can make, and it usually belongs to a platform-wide backup
    system rather than to one application. Wire `backup-manifest` + `pg_dump` into whatever takes
    your other backups, and store the manifest **beside** the dump.

### Archiving a hash chain

`decision_log` and `memory_audit` are the two fastest-growing tables in the schema and the two
retention will never prune (see below). Before either can ever be truncated, the rows have to
leave the box in a form that can still be *checked*:

```bash
kubeintellect chain-export memory_audit "$CLUSTER_ID" --out memory-audit-$(date +%F).json
kubeintellect chain-verify-export memory-audit-2026-01-01.json    # exits 1 if anything is wrong
```

The archive carries the rows verbatim, the head anchor as it stood, the link verdict at export
time and a SHA-256 over all of it, so `chain-verify-export` can check it years later **with no
database present**. A truncated source chain is visible in the archive even though every archived
link verifies — the anchor is what says rows are missing, which is the whole reason it is in
there.

Two limits worth stating plainly. The hash proves the archive has not been *edited*; it does not
prove who wrote it, so store the archive where this database's operators cannot silently replace
it. And the archive is not itself the prune — it is the thing that makes one legible.

### Pruning a hash chain, once it is archived

```bash
kubeintellect chain-truncate memory-audit-2026-01-01.json               # dry run
kubeintellect chain-truncate memory-audit-2026-01-01.json --yes --note "90-day retention"
```

This is the only command that deletes chain rows, and it does two things in one transaction: it
records the gap in `chain_truncation` — scope, `through_seq`, where the chain resumes and from
which hash, and the archive's hash — and then removes the archived rows. After it, the verifier
reports the chain **intact**; the same rows deleted in `psql` are reported **TAMPERED**, which is
the point and is not a bug you have found. It refuses on a stale archive, a chain that changed
since the export, a truncation that would leave nothing, or a surviving chain that does not link
to the archive's last hash. Full list in the CLI reference.

Three operational consequences:

- **Re-run `kubeintellect db-init` first.** `chain_truncation` is schema version 2. Until then
  the truncation fails cleanly (nothing is deleted) and `kq v5-status` reports a stale schema.
- **Keep the archive.** It is the only copy of what was removed, and the truncation record points
  at its hash. Losing it does not break verification; it loses the evidence.
- **Retention still never does this on a schedule.** `RETENTION_*` will not prune either ledger.
  Deciding that a chain may be shortened stays a human act, per chain, with an archive in hand.

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
id, session, user, role, path, status code, and duration — **provided the audit pool
is up**; see [When nothing is being audited](#when-nothing-is-being-audited) below,
because an empty table on its own does not tell you which. Query it for usage and
review:

```sql
-- Recent activity
SELECT created_at, user_id, user_role, path, status_code, duration_ms
FROM request_log ORDER BY created_at DESC LIMIT 50;

-- Who is using which role
SELECT user_role, count(*) FROM request_log GROUP BY user_role;
```

### When nothing is being audited

There are two ways for `request_log` to stop receiving rows, and an empty table looks
identical to a quiet server — so ask the process rather than the table:

```bash
curl -s localhost:8000/healthz | jq .audit
# {"enabled": true,  "state": "ready",       "reason": "",             "dropped": 0}
# {"enabled": false, "state": "sqlite",      "reason": "USE_SQLITE...", "dropped": 0}
# {"enabled": false, "state": "unavailable", "reason": "Connect call failed ...", "dropped": 412}
```

| `state` | Meaning | Action |
|---|---|---|
| `ready` | Rows are being written. | — |
| `sqlite` | `USE_SQLITE=true`: there is no Postgres to write to. Configuration, not a fault. | Switch to Postgres if you need an audit trail. |
| `unavailable` | Postgres refused the connection. **This replica is auditing nothing.** | Fix connectivity — it reconnects on its own, see below. |

`dropped` is the number of requests that went unrecorded since the process started. It is the
fact that replaces "the table looks empty", and it does not reset on reconnect — so a non-zero
`dropped` on a `ready` replica means there is a gap earlier in the log.

**A failed connect is not permanent.** The pod being scheduled before Postgres accepts
connections is the ordinary case during a rollout; the pool is retried from the write path at
most once every 30 seconds, so the audit log starts on its own without a restart. The first
dropped request and every hundredth after it log a warning, so the outage does not go silent
once the startup log has scrolled away.

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
