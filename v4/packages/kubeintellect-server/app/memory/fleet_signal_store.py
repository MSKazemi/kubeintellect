"""Postgres-backed fleet-signal store (v5 P5 activation).

Activates fleet-wide signal pooling: clusters record their detector/agent-runaway signals here, and
``detect_from_store`` reads a tenant's recent signals and runs the pure ``fleet_signals`` pattern
detector. This is the collector that turns per-cluster detections into a fleet incident once enough
distinct clusters show the same pattern. Tenant-scoped reads (isolation); async over asyncpg.
"""

from __future__ import annotations

from typing import Any

from app.memory.fleet_signals import FleetAlert, FleetSignal, detect_fleet_patterns


async def record_signal(pool: Any, sig: FleetSignal) -> None:
    """Persist one cluster's signal into its tenant's fleet-signal stream."""
    await pool.execute(
        "INSERT INTO fleet_signals (tenant, cluster_id, kind, severity) VALUES ($1, $2, $3, $4)",
        sig.tenant, sig.cluster_id, sig.kind, sig.severity,
    )


async def recent_signals(pool: Any, tenant: str, *, window_seconds: float = 3600.0) -> list[FleetSignal]:
    """A tenant's signals within the recent window (tenant-scoped — the isolation boundary)."""
    rows = await pool.fetch(
        "SELECT tenant, cluster_id, kind, severity FROM fleet_signals "
        "WHERE tenant = $1 AND created_at >= now() - make_interval(secs => $2)",
        tenant, window_seconds,
    )
    return [FleetSignal(tenant=r["tenant"], cluster_id=r["cluster_id"],
                        kind=r["kind"], severity=r["severity"]) for r in rows]


async def detect_from_store(
    pool: Any, tenant: str, *, min_clusters: int = 3, window_seconds: float = 3600.0,
) -> list[FleetAlert]:
    """Read a tenant's recent signals and raise fleet alerts for cross-cluster patterns."""
    signals = await recent_signals(pool, tenant, window_seconds=window_seconds)
    return detect_fleet_patterns(signals, min_clusters=min_clusters)
