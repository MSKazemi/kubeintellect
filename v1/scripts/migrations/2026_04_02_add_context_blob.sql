-- Migration: 2026-04-02
-- Add context_blob column to conversation_context table.
--
-- context_json  (existing) — lightweight scalars: namespace, resource_name,
--                            resource_type, last_tool. Used for namespace pinning.
-- context_blob  (new)      — richer working context: last_action (str),
--                            confirmed_facts (str[]). Used for investigation continuity.
--
-- Safe to re-run: ADD COLUMN IF NOT EXISTS is idempotent.

ALTER TABLE conversation_context
    ADD COLUMN IF NOT EXISTS context_blob JSONB NOT NULL DEFAULT '{}';
