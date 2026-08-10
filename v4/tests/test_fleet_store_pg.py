"""Postgres-backed fleet store (v5 P5) — query construction + tenant-isolation predicate."""
from __future__ import annotations

from app.memory.fleet_exchange import FleetEntry
from app.memory.fleet_store_pg import publish, read_fleet


class _FakePool:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows


class TestPublish:
    async def test_inserts_all_fields(self):
        pool = _FakePool()
        await publish(pool, FleetEntry("acme", "cl-1", "OOM|payments", "raise limit"))
        sql, args = pool.calls[0]
        assert "INSERT INTO fleet_memory" in sql
        assert args == ("acme", "cl-1", "OOM|payments", "raise limit")


class TestReadIsolation:
    async def test_tenant_predicate_always_present(self):
        pool = _FakePool()
        await read_fleet(pool, "acme")
        sql, args = pool.calls[0]
        assert "tenant = $1" in sql and args[0] == "acme"

    async def test_exclude_cluster_adds_clause(self):
        pool = _FakePool()
        await read_fleet(pool, "acme", exclude_cluster="cl-1")
        sql, args = pool.calls[0]
        assert "cluster_id <> $2" in sql and args[1] == "cl-1"

    async def test_signature_filter_adds_clause(self):
        pool = _FakePool()
        await read_fleet(pool, "acme", signature="OOM|payments")
        sql, args = pool.calls[0]
        assert "signature = $2" in sql and args[1] == "OOM|payments"

    async def test_maps_rows_to_entries(self):
        pool = _FakePool(rows=[
            {"tenant": "acme", "cluster_id": "cl-1", "signature": "s", "summary": "fix"},
        ])
        out = await read_fleet(pool, "acme")
        assert len(out) == 1 and isinstance(out[0], FleetEntry)
        assert out[0].cluster_id == "cl-1" and out[0].tenant == "acme"

    async def test_limit_is_last_param(self):
        pool = _FakePool()
        await read_fleet(pool, "acme", exclude_cluster="c", signature="s", limit=50)
        sql, args = pool.calls[0]
        assert "LIMIT $4" in sql and args[-1] == 50


class TestTenantContext:
    async def test_set_tenant_context_binds_guc(self):
        from app.memory.fleet_store_pg import set_tenant_context
        conn = _FakePool()
        await set_tenant_context(conn, "acme")
        sql, args = conn.calls[0]
        assert "set_config" in sql and "ki.tenant" in sql and args[0] == "acme"
        assert "true" in sql        # transaction-local (SET LOCAL semantics), literal in the SQL
