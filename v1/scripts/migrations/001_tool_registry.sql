-- Migration 001: tool_registry table
--
-- Migrates tool metadata from the JSON/fcntl registry (registry.json on PVC)
-- to PostgreSQL. ToolRegistryService now reads/writes this table exclusively.
--
-- Run against the kubeintellect Postgres instance:
--   kubectl exec -n kubeintellect deploy/kubeintellect-postgres -- \
--     psql -U kubeintellect -d kubeintellect -f /tmp/001_tool_registry.sql
--
-- Safe to run multiple times (all statements are idempotent).

CREATE TABLE IF NOT EXISTS tool_registry (
    tool_id                     TEXT PRIMARY KEY,
    name                        TEXT UNIQUE NOT NULL,
    description                 TEXT    DEFAULT '',
    file_path                   TEXT    NOT NULL,
    file_checksum               TEXT,
    function_name               TEXT    NOT NULL,
    pydantic_class_name         TEXT,
    tool_instance_variable_name TEXT    NOT NULL,
    input_schema                TEXT    DEFAULT '{}',
    output_schema               TEXT    DEFAULT '{}',
    created_at                  TIMESTAMP NOT NULL DEFAULT NOW(),
    base_app_version            TEXT    DEFAULT 'unknown',
    status                      TEXT    NOT NULL DEFAULT 'enabled',
    status_reason               TEXT,
    created_by                  TEXT    DEFAULT 'runtime',
    pr_url                      TEXT,
    pr_number                   INTEGER,
    pr_status                   TEXT
);

-- Index for the most common lookup patterns
CREATE INDEX IF NOT EXISTS idx_tool_registry_name   ON tool_registry (name);
CREATE INDEX IF NOT EXISTS idx_tool_registry_status ON tool_registry (status);
