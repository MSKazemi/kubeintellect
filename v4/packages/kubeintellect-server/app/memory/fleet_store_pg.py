"""Postgres-backed fleet memory store (v5 P5 Fleet HQ, ADR-105 durability).

The durable backing for the in-process `fleet_exchange`: cross-cluster resolutions persist in the
`fleet_memory` table and survive restarts, while the load-bearing invariant — **strict tenant
isolation** — is enforced by the tenant-scoped WHERE clause on every read (a query NEVER returns
rows from another tenant). Same contract as the in-process store; the difference is durability.

Async over asyncpg; the pool is injected so this composes with the app's existing connection.
"""

from __future__ import annotations

from typing import Any, Optional

from app.memory.fleet_exchange import FleetEntry


async def set_tenant_context(conn: Any, tenant: str) -> None:
    """Bind the per-transaction RLS GUC (ADR-105). Call inside a transaction before reads once the
    `fleet_tenant_isolation` policy is ENABLED — then even a bug in the query can't cross tenants.
    pgbouncer-safe (SET LOCAL is transaction-scoped)."""
    await conn.execute("SELECT set_config('ki.tenant', $1, true)", tenant)


async def publish(pool: Any, entry: FleetEntry) -> None:
    """Persist a resolution to its tenant's fleet knowledge."""
    await pool.execute(
        "INSERT INTO fleet_memory (tenant, cluster_id, signature, summary) VALUES ($1, $2, $3, $4)",
        entry.tenant, entry.cluster_id, entry.signature, entry.summary,
    )


async def read_fleet(
    pool: Any, tenant: str, *, exclude_cluster: Optional[str] = None,
    signature: Optional[str] = None, limit: int = 200,
) -> list[FleetEntry]:
    """Read a tenant's fleet knowledge. The ``tenant = $1`` predicate is the isolation boundary —
    no query path returns another tenant's rows."""
    clauses = ["tenant = $1"]
    args: list[Any] = [tenant]
    if exclude_cluster is not None:
        args.append(exclude_cluster)
        clauses.append(f"cluster_id <> ${len(args)}")
    if signature is not None:
        args.append(signature)
        clauses.append(f"signature = ${len(args)}")
    args.append(limit)
    rows = await pool.fetch(
        f"SELECT tenant, cluster_id, signature, summary FROM fleet_memory "
        f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ${len(args)}",
        *args,
    )
    return [FleetEntry(tenant=r["tenant"], cluster_id=r["cluster_id"],
                       signature=r["signature"], summary=r["summary"]) for r in rows]
