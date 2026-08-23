"""Postgres-backed fleet-signal store (v5 P5 activation).

Activates fleet-wide signal pooling: clusters record their detector/agent-runaway signals here, and
``detect_from_store`` reads a tenant's recent signals and runs the pure ``fleet_signals`` pattern
detector. This is the collector that turns per-cluster detections into a fleet incident once enough
distinct clusters show the same pattern. Tenant-scoped reads (isolation); async over asyncpg.

⚠️ **Reachability, audited 2026-08-19: NOT WIRED.** Nothing in ``app/`` imports this module — the
only importers are its own unit tests. Read the present tense above as the design contract, not as a
description of a running system: no cluster records a signal here today, because no caller exists.
The whole fleet tier is a closed island (``fleet_store_pg`` → ``fleet_exchange``,
``fleet_signal_store`` → ``fleet_signals``, and nothing → either root), and its flags
(``KI_V5_FLEET_EXCHANGE``, ``KI_V5_FLEET_SIGNAL_POOLING``, ``KI_V5_FLEET_PATTERN_MIN_CLUSTERS``) are
read by no code — see ``tests/test_v5_flag_wiring.py``. The code is tested and believed correct; it
is simply never executed in production. Wiring it is a v5 P5 task, not a bug fix.
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
