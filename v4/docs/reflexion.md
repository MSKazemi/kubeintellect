---
description: >-
  How KubeIntellect learns from past sessions — the reflexion subsystem,
  pattern lifecycle, retention policy, the 1-hour cooldown, and the Loki
  dashboard for monitoring it.
---

# Reflexion Subsystem

KubeIntellect remembers which fixes worked on which clusters and surfaces those
patterns in future sessions. This page explains *what* it stores, *why* the
retention and cooldown rules exist, and *how* to monitor it via Loki/Grafana.

The subsystem is layered on top of the five
[Agent Behaviors](agent-behaviors.md) — independent of them, but uses
`matched_playbooks` from `context_fetcher` as one of its keying signals.

---

## Two storage tiers

| Table | Purpose | Read path |
|---|---|---|
| `rca_outcomes` | Append-only log: every direct-answer turn that ran a mutation | `_load_past_rca` — soft, supplementary signal |
| `failure_patterns` | Curated, verified, recurring patterns | `_load_failure_hints` — high-confidence prompt injection |

A row only graduates from `rca_outcomes` to `failure_patterns` when **all** of
the following are true:

1. The matched playbook keys the pattern (R2 — structured key).
2. The cluster was actually healthy after the fix ran (R4 — verification gate).
   A verification snapshot that *could not be read* counts as unverified
   (`verified_resolved = NULL`), never as resolved — see below.
3. The same `(pattern_name, cluster_id)` has been seen at least twice
   (R6 — `occurrence_count >= 2`).
4. Confidence is `>= 0.9` (R5 — only "verified + playbook" reaches that).

Without this gating, a single accidental fix on a flaky test cluster would
become a permanent prompt injection on every future production query.

!!! warning "R4 verifies against a read that can fail"
    The post-fix snapshot runs `kubectl get pods` immediately after a mutation —
    the moment a cluster read is *most* likely to fail. Until 2026-08-20 the exit
    code was not checked, so a failed read scanned clean and R4 returned
    "resolved": an unverified fix was recorded as verified, and
    `promotion.py` selects exactly those rows (`WHERE verified = TRUE`) to mint
    learned rules and detector candidates. A failed verification read now returns
    `(None, None)` — the "failed to run" case the gate always claimed to have —
    and logs a warning naming the kubectl error.

---

## The 1-hour cooldown — why it exists

`REFLEXION_PATTERN_COOLDOWN_HOURS=1` (default).

If you re-trigger the same fault three times within an hour for testing,
**only the first occurrence increments `occurrence_count`**. The other two
refresh `last_seen_at` but leave the count alone. The end-to-end verification
in the production-grade plan explicitly relies on this — re-running scenario-10
twice in fresh sessions seeds the pattern (count = 2); a third run within the
cooldown window must NOT bump it to 3.

**Why we wait the full hour during verification.** The acceptance test in
`.claude/plans/reflexion-production-grade.md` (lines 245–249) requires
demonstrating the cooldown is active by running a *third* fault injection
within an hour and asserting that `occurrence_count` is still `2`. If you
shorten the gap, you cannot distinguish "cooldown is broken" from "two runs
were close together but the third is far enough out." The hour is the contract.

**Why an hour and not a minute.** Real production failures don't repeat 5×
in 10 minutes — that is *test-rig signal*, not a real recurring fault. An hour
is short enough to catch a meaningful re-occurrence (a flaky deploy hitting
the same fault on the next pipeline run) and long enough to drop fault
injection bursts. Set `REFLEXION_PATTERN_COOLDOWN_HOURS=0` to disable; bump
higher if you have very chatty test clusters.

**Cooldown skips are visible.** Every skip emits a structured log line:

```
reflexion: cooldown active for 'playbook=…' (last_seen=…); count not bumped
```

The Loki dashboard surfaces these as a stat panel — a high cooldown-skip
count means your test rig is talking, not your cluster.

---

## Retention — why patterns must age out

Two SQL-level retention rules, applied via `reflexion_purge()`:

| Table | Default retention | Survives forever? |
|---|---|---|
| `rca_outcomes` | `90 days` (`RETAIN_OUTCOMES_DAYS`) | No — append-only log, must rotate |
| `failure_patterns` | `30 days` if **unverified** | Yes if `confidence >= 0.9` |

**Why a hard purge on outcomes.** Every turn that mutates the cluster appends
a row. A modestly used deployment generates thousands of rows per week. The
high-value read (`_load_failure_hints`) doesn't even touch this table — the
soft read (`_load_past_rca`) only joins recent rows. Beyond ~90 days, every
row is dead weight bloating the index.

**Why verified patterns survive.** A pattern that reached `confidence=0.9`
is, by construction, one that:

- matched a known playbook,
- was observed at least twice on the same cluster,
- left the cluster verifiably healthy after the fix.

That is exactly the signal we want to keep. There is no aging policy that
makes this pattern *less* valuable in 60 days. It only ages out via
`demoted=TRUE` (R6 — set when an `outcome_feedback='incorrect'` lands).

**Why unverified patterns DON'T survive.** A pattern that has been sitting
in `failure_patterns` for 30 days without earning its way to confidence 0.9
is one of:

- a test-rig pattern that nobody triggered after the fault was fixed,
- a noisy pattern keyed on a query stub (no playbook match), or
- a one-off that will never recur.

Keeping it injected forever is pure prompt-pollution risk for zero benefit.

**How to run retention.**

```bash
make db-purge                                            # 90d / 30d defaults
RETAIN_OUTCOMES_DAYS=30 RETAIN_PATTERNS_DAYS=14 make db-purge   # tighter
```

Schedule it as a CronJob in production (typically daily at 03:00 cluster time);
the SQL function is idempotent and returns counts.

---

## Cluster identity — why patterns are scoped per cluster

`get_cluster_id()` (in `app/cluster_id.py`) derives a stable cluster
fingerprint — an explicit `CLUSTER_ID` if set, else the kubeconfig context name
and a hash of the kube-apiserver URL, else the `kube-system` namespace UID.
Every `rca_outcomes` row and every `failure_patterns` row carries it
(`cluster_id` column). See
[cluster identity](configuration.md#cluster-identity) for the full order.

**Why.** A pattern learned on a Kind dev cluster is almost never the right
fix for the same symptom on a production EKS cluster. Image registries,
node sizes, RBAC, and CSI drivers all differ. Without scoping, the dev cluster
poisons prod prompts (and vice versa).

The read paths filter `cluster_id IN (current, 'unknown')` — rows from
before this column existed (`'unknown'`) still match anywhere; identified rows
are strict.

!!! warning "`'unknown'` is a shared scope, not a placeholder"
    Because every cluster reads `'unknown'` rows, anything written under that id
    is visible to every cluster sharing the database. That is harmless for a
    single cluster and is exactly the cross-contamination described above for
    two. An in-cluster deployment has no kubeconfig to derive an identity from,
    so it lands on `'unknown'` **and keeps minting new `'unknown'` rows** — it
    never ages out into per-cluster patterns on its own. Set `CLUSTER_ID`
    (Helm: `config.clusterId`) on any cluster that shares a database.

---

## Confidence resolution table (R5)

| Path | Playbook matched | Verified resolved | Confidence | Eligible for promotion |
|---|---|---|---|---|
| Direct answer | yes | yes | **0.9** | **yes** |
| Direct answer | yes | no | 0.7 | no |
| Direct answer | no  | yes | 0.7 | no |
| Direct answer | no  | no | 0.5 | no |
| Synthesis (multi-subagent) | n/a | n/a | model-reported, ≤0.95 | yes if ≥0.9 |
| Read-only turn | n/a | n/a | not recorded | n/a |

The 0.7 / 0.5 buffer rows still get written to `rca_outcomes` so they can
inform the soft read. Only 0.9 reaches the high-value injection.

---

## Loki dashboard — the operational view

A pre-built Grafana dashboard ships at
`deploy/grafana/dashboards/reflexion.json`. Import it into your Grafana
instance (Dashboards → New → Import) and pick your Loki datasource when
prompted.

**Panels:**

| Panel | Question it answers |
|---|---|
| Outcomes recorded (24h) | Are we even writing to the table? |
| Verified resolutions (24h) | Are fixes actually fixing? |
| Patterns seeded (24h) | Are new patterns graduating to `failure_patterns`? |
| Cooldown skips (24h) | Is your test rig inflating counts? |
| Verified vs partial vs regression (rate) | Quality timeseries — verified should dominate |
| Confidence distribution | Are we mostly producing 0.5s, 0.7s, or 0.9s? |
| Pattern seeds (logs) | Audit trail of every promotion |
| Pattern bumps + demotions (logs) | Audit trail of every count bump |
| Reflexion failures (logs) | Async write/verify failures (don't break user response) |

**Why a Loki dashboard, not a database query view.** The dashboard reads
structured log lines (`reflexion: outcome recorded …`) emitted at the moment
the event happens. That gives time resolution, retention beyond the DB purge,
and decoupling — even if the DB is briefly unavailable, the log line still
lands. The DB is the source of truth for *current* state; Loki is the source
of truth for *what happened*.

The log lines come from:

- `app/db/memory_store.py` — `outcome recorded`, `seeded pattern`,
  `cooldown active`, `bumped pattern`, `demoted pattern`
- `app/agent/nodes/coordinator.py` — `recorded RCA outcome`,
  `failed to schedule outcome write`

All prefixed `reflexion:` so the dashboard's LogQL filters are stable.

---

## Feature flags

All knobs live in `app/core/config.py`. See
[Configuration → Reflexion flags](configuration.md#reflexion-flags) for the
full table.

| Flag | Default | Effect |
|---|---|---|
| `REFLEXION_ENABLED` | `True` | Master switch |
| `REFLEXION_VERIFY_RESOLUTION` | `True` | R4 — post-fix verification snapshot |
| `REFLEXION_REDACT_SECRETS` | `True` | R8 — strip credentials before storing manifests |
| `REFLEXION_PATTERN_COOLDOWN_HOURS` | `1` | R6 — minimum gap between count bumps |
| `REFLEXION_PATTERN_DECAY_DAYS` | `30` | R6 — read-side filter on stale patterns |

Each can be flipped independently to roll back a specific behavior without
disabling the whole subsystem.
