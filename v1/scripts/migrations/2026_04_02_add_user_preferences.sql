-- Migration: user_preferences table
-- Date: 2026-04-02
-- Purpose: Persistent per-user preference store for verbosity, response format,
--          default namespace/cluster, and remediation style.
--          Populated by detection heuristics and explicit user instructions.
--          Used by MemoryOrchestrator to inject a [User preferences: ...] hint
--          at the start of every workflow run.

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id          TEXT        NOT NULL,
    preference_key   TEXT        NOT NULL,
    preference_value TEXT        NOT NULL,
    confidence       FLOAT       NOT NULL DEFAULT 0.5,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- One row per user per key.  Upserts check confidence before overwriting.
    PRIMARY KEY (user_id, preference_key)
);

CREATE INDEX IF NOT EXISTS idx_user_preferences_user_id
    ON user_preferences (user_id);
