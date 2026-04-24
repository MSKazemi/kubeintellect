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
    outcome_feedback TEXT,                     -- "resolved" | "incorrect" | NULL
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rca_outcomes_user_id ON rca_outcomes (user_id, created_at DESC);

-- ── Failure patterns (auto-seeded, confidence ≥0.9 AND occurrence ≥2) ────────
CREATE TABLE IF NOT EXISTS failure_patterns (
    pattern_name     TEXT    PRIMARY KEY,
    description      TEXT    NOT NULL,
    recommended_fix  TEXT    NOT NULL,
    confidence       FLOAT   NOT NULL DEFAULT 0.0,
    occurrence_count INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
