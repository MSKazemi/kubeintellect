"""Memory V5 P5 (ADR-016) — promotion pipeline: episode → semantic rule.

Uses a fake pool patched onto the memory service (the module reads `service._pool`, like the
consolidation worker). SQL is validated separately (sqlglot) and against a live DB at the gate.
"""
from __future__ import annotations

from app.memory import promotion, service


class FakePool:
    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row
        self.calls: list[tuple] = []
        self.raise_on = None

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        if self.raise_on == "fetch":
            raise RuntimeError("db down")
        return self.rows

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        if self.raise_on == "fetchrow":
            raise RuntimeError("db down")
        return self.row


class TestRecordRule:
    async def test_returns_id(self, mocker):
        pool = FakePool(row={"id": "r1", "status": "active", "recurrence_count": 2})
        mocker.patch.object(service, "_pool", pool)
        rid = await promotion.record_rule("c1", "CrashLoopBackOff", "roll back the bad image")
        assert rid == "r1"
        _, sql, args = pool.calls[0]
        assert "ON CONFLICT (cluster_id, context)" in sql  # upsert / recurrence bump
        assert args[:3] == ("c1", "CrashLoopBackOff", "roll back the bad image")

    async def test_skips_empty_context_or_guidance(self, mocker):
        pool = FakePool(row={"id": "r1"})
        mocker.patch.object(service, "_pool", pool)
        assert await promotion.record_rule("c1", "", "x") is None
        assert await promotion.record_rule("c1", "ctx", "  ") is None
        assert pool.calls == []  # nothing written

    async def test_never_raises(self, mocker):
        pool = FakePool()
        pool.raise_on = "fetchrow"
        mocker.patch.object(service, "_pool", pool)
        assert await promotion.record_rule("c1", "ctx", "guidance") is None


class TestPromoteFromEpisodes:
    async def test_flag_off_is_noop(self, mocker):
        mocker.patch.object(promotion.settings, "MEMORY_PROMOTION", False)
        pool = FakePool(rows=[{"cluster_id": "c1", "ctx": "X", "n": 3, "latest_rc": "rc"}])
        mocker.patch.object(service, "_pool", pool)
        assert await promotion.promote_from_episodes() == 0
        assert pool.calls == []  # gated before any query

    async def test_promotes_recurring_verified_signatures(self, mocker):
        mocker.patch.object(promotion.settings, "MEMORY_PROMOTION", True)
        groups = [
            {"cluster_id": "c1", "ctx": "CrashLoopBackOff", "n": 3, "latest_rc": "bad image tag"},
            {"cluster_id": "c1", "ctx": "OOMKilled", "n": 2, "latest_rc": "raise memory limit"},
        ]
        # fetch → groups; each record_rule → fetchrow returns an id
        pool = FakePool(rows=groups, row={"id": "r", "status": "active", "recurrence_count": 2})
        mocker.patch.object(service, "_pool", pool)
        assert await promotion.promote_from_episodes(min_recurrence=2) == 2
        # the grouping query enforces verified + recurrence
        _, sql, args = pool.calls[0]
        assert "verified = TRUE" in sql and "HAVING count(*) >= $1" in sql and args == (2,)

    async def test_missing_root_cause_falls_back(self, mocker):
        mocker.patch.object(promotion.settings, "MEMORY_PROMOTION", True)
        pool = FakePool(
            rows=[{"cluster_id": "c1", "ctx": "X", "n": 2, "latest_rc": None}],
            row={"id": "r", "status": "active", "recurrence_count": 2},
        )
        mocker.patch.object(service, "_pool", pool)
        assert await promotion.promote_from_episodes() == 1
        upsert = next(c for c in pool.calls if c[0] == "fetchrow")
        assert upsert[2][2].startswith("(verified fix")  # guidance fallback

    async def test_never_raises(self, mocker):
        mocker.patch.object(promotion.settings, "MEMORY_PROMOTION", True)
        pool = FakePool()
        pool.raise_on = "fetch"
        mocker.patch.object(service, "_pool", pool)
        assert await promotion.promote_from_episodes() == 0


class TestActiveRulesAndRender:
    async def test_active_rules_returns_dicts(self, mocker):
        rows = [{"context": "CrashLoopBackOff", "guidance": "roll back",
                 "recurrence_count": 4, "confidence": 0.9}]
        pool = FakePool(rows=rows)
        mocker.patch.object(service, "_pool", pool)
        out = await promotion.active_rules("c1")
        assert out[0]["context"] == "CrashLoopBackOff"
        _, sql, _ = pool.calls[0]
        assert "status = 'active'" in sql

    def test_render_rules_block(self):
        block = promotion.render_rules_block([
            {"context": "CrashLoopBackOff", "guidance": "roll back the bad image",
             "recurrence_count": 4},
        ])
        assert "Learned rules" in block and "IF CrashLoopBackOff THEN" in block and "×4" in block

    def test_render_empty_is_blank(self):
        assert promotion.render_rules_block([]) == ""
