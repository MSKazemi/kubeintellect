#!/usr/bin/env python3
"""
Migration 002: import existing registry.json into the tool_registry Postgres table.

Run this ONCE on any deployment that already has a populated registry.json on
the runtime-tools PVC.  It is safe to run multiple times — existing rows are
skipped (INSERT ... ON CONFLICT DO NOTHING).

Usage (inside the kubeintellect-core pod, or via kubectl exec):
    python scripts/migrations/002_import_registry_json.py

Environment variables (same ones the app uses):
    POSTGRES_HOST, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD
    TOOL_REGISTRY_FILE   (default: /mnt/runtime-tools/registry.json)
"""

import json
import os
import sys
from datetime import datetime


def main():
    registry_path = os.environ.get("TOOL_REGISTRY_FILE", "/mnt/runtime-tools/registry.json")

    if not os.path.exists(registry_path):
        print(f"No registry.json found at {registry_path} — nothing to migrate.")
        return

    with open(registry_path) as f:
        data = json.load(f)

    tools = data.get("tools", {})
    if not tools:
        print("registry.json is empty — nothing to migrate.")
        return

    print(f"Found {len(tools)} tool(s) in {registry_path}")

    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 not installed — run: pip install psycopg2-binary")

    # Use the same defaults as app/core/config.py so the script works inside
    # the pod without needing all env vars explicitly set.
    conn = psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        dbname=os.environ.get("POSTGRES_DB", "kubeintellectdb"),
        user=os.environ.get("POSTGRES_USER", "kubeuser"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        connect_timeout=5,
    )
    conn.autocommit = True

    inserted = skipped = 0

    with conn.cursor() as cur:
        # Ensure table exists (idempotent)
        cur.execute("""
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
        """)

        for tool_id, t in tools.items():
            created_at_raw = t.get("created_at", datetime.utcnow().isoformat())
            # Parse ISO string → datetime for Postgres
            try:
                created_at = datetime.fromisoformat(created_at_raw)
            except (ValueError, TypeError):
                created_at = datetime.utcnow()

            cur.execute(
                """
                INSERT INTO tool_registry (
                    tool_id, name, description, file_path, file_checksum,
                    function_name, pydantic_class_name, tool_instance_variable_name,
                    input_schema, output_schema, created_at, base_app_version,
                    status, status_reason, created_by,
                    pr_url, pr_number, pr_status
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (tool_id) DO NOTHING
                """,
                (
                    tool_id,
                    t["name"],
                    t.get("description", ""),
                    t["file_path"],
                    t.get("file_checksum"),
                    t["function_name"],
                    t.get("pydantic_class_name"),
                    t["tool_instance_variable_name"],
                    json.dumps(t.get("input_schema", {})),
                    json.dumps(t.get("output_schema", {})),
                    created_at,
                    t.get("base_app_version", "unknown"),
                    t.get("status", "enabled"),
                    t.get("status_reason"),
                    t.get("created_by", "runtime"),
                    t.get("pr_url"),
                    t.get("pr_number"),
                    t.get("pr_status"),
                ),
            )
            if cur.rowcount:
                inserted += 1
                print(f"  ✓ imported  {t['name']} ({tool_id})")
            else:
                skipped += 1
                print(f"  - skipped   {t['name']} ({tool_id}) — already in Postgres")

    conn.close()
    print(f"\nDone: {inserted} inserted, {skipped} skipped.")

    if inserted:
        print(
            "\nNOTE: registry.json is no longer used by the app. You may delete it\n"
            f"from the PVC once you have confirmed the Postgres data looks correct:\n"
            f"  kubectl exec -n kubeintellect <pod> -- rm {registry_path}"
        )


if __name__ == "__main__":
    main()
