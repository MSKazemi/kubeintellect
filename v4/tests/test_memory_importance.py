"""Memory V5 P6 (ADR-017) — importance/surprise-weighted retention.

A fake pool is patched onto the episodes module (it reads its own `_pool`). SQL is validated
against a live DB at the gate; here we assert the scoring heuristics, the surprise write-gate,
and that the flag swaps recall to the importance-weighted ORDER BY.
"""
from __future__ import annotations

from app.memory import episodes


class FakePool:
    def __init__(self, fetchrow=None, surprise_top=None, fetch=None):
        self._fetchrow = fetchrow
        self._surprise_top = surprise_top
        self._fetch = fetch or []
        self.calls: list[tuple] = []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        if "max(similarity" in sql:                 # the surprise-novelty query
            return {"top": self._surprise_top}
        return self._fetchrow

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self._fetch


class TestImportanceScore:
    def test_severity_ordering(self):
        s = episodes._importance_score
        assert s("regression", False, None) > s("partial", False, None)
        assert s("partial", False, None) > s("resolved", False, None)
        assert s("resolved", False, None) > s("report_only", False, None)

    def test_verified_and_confidence_boost(self):
        s = episodes._importance_score
        assert s("resolved", True, None) > s("resolved", False, None)
        assert s("resolved", False, 1.0) > s("resolved", False, 0.0)

    def test_bounded_unit_interval(self):
        s = episodes._importance_score
        assert s("regression", True, 1.0) <= 1.0
        assert s("unknown-outcome", None, None) >= 0.0

    def test_low_value_only_unverified_reportonly(self):
        assert episodes._is_low_value(False, "report_only")
        assert episodes._is_low_value(False, "")
        assert episodes._is_low_value(False, None)
        assert not episodes._is_low_value(True, "report_only")     # verified is kept
        assert not episodes._is_low_value(False, "regression")     # actioned is kept


class TestImportanceWeightedRecallSQL:
    def test_trgm_variant_weights_by_importance(self):
        # ⚠️ This used to assert the literal `"sim * (0.5 + 0.5 * COALESCE(importance, 0.5))"`,
        # which pinned the MECHANISM rather than the property — and the mechanism it pinned was
        # a query Postgres rejects outright (`sim` is a SELECT-list alias, illegal inside an
        # ORDER BY expression), so this test stayed green while the arm it covers could not
        # recall anything at all. See test_the_importance_ranked_recall_query_never_ran.py.
        s = episodes._SQL_RECALL_TRGM_IMP
        assert "* (0.5 + 0.5 * COALESCE(importance, 0.5))" in s          # relevance is weighted
        assert "ORDER BY sim *" not in s                                 # ...but not via the alias
        assert "COALESCE(importance" not in episodes._SQL_RECALL_TRGM   # flat variant untouched

    def test_hybrid_variant_threads_importance(self):
        s = episodes._SQL_RECALL_HYBRID_IMP
        assert "f.rrf, e.importance" in s                              # selected
        assert "f.rrf * (0.5 + 0.5 * COALESCE(e.importance, 0.5))" in s  # ordered

    async def test_flag_on_selects_weighted_sql(self, mocker):
        pool = FakePool(fetch=[])
        episodes.init_episodes(pool)
        mocker.patch.object(episodes.settings, "MEMORY_HYBRID_RETRIEVAL", False)
        mocker.patch.object(episodes.settings, "MEMORY_IMPORTANCE", True)
        await episodes.recall_episodes("crashloop", "c1", k=3)
        episodes.close_episodes()
        sql = pool.calls[-1][1]
        assert "COALESCE(importance, 0.5)" in sql

    async def test_flag_off_uses_flat_sql(self, mocker):
        pool = FakePool(fetch=[])
        episodes.init_episodes(pool)
        mocker.patch.object(episodes.settings, "MEMORY_HYBRID_RETRIEVAL", False)
        mocker.patch.object(episodes.settings, "MEMORY_IMPORTANCE", False)
        await episodes.recall_episodes("crashloop", "c1", k=3)
        episodes.close_episodes()
        assert "COALESCE(importance" not in pool.calls[-1][1]


class TestSurpriseGate:
    async def test_flag_off_writes_without_scoring(self, mocker):
        pool = FakePool(fetchrow={"id": "e1"})
        episodes.init_episodes(pool)
        mocker.patch.object(episodes.settings, "MEMORY_IMPORTANCE", False)
        eid = await episodes.write_episode(
            cluster_id="c1", trigger_kind="user_query", summary="x", outcome="report_only"
        )
        episodes.close_episodes()
        assert eid == "e1"
        # No surprise-novelty query was issued; importance/surprise/trust inserted as NULL.
        assert not any("max(similarity" in c[1] for c in pool.calls)
        insert = next(c for c in pool.calls if "INSERT INTO episodes" in c[1])
        assert insert[2][-3:] == (None, None, None)                   # importance, surprise, trust

    async def test_gate_drops_redundant_low_value_write(self, mocker):
        # top similarity 0.95 → surprise 0.05 < floor, and report_only+unverified is low-value.
        pool = FakePool(fetchrow={"id": "e1"}, surprise_top=0.95)
        episodes.init_episodes(pool)
        mocker.patch.object(episodes.settings, "MEMORY_IMPORTANCE", True)
        eid = await episodes.write_episode(
            cluster_id="c1", trigger_kind="user_query",
            summary="pod restarted again", outcome="report_only", verified=False,
        )
        episodes.close_episodes()
        assert eid is None
        assert not any("INSERT INTO episodes" in c[1] for c in pool.calls)  # never inserted

    async def test_gate_keeps_verified_even_if_redundant(self, mocker):
        pool = FakePool(fetchrow={"id": "e1"}, surprise_top=0.99)
        episodes.init_episodes(pool)
        mocker.patch.object(episodes.settings, "MEMORY_IMPORTANCE", True)
        eid = await episodes.write_episode(
            cluster_id="c1", trigger_kind="detector",
            summary="oomkilled", outcome="resolved", verified=True,
        )
        episodes.close_episodes()
        assert eid == "e1"
        insert = next(c for c in pool.calls if "INSERT INTO episodes" in c[1])
        importance, surprise, _trust = insert[2][-3:]
        assert importance is not None and 0.0 <= surprise <= 1.0       # scored + stored

    async def test_novel_low_value_write_is_kept(self, mocker):
        pool = FakePool(fetchrow={"id": "e1"}, surprise_top=0.0)       # nothing similar
        episodes.init_episodes(pool)
        mocker.patch.object(episodes.settings, "MEMORY_IMPORTANCE", True)
        eid = await episodes.write_episode(
            cluster_id="c1", trigger_kind="user_query",
            summary="a brand new symptom", outcome="report_only", verified=False,
        )
        episodes.close_episodes()
        assert eid == "e1"                                             # surprise 1.0 ≥ floor

    async def test_write_never_raises_on_db_error(self, mocker):
        class Boom:
            async def fetchrow(self, *a):
                raise RuntimeError("db down")
        episodes.init_episodes(Boom())
        mocker.patch.object(episodes.settings, "MEMORY_IMPORTANCE", True)
        assert await episodes.write_episode(
            cluster_id="c1", trigger_kind="user_query", summary="x"
        ) is None
        episodes.close_episodes()
