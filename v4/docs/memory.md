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
| `MEMORY_IMPORTANCE` | `False` | *Experimental (Memory V5).* Weight retention by **importance** (incident severity) and **surprise** (KG-novelty): recall ranks recency × importance × relevance (ranking only — nothing is ever deleted), and a surprise gate skips redundant low-value writes so noise doesn't accrete. |
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
