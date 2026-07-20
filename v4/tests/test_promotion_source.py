"""Postgres-backed promotion outcome store (v5 P3, ADR-102) — record/read/decide wiring."""
from __future__ import annotations

from app.autonomy.promotion_source import (
    decide_from_store,
    outcomes_from_store,
    record_outcome,
)
from app.autonomy.promotion_stats import Event


class _FakePool:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows


def _row(ts, success=True, inc="i0", typ="t0", crit=False):
    return {"ts_days": ts, "success": success, "incident_id": inc, "incident_type": typ, "critical": crit}


class TestRecord:
    async def test_inserts_fields(self):
        pool = _FakePool()
        await record_outcome(pool, "misconfig-fix", Event(5.0, True, "inc1", "typeA", False))
        sql, args = pool.calls[0]
        assert "INSERT INTO promotion_outcomes" in sql
        assert args == ("misconfig-fix", 5.0, True, "inc1", "typeA", False)


class TestRead:
    async def test_maps_rows_to_events_chronological(self):
        pool = _FakePool(rows=[_row(3.0), _row(2.0), _row(1.0)])   # DESC from DB
        events = await outcomes_from_store(pool, "c")
        assert [e.ts_days for e in events] == [1.0, 2.0, 3.0]       # reversed to chronological
        assert all(isinstance(e, Event) for e in events)

    async def test_query_is_class_scoped(self):
        pool = _FakePool()
        await outcomes_from_store(pool, "misconfig-fix")
        sql, args = pool.calls[0]
        assert "action_class = $1" in sql and args[0] == "misconfig-fix"


class TestDecideFromStore:
    async def test_promotes_on_strong_recorded_record(self):
        # 60 all-success across 6 incidents / 10 days ⇒ engine promotes L1->L2
        rows = [_row(i / 59 * 10, True, f"inc{i % 6}", "t0") for i in range(60)]
        pool = _FakePool(rows=list(reversed(rows)))   # DB returns DESC
        d = await decide_from_store(pool, "misconfig-fix", "L1->L2", "L1", now_days=10.0)
        assert d.action == "promote" and d.to_rung == "L2"

    async def test_empty_store_holds(self):
        d = await decide_from_store(_FakePool(rows=[]), "c", "L1->L2", "L1", now_days=10.0)
        assert d.action == "hold"
