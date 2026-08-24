---
description: >-
  The KubeIntellect V4 memory hierarchy — episodic recall of past
  investigations, a temporal knowledge graph of the cluster, the consolidation
  worker, and what gets injected into the agent's prompt.
---

# Memory Hierarchy (V4)

V2 KubeIntellect remembers verified fix patterns (the
[reflexion subsystem](reflexion.md), which remains accurate and active). V4
adds a fuller memory hierarchy: every investigation becomes a recallable
episode, and the cluster's structure is tracked over time so "what changed in
the last 15 minutes" is one indexed query instead of an investigation.

---

## The four tiers

| Tier | What it holds | What you see |
|---|---|---|
| **L0 — working memory** | The current session's messages and state (in-process) | The conversation you're having |
| **L1 — episodes** | Every investigation — user-asked or autonomous — as one summarized, recallable record | *"Similar past episodes"* informing answers; rows in the [digest](autonomy.md#the-morning-digest) |
| **L2 — temporal knowledge graph** | Cluster entities (Pods, Nodes, workloads, Incidents) and **valid-time edges** between them (`runs_on`, `owns`, `crashed_with`) | *"Recent cluster changes"* context in answers — e.g. the agent already knows a pod moved nodes 3 minutes ago |
| **L3 — procedural** | Playbooks, verified failure patterns ([reflexion](reflexion.md)), and learned detector candidates | The agent following proven investigation recipes |

L2 edges carry validity windows: an edge with no end time is currently true;
when a pod is deleted or moves nodes, the old edge is closed with a timestamp
rather than erased. That turns *"what changed between 14:02 and 14:07"* into a
single query over edge open/close events.

Each edge also records **what it was derived from**. An edge learned from the cluster
watch cites the apiserver's own identity for the exact object version that produced it —
`uid` plus `resourceVersion` — so *"why does the agent believe this pod runs on that node"*
has an answer you can check, rather than an unqualified `observation` label. Two limits
stated plainly: a `resourceVersion` is not retained indefinitely and a deleted object is
gone, so an old citation may name something you can no longer fetch; and an edge built from
an observation that carried no identity is stored with **no** citation rather than a
synthetic one, because a reference that looks resolvable and is not would be worse than an
honest blank.

---

## What gets injected into the prompt

At the start of every turn, the memory loader adds two compact blocks to the
agent's context (when relevant content exists):

- **Similar past episodes** — the top-3 most similar episodes for *this
  cluster* (text similarity + recency, with a noise floor so unrelated
  episodes are dropped). Rendered at roughly 400 tokens for k=3, each line
  tagged with its outcome and verification status.
- **Recent cluster changes (last 15m)** — up to 12 lines of knowledge-graph
  edge changes, e.g.
  `14:02:11 opened Pod/prod/web-1 -runs_on-> Node//worker-2`, with a
  `(+N more)` tail.

The injection is budget-bounded and latency-tracked; if the hierarchy is
inactive or finds nothing relevant, nothing is injected. Memory can **never
break a request** — a memory failure degrades the investigation, it does not
fail it.

Degrading, however, is not the same as hiding. Episode recall distinguishes
*"nothing matched"* from *"I could not check"*: when recall fails outright, the
prompt carries an explicit **`## Memory unavailable`** block instructing the
model not to report the incident as having no precedent, and the log line
records `degraded=true`. Returning an empty result for both made a database
outage look identical to a cluster with no history — in the prompt, and in the
`episodes=0` log line. A genuine empty recall still injects nothing at all.

The **recent-changes** half of the same block follows the same rule. "What changed
in the last 15 minutes" is the first question of an incident, and an omitted
`## Recent cluster changes` section is exactly the shape a calm cluster makes — so
a failed graph read used to reach the model as *nothing changed*. A failed read now
injects **`## Recent changes unavailable`**, which explicitly tells the model not to
rule out a recent change as the cause, and sets `degraded=true`. A cluster that
genuinely changed nothing still injects nothing. Graph **writes** are unaffected:
a failed observation write is still swallowed, because nothing downstream reads its
result as a fact.

The same rule applies **per section**. The pinned context is assembled from four
independent queries — operator preferences, failure hints, session notes, past
RCA — and one of them failing costs only that section, never the other three. But
it is never silent: the prompt then opens with a **`## Memory partially
unavailable`** block naming exactly what could not be read, e.g.

```
## Memory partially unavailable
Stored operator preferences could NOT be read — this is not the same as there
being none. Do not assume the user has no preferences or that this issue has no
precedent; say that part of the stored history could not be checked.
```

and the failure is logged at `WARNING` with the section name. The notice is
placed first so the context budget can never truncate it away. A section that
simply has no rows — the normal state for a new user — is not a failure and says
nothing.

The third variant of the same rule is **capping**. Each section is bounded so the
whole block stays inside its ~500-token budget — 8 preferences, 5 failure
patterns, 3 session notes, 3 past RCA outcomes — and the bound is deliberate.
What was wrong was leaving it unsaid: measured on an operator with 12 explicit
preferences, the prompt carried 8 under a header reading *"(remembered)"*, and
`NEVER drain node-07, it hosts the license server` was among the four dropped.
Read literally — the only way a model can read it — that block says the
instruction does not exist.

Each section now queries **one row past its cap**. Getting that row back is proof
more exist, so the section closes with a line saying so; not getting it is proof
they do not, and the section stays silent. That is one query rather than two, and
it is why the notice never states a number nobody counted:

```
## Operator Preferences (remembered)
  ops-01: never restart pods in prod without approval
  … (7 more)
  … MORE operator preferences are stored than the ones listed above and are NOT
  shown here (oldest/lowest-ranked omitted for space). Absence from this list is
  NOT evidence that none exists — ask, or say you only checked the most relevant.
```

A stored remediation is cut to 160 characters for the same budget reason, and is
now marked `…[fix truncated, not the whole command]` when it is. A `kubectl`
command cut mid-flag reads as a complete command otherwise.

---

## Operator preferences

Alongside the four tiers, KubeIntellect keeps a **learned preference layer**
(`PREFERENCE_MEMORY_ENABLED`, default on) — memory of *how you like to operate*, not
of cluster state:

- **Explicit** preferences you set (`kq preference set remediation dry-run-first`) are
  stored at confidence 1.0 and never decay.
- **Behaviour-inferred** preferences are learned from your history by the consolidation
  worker — e.g. after you repeatedly work in one namespace it infers
  `default_namespace` (needs `PREFERENCE_INFER_MIN_OCCURRENCE` recurrences, tracked
  with a confidence score).
- At prompt time the top preferences (explicit first, then inferred above
  `PREFERENCE_MIN_CONFIDENCE`) are injected as a compact block, so the agent applies
  your conventions across sessions.
- **Forgetting:** inferred preferences not re-seen within `PREFERENCE_DECAY_DAYS` are
  purged (`preference_purge()`), so stale guesses don't accumulate.

Managed via `kq preference set/list/forget` and the `/v1/preferences` API (GET/PUT/DELETE).
Like all memory, it is fail-open — a preference-store outage never breaks a response.

## The consolidation worker

A background worker runs every 10 minutes (plus once at startup) and keeps the
hierarchy healthy — all passes are deterministic, no LLM calls:

| Pass | What it does | When |
|---|---|---|
| **Backfill** | One-shot, idempotent migration of legacy `rca_outcomes` rows into episodes (`trigger_kind=backfill`) | Startup only |
| **Stale-edge cleanup** | Closes graph edges whose pod hasn't been observed for 24 h (catches watcher downtime; normal deletes close edges live) | Every pass |
| **Detector-candidate proposals** | Verified, recurring failure patterns (occurrence ≥ 2, confidence ≥ 0.9) whose playbooks carry compiled `detect:` blocks become candidate rows in the `detectors` table — **human review required before activation**; nothing self-activates | Every pass |

Each pass is guarded independently, so one that fails never stops the loop or the passes after
it. Every pass also returns a counter, and a **failed pass fails safe to `0`** — which is the same
number a pass returns when it ran and found nothing to do. Until 2026-08-24 that was the whole
report: a subsystem in which every statement was failing (schema drift after a migration, a
revoked grant) returned counters byte-identical to a healthy idle cluster, and the worker's own
summary line was suppressed for both.

The worker now reports the two apart. A pass that completes with work done logs at `INFO`; a pass
in which anything failed logs at `WARNING`, names each failed pass and its reason, and sets
`failed_passes` in the returned counters:

```text
consolidation_pass {"stale_edges_closed": 7, "detector_candidates": 1, "failed_passes": 0}
consolidation_pass INCOMPLETE — 2 of 5 passes failed [prefs_inferred: permission denied for
  relation episodes; backfilled: relation "kg_edges" does not exist] · counters {...}
```

A genuinely idle pass still logs nothing — at one pass every 10 minutes, a heartbeat for "there
was nothing to do" is noise, and `/healthz` already answers whether the worker is running at all.

---

## When the hierarchy is not running

The hierarchy needs Postgres, and the API pod is routinely scheduled before Postgres accepts
connections. That is not fatal — the pool is retried every 30 seconds until it comes up, and
reconnecting completes startup properly (L1, L2, the observation drain and the consolidation
worker all start then), so a rollout race costs no restart.

While it is down, **nothing is recorded**: no episodes, no knowledge-graph edges, no
consolidation, no preference learning. An empty knowledge graph looks exactly like a cluster in
which nothing has happened, so ask the process rather than the tables:

```bash
curl -s localhost:8000/healthz | jq .memory
# {"enabled": true,  "state": "ready",       "reason": "",              "observations_dropped": 0}
# {"enabled": false, "state": "unavailable", "reason": "Connect call failed ...", "observations_dropped": 8123}
```

| `state` | Meaning |
|---|---|
| `ready` | L1, L2 and consolidation are running. |
| `flag` | `MEMORY_HIERARCHY_ENABLED=false`. Configuration, not a fault. |
| `sqlite` | `USE_SQLITE=true`; the hierarchy needs Postgres. Configuration, not a fault. |
| `unavailable` | Postgres refused the connection. **Nothing is being recorded**; a reconnect loop is retrying. |

`observations_dropped` counts sensorium observations discarded since the process started —
those that arrived with no queue to receive them, and those dropped because the queue was full.
It does not reset on reconnect, so a non-zero value on a `ready` replica means there is a gap
earlier in the graph. The first discarded observation and every thousandth after it log a
warning, so the outage does not go silent once the startup log has scrolled away.

---

## Configuration & compatibility

| Flag | Default | Effect |
|---|---|---|
| `MEMORY_HIERARCHY_ENABLED` | `True` | Master switch for L1 + L2 + consolidation. |
| `MEMORY_HYBRID_RETRIEVAL` | `False` | *Experimental (Memory V5).* Fuse trigram + full-text recall via Reciprocal Rank Fusion for sharper episode recall. Falls back to the trigram baseline on any error. |
| `MEMORY_BITEMPORAL_ENABLED` | `False` | *Experimental (Memory V5).* Give the L2 knowledge graph a second (transaction-time) axis: point-in-time `as_of()` queries, audit-preserving retract-not-delete, and an ingest-lag freshness signal. |
| `MEMORY_KG_PPR` | `False` | *Experimental (Memory V5).* Multi-hop **blast-radius**: rank the entities most related to an incident via a bounded subgraph + in-process Personalized PageRank. |
| `MEMORY_WRITE_RECONCILE` | `False` | *Experimental (Memory V5).* Reconcile writes (ADD/UPDATE/**retract**/NOOP) behind a salience gate so the graph stays compact and contradictions supersede instead of piling up. |
| `MEMORY_PROMOTION` | `False` | *Experimental (Memory V5).* Close the **learning loop**: turn verified, recurring incidents into reusable `semantic_rules` (IF→THEN), injected into the prompt and eligible to seed a human-reviewed detector. |
| `MEMORY_IMPORTANCE` | `False` | *Experimental (Memory V5).* Weight retention by **importance** (incident severity) and **surprise** (KG-novelty): recall ranks recency × importance × relevance (ranking only — nothing is ever deleted), and a surprise gate skips redundant low-value writes so noise doesn't accrete. Novelty needs `pg_trgm`; an episode whose novelty could not be scored stores `NULL`, never `1.0`, and is never gated on the score that failed. |
| `MEMORY_PROSPECTIVE` | `False` | *Experimental (Memory V5).* **Remember to act later**: after an autonomous fix the watchtower schedules a "did the fix hold?" re-check; the consolidation scheduler fires due re-checks through the autonomy ladder and records the outcome. |
| `MEMORY_SECURITY_HARDENING` | `False` | *Experimental (Memory V5).* **Defend the memory**: a write-admission guard (diverse, non-LLM validators — provenance trust, injection-signature, rate limit, contradiction) quarantines **MINJA query-only poisoning** before it is learned; provenance `trust` is stamped on each episode. Right-to-be-forgotten + RLS tenant scaffolding included. |
| `MEMORY_SUMMARY_TREE` | `False` | *Experimental (Memory V5).* **Zoom out**: build RAPTOR/GraphRAG-style theme summaries over episode clusters so the agent answers "what keeps failing in X?" without scanning every incident. Regeneration is tied to KG change-rate, not a clock. |

**Memory V5 (experimental, off by default).** The flags above are the
implemented slices (P1–P8) of the Memory V5 upgrade — a
state-of-the-art-grounded evolution of the hierarchy: hybrid retrieval, a
bi-temporal knowledge graph, multi-hop blast-radius, write reconciliation, the
episode→rule→detector promotion loop, importance/surprise-weighted retention
plus prospective ("re-check later") memory, a security-hardened write path
(MINJA-poisoning defense + RTBF + RLS tenant scaffolding), and a RAPTOR-style
theme summary hierarchy. They are additive,
PostgreSQL 16-compatible, and preserve the never-break-a-response discipline;
enable them per-cluster to trial the upgrade. Design and rationale live in the
internal design track (ADRs 013–018).

**Stock Postgres is enough.** Episode recall uses the `pg_trgm` extension
(text trigram similarity) — no special database build. `pgvector` is optional
and currently unused: the embedding column is only added once an embedding
provider is configured, and recall degrades gracefully to the trigram baseline
without it.

**SQLite mode** (`USE_SQLITE=true`) disables the hierarchy entirely — the
agent still works, with V2 session memory only.

**Privacy.** Episode summaries, trigger details, and root causes pass through
the same secret redaction as [reflexion](reflexion.md)
(`REFLEXION_REDACT_SECRETS`, default `True`) before being stored.

---

## Relationship to reflexion

[Reflexion](reflexion.md) stays the authority on **verified fix patterns** —
its promotion gates, cooldown, retention, and cluster scoping are unchanged
and documented there. The V4 hierarchy sits around it:

- Reflexion's `rca_outcomes` history is backfilled into L1 episodes, so old
  investigations become recallable too.
- Reflexion's verified `failure_patterns` are the input to L3
  detector-candidate proposals.
- Both share the same redaction and the same never-break-a-response
  discipline.

---

## Related

- [Reflexion Subsystem](reflexion.md) — pattern promotion, cooldown, retention.
- [Autonomous Operations](autonomy.md) — autonomous investigations write episodes too.
- [Flight Recorder & Replay](flight-recorder.md) — the full, tamper-evident event record behind each episode.
