"""Live fleet-store probe (v5 P5) — durable cross-cluster sharing + tenant isolation on real PG.

Publishes resolutions for two tenants to real Postgres, then verifies each tenant's read returns
ONLY its own rows — the security-critical isolation invariant, proven against a live database (not
just an in-process store). Cleans up its test rows.

Run on n1:  DATABASE_URL=postgresql://... uv run python scripts/fleet_store_probe.py
"""
from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

from app.memory.fleet_exchange import FleetEntry
from app.memory.fleet_store_pg import publish, read_fleet

FAIL: list[str] = []
T1, T2 = "probe-acme", "probe-globex"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


async def main() -> int:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3)
    await pool.execute(
        "CREATE TABLE IF NOT EXISTS fleet_memory (id BIGSERIAL PRIMARY KEY, tenant TEXT NOT NULL, "
        "cluster_id TEXT NOT NULL, signature TEXT NOT NULL, summary TEXT NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    # clean slate for the probe tenants
    await pool.execute("DELETE FROM fleet_memory WHERE tenant = ANY($1)", [T1, T2])

    await publish(pool, FleetEntry(T1, "cl-a1", "OOM|payments", "raise memory limit"))
    await publish(pool, FleetEntry(T1, "cl-a2", "CrashLoop|web", "fix probe path"))
    await publish(pool, FleetEntry(T2, "cl-g1", "OOM|payments", "globex-only fix"))
    check("published to real Postgres", True)

    acme = await read_fleet(pool, T1)
    globex = await read_fleet(pool, T2)
    check("tenant read returns its own rows", len(acme) == 2 and len(globex) == 1,
          f"acme={len(acme)} globex={len(globex)}")
    check("STRICT isolation: acme never sees globex", all(e.tenant == T1 for e in acme))
    check("STRICT isolation: globex never sees acme", all(e.tenant == T2 for e in globex))

    # even a shared signature stays tenant-scoped
    shared = await read_fleet(pool, T1, signature="OOM|payments")
    check("shared signature is still tenant-scoped", len(shared) == 1 and shared[0].cluster_id == "cl-a1")

    excl = await read_fleet(pool, T1, exclude_cluster="cl-a1")
    check("exclude_cluster drops own cluster", [e.cluster_id for e in excl] == ["cl-a2"])

    await pool.execute("DELETE FROM fleet_memory WHERE tenant = ANY($1)", [T1, T2])
    await pool.close()
    print(f"\n==== {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)} ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
