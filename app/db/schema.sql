-- KubeIntellect V2 database schema
-- Run once: psql $POSTGRES_DSN -f app/db/schema.sql

-- ── User preferences ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);

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
