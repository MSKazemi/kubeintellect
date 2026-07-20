"""Postgres-backed fleet-signal store (v5 P5) — record/read/detect wiring, tenant-scoped."""
from __future__ import annotations

from app.memory.fleet_signal_store import detect_from_store, record_signal
from app.memory.fleet_signals import FleetSignal


class _FakePool:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append(("exec", sql, args))

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self.rows


def _row(cluster, kind="agent-runaway", severity="warning"):
    return {"tenant": "acme", "cluster_id": cluster, "kind": kind, "severity": severity}


class TestRecord:
    async def test_inserts_fields(self):
        pool = _FakePool()
        await record_signal(pool, FleetSignal("acme", "c1", "agent-runaway", "critical"))
        _, sql, args = pool.calls[0]
        assert "INSERT INTO fleet_signals" in sql and args == ("acme", "c1", "agent-runaway", "critical")


class TestDetect:
    async def test_reads_tenant_scoped(self):
        pool = _FakePool(rows=[])
        await detect_from_store(pool, "acme", min_clusters=3)
        _, sql, args = pool.calls[0]
        assert "tenant = $1" in sql and args[0] == "acme"

    async def test_fleet_pattern_from_store(self):
        rows = [_row("c1"), _row("c2"), _row("c3")]
        alerts = await detect_from_store(_FakePool(rows=rows), "acme", min_clusters=3)
        assert len(alerts) == 1 and alerts[0].cluster_count == 3

    async def test_below_threshold_no_alert(self):
        alerts = await detect_from_store(_FakePool(rows=[_row("c1"), _row("c2")]), "acme", min_clusters=3)
        assert alerts == []
