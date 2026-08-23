-- KubeIntellect V2 database schema
-- Run once: psql -v ON_ERROR_STOP=1 --single-transaction $POSTGRES_DSN -f app/db/schema.sql
-- (both flags matter: psql otherwise exits 0 after a failed statement)

-- ── User preferences ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);

-- Preference memory (MemoryAgent) — additive columns, idempotent for existing
-- deployments. Explicit prefs are set by the user (confidence 1.0, immortal);
-- inferred prefs are learned from behaviour and decay/forget when not reinforced.
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS source           TEXT    NOT NULL DEFAULT 'explicit';  -- explicit | inferred
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS confidence       REAL    NOT NULL DEFAULT 1.0;
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS occurrence_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_user_prefs_active
    ON user_prefs (user_id, confidence DESC, last_seen_at DESC);

-- Forgetting policy for inferred preferences. Explicit prefs are never purged.
-- Returns the number of stale, low-confidence inferred preferences deleted.
CREATE OR REPLACE FUNCTION preference_purge(
    decay_days     INTEGER DEFAULT 60,
    min_confidence REAL    DEFAULT 0.3
) RETURNS BIGINT AS $$
DECLARE
    deleted BIGINT;
BEGIN
    DELETE FROM user_prefs
        WHERE source = 'inferred'
          AND confidence < min_confidence
          AND last_seen_at < now() - (decay_days || ' days')::INTERVAL;
    GET DIAGNOSTICS deleted = ROW_COUNT;
    RETURN deleted;
END;
$$ LANGUAGE plpgsql;

-- ── Session notes ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS session_notes (
    id         BIGSERIAL PRIMARY KEY,
    session_id TEXT        NOT NULL,
    note       TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_session_notes_session_id ON session_notes (session_id);

-- ── RCA outcomes (self-improvement source) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS rca_outcomes (
    id               BIGSERIAL PRIMARY KEY,
    session_id       TEXT          NOT NULL,
    user_id          TEXT          NOT NULL,
    root_cause       TEXT          NOT NULL,
    confidence       FLOAT         NOT NULL,
    recommended_fix  TEXT          NOT NULL,
    outcome_feedback TEXT,                     -- "resolved" | "partial" | "regression" | "incorrect" | NULL
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rca_outcomes_user_id ON rca_outcomes (user_id, created_at DESC);

-- Reflexion v2 — additive columns. Idempotent for existing deployments.
ALTER TABLE rca_outcomes ADD COLUMN IF NOT EXISTS cluster_id        TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE rca_outcomes ADD COLUMN IF NOT EXISTS namespace         TEXT;
ALTER TABLE rca_outcomes ADD COLUMN IF NOT EXISTS verified_resolved BOOLEAN;
ALTER TABLE rca_outcomes ADD COLUMN IF NOT EXISTS playbooks_matched TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE rca_outcomes ADD COLUMN IF NOT EXISTS created_by_role   TEXT;
ALTER TABLE rca_outcomes ADD COLUMN IF NOT EXISTS request_id        TEXT;
CREATE INDEX IF NOT EXISTS idx_rca_outcomes_cluster
    ON rca_outcomes (cluster_id, user_id, created_at DESC);

-- ── Failure patterns (auto-seeded; verified, confidence ≥0.9 AND occurrence ≥2) ─
CREATE TABLE IF NOT EXISTS failure_patterns (
    pattern_name     TEXT    PRIMARY KEY,
    description      TEXT    NOT NULL,
    recommended_fix  TEXT    NOT NULL,
    confidence       FLOAT   NOT NULL DEFAULT 0.0,
    occurrence_count INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Reflexion v2 — pattern lifecycle (cluster scoping, decay, demotion).
ALTER TABLE failure_patterns ADD COLUMN IF NOT EXISTS cluster_id   TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE failure_patterns ADD COLUMN IF NOT EXISTS namespace    TEXT;
ALTER TABLE failure_patterns ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE failure_patterns ADD COLUMN IF NOT EXISTS demoted      BOOLEAN NOT NULL DEFAULT FALSE;
-- Composite uniqueness on (pattern_name, cluster_id) so the same pattern can
-- exist independently per cluster. The legacy single-column PK stays for
-- backwards compat; new writes use ON CONFLICT against the composite index.
CREATE UNIQUE INDEX IF NOT EXISTS uq_failure_patterns_cluster
    ON failure_patterns (pattern_name, cluster_id);
CREATE INDEX IF NOT EXISTS idx_failure_patterns_active
    ON failure_patterns (cluster_id, last_seen_at DESC) WHERE demoted = FALSE;

-- ── API request audit log ────────────────────────────────────────────────────
-- Every POST /v1/chat/completions is recorded here so you can see who ran what.
-- user_role tells you if it was an admin, operator, or readonly user.
-- This is KubeIntellect's own Postgres (POSTGRES_DSN), NOT Langfuse's Postgres.
CREATE TABLE IF NOT EXISTS request_log (
    id           BIGSERIAL    PRIMARY KEY,
    request_id   TEXT,
    session_id   TEXT,
    user_id      TEXT,
    user_role    TEXT,
    path         TEXT         NOT NULL,
    method       TEXT         NOT NULL,
    status_code  INTEGER,
    duration_ms  FLOAT,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_request_log_user_id    ON request_log (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_request_log_session_id ON request_log (session_id, created_at DESC);

-- ── Runbooks ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runbooks (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    tags        TEXT[] NOT NULL DEFAULT '{}',
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Reflexion retention (R8) ─────────────────────────────────────────────────
-- Single function, idempotent. Call from cron, ops command, or `make db-purge`.
-- Returns a row of (rca_outcomes_deleted, failure_patterns_deleted).
-- Policy:
--   * rca_outcomes  older than `retain_outcomes_days` (default 90) — purged.
--   * failure_patterns whose last_seen_at is older than `retain_patterns_days`
--     (default 30) AND that never reached confidence ≥ 0.9 — purged.
--   Verified, high-confidence patterns survive forever (until demoted).
CREATE OR REPLACE FUNCTION reflexion_purge(
    retain_outcomes_days INTEGER DEFAULT 90,
    retain_patterns_days INTEGER DEFAULT 30
) RETURNS TABLE (rca_deleted BIGINT, patterns_deleted BIGINT) AS $$
DECLARE
    rca_count BIGINT;
    pat_count BIGINT;
BEGIN
    DELETE FROM rca_outcomes
        WHERE created_at < now() - (retain_outcomes_days || ' days')::INTERVAL;
    GET DIAGNOSTICS rca_count = ROW_COUNT;

    DELETE FROM failure_patterns
        WHERE last_seen_at < now() - (retain_patterns_days || ' days')::INTERVAL
          AND confidence < 0.9;
    GET DIAGNOSTICS pat_count = ROW_COUNT;

    RETURN QUERY SELECT rca_count, pat_count;
END;
$$ LANGUAGE plpgsql;

-- ── Flight recorder (V4 ADR-005) ─────────────────────────────────────────────
-- Append-only, hash-chained decision log. One row per recorded event; the
-- chain is per episode_id (== session_id under V2 instrumentation):
--   hash = sha256(prev_hash || canonical_json(episode_id, seq, kind, payload))
-- Tampering with any row breaks every subsequent hash in that episode.
CREATE TABLE IF NOT EXISTS decision_log (
    id          BIGSERIAL PRIMARY KEY,
    episode_id  TEXT        NOT NULL,
    seq         INTEGER     NOT NULL,
    kind        TEXT        NOT NULL,
    payload     JSONB       NOT NULL,
    prev_hash   TEXT        NOT NULL DEFAULT '',
    hash        TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (episode_id, seq)
);

-- ── v5 OTel-span projection columns (specs/02) ───────────────────────────────
-- Fast provenance-query projections for kind='ki_otel_span' rows. Deliberately
-- EXCLUDED from the hash canonical form {episode_id,seq,kind,payload} so every
-- pre-existing chain stays valid byte-for-byte; span identity is authoritative
-- in payload. Idempotent so the migration is a no-op on an already-migrated DB.
ALTER TABLE decision_log ADD COLUMN IF NOT EXISTS trace_id       TEXT;
ALTER TABLE decision_log ADD COLUMN IF NOT EXISTS span_id        TEXT;
ALTER TABLE decision_log ADD COLUMN IF NOT EXISTS parent_span_id TEXT;
CREATE INDEX IF NOT EXISTS idx_decision_log_trace ON decision_log (trace_id) WHERE trace_id IS NOT NULL;

-- ── V4 Hippocampus (ADR-002) ─────────────────────────────────────────────────
-- L1 episodic memory: every investigation becomes an Episode. The embedding
-- column is added at runtime only when the pgvector extension is available;
-- the baseline recall path uses pg_trgm similarity (stock Postgres).
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS episodes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_id      TEXT NOT NULL,
    namespace       TEXT,
    trigger_kind    TEXT NOT NULL,           -- detector | user_query | schedule | backfill
    trigger_detail  TEXT,
    summary         TEXT NOT NULL DEFAULT '',
    root_cause      TEXT,
    actions         JSONB NOT NULL DEFAULT '[]',
    outcome         TEXT,                    -- resolved | partial | regression | report_only
    verified        BOOLEAN,
    confidence      REAL,
    playbooks       TEXT[] NOT NULL DEFAULT '{}',
    created_by_role TEXT,
    request_id      TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_episodes_cluster_time ON episodes (cluster_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_trgm
    ON episodes USING gin ((summary || ' ' || COALESCE(root_cause, '')) gin_trgm_ops);
-- Memory V5 P0 (ADR-014): functional full-text GIN index for the hybrid recall
-- lexical channel. A FUNCTIONAL index (not a stored generated tsvector column) is
-- used deliberately — it avoids the ACCESS EXCLUSIVE table rewrite a stored column
-- would force on a large episodes table (design review S2). The expression MUST match
-- the to_tsvector(...) call in episodes.recall_episodes exactly for the index to apply.
CREATE INDEX IF NOT EXISTS idx_episodes_fts
    ON episodes USING gin (to_tsvector('english', summary || ' ' || COALESCE(root_cause, '')));

-- Memory V5 P6 (ADR-017): importance/surprise-weighted retention. `importance`
-- derives from incident severity (regression/verified/confidence) and modulates
-- RECALL RANKING only — never retention, because episodes are an audit record
-- (R6.2). `surprise` is a KG-novelty proxy computed at write time (fraction of the
-- episode unseen vs recent memory) used to gate low-value auto-writes (R6.3).
-- Idempotent, no table rewrite: ADD COLUMN IF NOT EXISTS with NULL default (old
-- rows keep NULL → recall COALESCEs them to a neutral 0.5 weight).
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS importance REAL;
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS surprise   REAL;

-- Memory V5 P7 (ADR-018): provenance TRUST on every episode — the write-admission guard
-- (R8.1) stamps a [0,1] trust derived from the write's provenance (sensor/detector = 1.0
-- ground truth; user-chat-derived = low, the MINJA surface). Low-trust user writes are
-- quarantined at admission, not silently trusted. NULL for pre-P7 rows.
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS trust REAL;

-- L2 semantic memory: temporal knowledge graph (valid-time edges).
CREATE TABLE IF NOT EXISTS kg_entities (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,               -- Workload | Pod | Node | Incident | ...
    name        TEXT NOT NULL,
    namespace   TEXT NOT NULL DEFAULT '',
    attrs       JSONB NOT NULL DEFAULT '{}',
    UNIQUE (cluster_id, kind, namespace, name)
);

CREATE TABLE IF NOT EXISTS kg_edges (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_id   TEXT NOT NULL,
    src          UUID NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    rel          TEXT NOT NULL,              -- runs_on | owns | has_status | crashed_with | fixed_by
    dst          UUID NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    attrs        JSONB NOT NULL DEFAULT '{}',
    valid_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to     TIMESTAMPTZ,                -- NULL = currently true
    source_kind  TEXT NOT NULL DEFAULT 'observation',
    source_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_kg_edges_window ON kg_edges (cluster_id, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_kg_edges_open ON kg_edges (src, rel) WHERE valid_to IS NULL;

-- Memory V5 P2 (ADR-013): make the KG bi-temporal. Existing valid_from/valid_to are
-- the EVENT-time axis (when a cluster fact held). Add the TRANSACTION-time axis
-- (ingested_at = when the agent learned it, retracted_at = when it stopped believing
-- it). The gap ingested_at − valid_from is the freshness/ingest-lag signal (LOFA-L1).
-- PG16-compatible: plain columns + indexes; native temporal tables (PG18+) NOT used.
-- Idempotent: ADD COLUMN IF NOT EXISTS + a NULL-only backfill that never rewrites
-- correctly-set values on re-run (avoids the rewrite/lock risk, review S2/F3).
ALTER TABLE kg_edges ADD COLUMN IF NOT EXISTS ingested_at  TIMESTAMPTZ;
UPDATE kg_edges SET ingested_at = valid_from WHERE ingested_at IS NULL;   -- legacy backfill
ALTER TABLE kg_edges ALTER COLUMN ingested_at SET DEFAULT now();          -- new rows stamp learn-time
ALTER TABLE kg_edges ADD COLUMN IF NOT EXISTS retracted_at TIMESTAMPTZ;   -- NULL = still believed
-- Transaction-time lookup index (mirrors the valid-time window index).
CREATE INDEX IF NOT EXISTS idx_kg_edges_tx ON kg_edges (cluster_id, ingested_at, retracted_at);
-- "Currently believed" fast path (S1 default read): valid + not-retracted.
CREATE INDEX IF NOT EXISTS idx_kg_edges_current ON kg_edges (cluster_id, src, rel)
    WHERE valid_to IS NULL AND retracted_at IS NULL;

-- Memory V5 P5 (ADR-016): the promotion middle layer. Verified, recurring episodic
-- reflections are consolidated into semantic rules (IF context → THEN guidance); a rule
-- that recurs enough becomes 'active' (injected into the prompt) and is eligible to seed a
-- detector *candidate* (which still requires human review before it reaches the watchtower).
CREATE TABLE IF NOT EXISTS semantic_rules (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_id       TEXT NOT NULL,
    context          TEXT NOT NULL,                     -- IF: trigger signature (playbook/symptom)
    guidance         TEXT NOT NULL,                     -- THEN: verified remediation guidance
    source           TEXT NOT NULL DEFAULT 'episode',   -- episode | pattern | manual
    recurrence_count INTEGER NOT NULL DEFAULT 1,
    confidence       REAL NOT NULL DEFAULT 0.5,
    status           TEXT NOT NULL DEFAULT 'candidate', -- candidate | active | demoted
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (cluster_id, context)
);
CREATE INDEX IF NOT EXISTS idx_semantic_rules_active ON semantic_rules (cluster_id, status)
    WHERE status = 'active';

-- Memory V5 P6 (ADR-017): first-class PROSPECTIVE memory — "remember to re-check
-- condition C at/after time T." The watchtower records one after an autonomous fix
-- ("did the fix hold?"); the consolidation scheduler fires due rows and records the
-- outcome (R6.4). Firing routes through the autonomy ladder (A0 namespaces never
-- fire). Idempotent per (cluster_id, dedup_key): a re-check for the same condition
-- is refreshed, not duplicated, so a flapping fix does not spawn a backlog.
CREATE TABLE IF NOT EXISTS prospective_memory (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_id        TEXT NOT NULL,
    namespace         TEXT NOT NULL DEFAULT '',
    condition         TEXT NOT NULL,                     -- human-readable re-check intent
    check_query       TEXT,                              -- optional NL/PromQL to re-run
    dedup_key         TEXT NOT NULL,                     -- collapses duplicate re-checks
    due_at            TIMESTAMPTZ NOT NULL,              -- fire at/after this instant
    status            TEXT NOT NULL DEFAULT 'pending',   -- pending | fired | done | cancelled
    outcome           TEXT,                              -- recorded when fired/resolved
    source_episode_id UUID,                              -- the fix episode that scheduled it
    created_by        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    fired_at          TIMESTAMPTZ,
    UNIQUE (cluster_id, dedup_key)
);
-- Scheduler fast path: the next pending re-check that is due.
CREATE INDEX IF NOT EXISTS idx_prospective_due ON prospective_memory (due_at)
    WHERE status = 'pending';

-- Memory V5 P8 (spec R7): a RAPTOR/GraphRAG-style SUMMARY HIERARCHY over episode clusters,
-- so the agent can answer theme-level questions ("what keeps failing in payments?") without
-- scanning every episode. `level` 1 = a theme summary (leaf cluster); higher levels reserved
-- for recursive roll-ups. `theme_key` is the deterministic cluster signature (playbook or
-- namespace). Regeneration is tied to **KG change-rate** (R7.1): a summary is rebuilt only when
-- new episodes arrived (`last_episode_at` advanced) OR the cluster's KG edge count moved
-- (`kg_watermark`), never on a fixed clock alone.
CREATE TABLE IF NOT EXISTS memory_summaries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_id      TEXT NOT NULL,
    level           INTEGER NOT NULL DEFAULT 1,        -- 1 = theme (leaf cluster) summary
    theme_key       TEXT NOT NULL,                     -- grouping signature (playbook | namespace)
    summary         TEXT NOT NULL,
    member_count    INTEGER NOT NULL DEFAULT 0,
    verified_count  INTEGER NOT NULL DEFAULT 0,
    last_episode_at TIMESTAMPTZ,                        -- newest episode covered (new-episode watermark)
    kg_watermark    BIGINT NOT NULL DEFAULT 0,          -- cluster KG edge count at last regen (change-rate)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (cluster_id, level, theme_key)
);
CREATE INDEX IF NOT EXISTS idx_memory_summaries_cluster ON memory_summaries (cluster_id, level);
CREATE INDEX IF NOT EXISTS idx_memory_summaries_trgm
    ON memory_summaries USING gin ((theme_key || ' ' || summary) gin_trgm_ops);

-- L3 procedural memory: learned detector candidates (ADR-006). Human review
-- is always required before activation.
CREATE TABLE IF NOT EXISTS detectors (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_id      TEXT NOT NULL DEFAULT 'global',
    name            TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'learned',   -- playbook | learned | nl (ADR-012)
    predicate       JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'candidate', -- candidate | shadow | active | demoted
    precision_stats JSONB NOT NULL DEFAULT '{}',
    created_from    TEXT,
    reviewed_by     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (cluster_id, name)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Memory V5 P7 (ADR-018 R8.3): per-cluster/tenant Row-Level Security scaffolding.
--
-- These policies are the DB-enforced isolation layer. They are DELIBERATELY LEFT
-- DISABLED here: enabling RLS unconditionally would make every read return zero rows
-- until the application sets the tenant GUC on every transaction, breaking the running
-- app. Enablement is the paired P7-completion step — turn it on ONLY together with
-- `app.memory.security.set_tenant_context()` issuing `SET LOCAL ki.cluster_id` inside
-- each transaction (review S3: a session-level GUC leaks across pgbouncer-pooled
-- connections, so `SET LOCAL` — transaction-scoped — is mandatory, not `SET`).
--
-- To enable (per table), run once RLS-aware connection handling is in place:
--
--   ALTER TABLE episodes  ENABLE ROW LEVEL SECURITY;
--   CREATE POLICY episodes_tenant_isolation ON episodes
--       USING (cluster_id = current_setting('ki.cluster_id', true));
--   ALTER TABLE kg_entities ENABLE ROW LEVEL SECURITY;
--   CREATE POLICY kg_entities_tenant_isolation ON kg_entities
--       USING (cluster_id = current_setting('ki.cluster_id', true));
--   -- …and analogously for kg_edges, semantic_rules, prospective_memory, detectors.
--
-- `current_setting(..., true)` returns NULL (not an error) when unset, so an
-- admin/maintenance connection that does not set the GUC still fails CLOSED (no rows),
-- never open. RTBF deletes (R8.4) run through the same tenant context.

-- Memory V5 P7 (ADR-018 R8.2): tamper-evidence for the learning write path. An append-only,
-- per-cluster SHA-256 hash chain over memory-mutating events (episode writes, quarantines,
-- forgets), computed with the SAME primitive as the flight recorder (ADR-005) so an auditor can
-- detect any silent edit/delete/reorder of learned memory. `hash = sha256(prev_hash || canonical)`.
CREATE TABLE IF NOT EXISTS memory_audit (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_id TEXT   NOT NULL,
    seq        BIGINT NOT NULL,                   -- per-cluster monotonic chain position
    kind       TEXT   NOT NULL,                   -- episode_write | quarantine | forget | edge_reconcile
    ref_id     TEXT,                              -- the affected record id, when applicable
    payload    JSONB  NOT NULL DEFAULT '{}',      -- redacted, minimal (no secrets)
    prev_hash  TEXT   NOT NULL,
    hash       TEXT   NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (cluster_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_memory_audit_chain ON memory_audit (cluster_id, seq);

-- Chain head — the anchor that makes a *truncation* visible. A hash chain detects an edit,
-- a reorder and an interior delete, because each breaks a link. Deleting the most recent
-- rows breaks nothing: what is left is a shorter, perfectly valid chain, and the next append
-- continues from it, so the deletion is invisible for ever. This row records how far the
-- chain got, so a shorter chain contradicts it. It is tamper-EVIDENCE, not prevention: an
-- attacker with full write access can forge this row too — but they must now forge two
-- places instead of one, and a partial tamper is caught.
CREATE TABLE IF NOT EXISTS memory_chain_head (
    cluster_id TEXT PRIMARY KEY,
    seq        BIGINT NOT NULL,                   -- seq of the newest appended entry
    hash       TEXT   NOT NULL,                   -- its hash
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── v5 P5 Fleet memory exchange (ADR-105) ────────────────────────────────────
-- Cross-cluster resolution sharing with STRICT tenant isolation enforced by the
-- tenant-scoped query (a read never crosses tenants). Additive; idempotent.
CREATE TABLE IF NOT EXISTS fleet_memory (
    id         BIGSERIAL PRIMARY KEY,
    tenant     TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    signature  TEXT NOT NULL,
    summary    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fleet_memory_tenant ON fleet_memory (tenant, signature);

-- ── v5 P3 Statistical-promotion outcome store (ADR-102) ──────────────────────
-- Per-action-class shadow-agreement outcomes that feed the Wilson-LCB promotion
-- engine. Purpose-built (not hash-chained — these are statistical samples, not
-- an audit ledger). Additive; idempotent.
CREATE TABLE IF NOT EXISTS promotion_outcomes (
    id            BIGSERIAL PRIMARY KEY,
    action_class  TEXT   NOT NULL,
    ts_days       DOUBLE PRECISION NOT NULL,
    success       BOOLEAN NOT NULL,
    incident_id   TEXT   NOT NULL,
    incident_type TEXT   NOT NULL DEFAULT 'generic',
    critical      BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_promotion_outcomes_class ON promotion_outcomes (action_class, ts_days);

-- ── v5 P5 fleet_memory RLS tenancy (ADR-105) — DEFINED but DISABLED ───────────
-- DB-enforced tenant isolation (defense in depth beyond the tenant-scoped query).
-- The policy keys on a per-transaction GUC (ki.tenant). Kept DISABLED because
-- enabling it requires the read path to SET LOCAL ki.tenant on every query — else
-- current_setting returns NULL and reads go empty (same gate as the P7 memory RLS).
-- Enable together with fleet_store_pg.set_tenant_context wiring on the read path.
DROP POLICY IF EXISTS fleet_tenant_isolation ON fleet_memory;
CREATE POLICY fleet_tenant_isolation ON fleet_memory
    USING (tenant = current_setting('ki.tenant', true));
-- ALTER TABLE fleet_memory ENABLE ROW LEVEL SECURITY;  -- enable with GUC binding

-- ── v5 P5 Fleet signal store (fleet-wide pattern pooling) ─────────────────────
-- Per-cluster detector/agent-runaway signals, pooled per tenant for fleet-wide
-- pattern detection. Tenant-scoped reads (isolation). Additive; idempotent.
CREATE TABLE IF NOT EXISTS fleet_signals (
    id         BIGSERIAL PRIMARY KEY,
    tenant     TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    severity   TEXT NOT NULL DEFAULT 'warning',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fleet_signals_tenant ON fleet_signals (tenant, created_at);
