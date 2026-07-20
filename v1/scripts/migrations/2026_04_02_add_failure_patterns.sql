-- Migration: failure_patterns table
-- Date: 2026-04-02
-- Purpose: Persistent store for Kubernetes failure patterns used by the
--          pre-query hint injection hook (3D-5).  v1 uses keyword matching
--          via a GENERATED signals_text column — no pgvector dependency.

CREATE TABLE IF NOT EXISTS failure_patterns (
    pattern_id          TEXT PRIMARY KEY,
    type                TEXT        NOT NULL,
    -- stored as a native Postgres array so array_to_string works in the
    -- generated column and individual signals can be indexed later
    signals             TEXT[]      NOT NULL,
    root_cause          TEXT        NOT NULL,
    recommended_checks  TEXT[]      NOT NULL,
    remediation_steps   TEXT[]      NOT NULL,
    confidence          FLOAT       NOT NULL DEFAULT 0.8,
    times_seen          INTEGER     NOT NULL DEFAULT 0,
    last_seen           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verified            BOOLEAN     NOT NULL DEFAULT FALSE,
    namespace_scope     TEXT,                              -- NULL = cluster-wide
    -- Searchable concatenation of all signals, populated at INSERT time by the
    -- application layer (' '.join(signals)).  GENERATED ALWAYS AS was removed
    -- because array_to_string fails the immutability check on some Postgres builds.
    signals_text        TEXT        NOT NULL DEFAULT ''
);

-- GIN index on signals array for future containment queries (@>, &&)
CREATE INDEX IF NOT EXISTS idx_failure_patterns_signals
    ON failure_patterns USING GIN (signals);

-- Full-text index on signals_text for fast keyword matching
CREATE INDEX IF NOT EXISTS idx_failure_patterns_signals_text
    ON failure_patterns USING GIN (to_tsvector('english', signals_text));

-- Type index for filtering by failure category
CREATE INDEX IF NOT EXISTS idx_failure_patterns_type
    ON failure_patterns (type);
