"""Memory V5 P8 (spec R7) — summary hierarchy: theme summaries over episode clusters.

A fake pool is patched onto the memory service (the module reads `service._pool`, like the
consolidation worker). SQL is validated against a live DB at the gate; here we assert the
change-rate-tied rebuild, deterministic rendering, flag gating, and theme recall.
"""
from __future__ import annotations

from app.memory import service, summaries


class FakePool:
    def __init__(self, fetch_results=None, fetchrow_result="ANY"):
        # fetch_results: list consumed FIFO per fetch() call.
        self._fetch = list(fetch_results or [])
        self._fetchrow_result = fetchrow_result
        self.calls: list[tuple] = []

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self._fetch.pop(0) if self._fetch else []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        # "ANY" sentinel = a row (upsert wrote); None = ON CONFLICT WHERE suppressed it.
        return {"id": "s1"} if self._fetchrow_result == "ANY" else self._fetchrow_result


class TestRenderSummary:
    def test_includes_counts_outcomes_rootcauses(self):
        s = summaries._render_summary(
            "CrashLoopBackOff", 5, 3, ["resolved", "regression"], ["bad image tag", "OOM"]
        )
        assert "CrashLoopBackOff" in s and "5 episodes (3 verified)" in s
        assert "resolved" in s and "regression" in s
        assert "bad image tag" in s

    def test_handles_empty_aggregates(self):
        s = summaries._render_summary("general", 3, 0, None, None)
        assert "general" in s and "3 episodes (0 verified)" in s

    def test_bounded_length(self):
        s = summaries._render_summary("t", 9, 9, ["x"], ["y" * 500])
        assert len(s) <= 1500


class TestBuildSummaryTree:
    async def test_flag_off_is_noop(self, mocker):
        pool = FakePool()
        mocker.patch.object(service, "_pool", pool)
        mocker.patch.object(summaries.settings, "MEMORY_SUMMARY_TREE", False)
        assert await summaries.build_summary_tree() == 0
        assert pool.calls == []                                       # gated before any query

    async def test_builds_theme_summaries(self, mocker):
        wm = [{"cluster_id": "c1", "n": 12}]                          # KG watermark
        groups = [
            {"cluster_id": "c1", "theme_key": "CrashLoopBackOff", "n": 4, "n_verified": 3,
             "last_ep": None, "outcomes": ["resolved"], "recent_rcs": ["bad image"]},
            {"cluster_id": "c1", "theme_key": "payments", "n": 3, "n_verified": 0,
             "last_ep": None, "outcomes": None, "recent_rcs": None},
        ]
        pool = FakePool(fetch_results=[wm, groups], fetchrow_result="ANY")
        mocker.patch.object(service, "_pool", pool)
        mocker.patch.object(summaries.settings, "MEMORY_SUMMARY_TREE", True)
        mocker.patch.object(summaries.settings, "MEMORY_SUMMARY_MIN_CLUSTER", 3)
        n = await summaries.build_summary_tree()
        assert n == 2
        upserts = [c for c in pool.calls if c[0] == "fetchrow"]
        assert len(upserts) == 2
        # KG watermark threaded into the upsert (change-rate tie, R7.1).
        assert upserts[0][2][-1] == 12

    async def test_unchanged_theme_not_rewritten(self, mocker):
        # fetchrow returns None → ON CONFLICT WHERE suppressed the rewrite (no change).
        wm = [{"cluster_id": "c1", "n": 5}]
        groups = [{"cluster_id": "c1", "theme_key": "t", "n": 3, "n_verified": 1,
                   "last_ep": None, "outcomes": None, "recent_rcs": None}]
        pool = FakePool(fetch_results=[wm, groups], fetchrow_result=None)
        mocker.patch.object(service, "_pool", pool)
        mocker.patch.object(summaries.settings, "MEMORY_SUMMARY_TREE", True)
        assert await summaries.build_summary_tree() == 0             # nothing rebuilt on a quiet cluster

    async def test_upsert_sql_is_change_rate_conditional(self):
        sql = summaries._SQL_UPSERT_SUMMARY
        assert "ON CONFLICT (cluster_id, level, theme_key) DO UPDATE" in sql
        assert "WHERE EXCLUDED.last_episode_at IS DISTINCT FROM" in sql
        assert "EXCLUDED.kg_watermark <> memory_summaries.kg_watermark" in sql

    async def test_never_raises(self, mocker):
        class Boom:
            async def fetch(self, *a):
                raise RuntimeError("db down")
        mocker.patch.object(service, "_pool", Boom())
        mocker.patch.object(summaries.settings, "MEMORY_SUMMARY_TREE", True)
        assert await summaries.build_summary_tree() == 0


class TestRecallThemeSummaries:
    async def test_returns_matches_above_floor(self, mocker):
        rows = [
            {"theme_key": "CrashLoopBackOff", "summary": "…", "member_count": 4,
             "verified_count": 3, "last_episode_at": None, "sim": 0.5},
            {"theme_key": "noise", "summary": "…", "member_count": 1,
             "verified_count": 0, "last_episode_at": None, "sim": 0.001},   # below floor
        ]
        pool = FakePool(fetch_results=[rows])
        mocker.patch.object(service, "_pool", pool)
        out = await summaries.recall_theme_summaries("crashloop", "c1", k=3)
        assert len(out) == 1 and out[0]["theme_key"] == "CrashLoopBackOff"

    async def test_empty_query_is_empty(self, mocker):
        mocker.patch.object(service, "_pool", FakePool())
        assert await summaries.recall_theme_summaries("  ", "c1") == []

    def test_render_block(self):
        block = summaries.render_summaries_block(
            [{"summary": "Theme 'X': 4 episodes (3 verified)."}]
        )
        assert "Memory themes" in block and "Theme 'X'" in block
        assert summaries.render_summaries_block([]) == ""


class TestTriageInjection:
    """P8 completion — theme summaries reach the triage prompt via memory_loader (flag-gated)."""

    async def _run(self, mocker, flag_on):
        from app.agent.nodes import memory_loader as ml

        mocker.patch("app.memory.service.memory_active", return_value=True)
        mocker.patch("app.cluster_id.get_cluster_id", return_value="c1")
        mocker.patch("app.memory.episodes.recall_episodes", return_value=[])
        mocker.patch("app.memory.episodes.render_recall_block", return_value="")
        mocker.patch("app.memory.kg.recent_changes_block", return_value="")
        mocker.patch("app.memory.summaries.recall_theme_summaries",
                     return_value=[{"summary": "Theme 'payments': 4 episodes (3 verified)."}])
        mocker.patch("app.core.config.settings.MEMORY_SUMMARY_TREE", flag_on)
        state = {"messages": [], "session_id": "s"}
        return await ml._hierarchy_context(state)

    async def test_flag_on_injects_theme_block(self, mocker):
        out = await self._run(mocker, flag_on=True)
        assert "Memory themes" in out and "payments" in out

    async def test_flag_off_no_theme_block(self, mocker):
        out = await self._run(mocker, flag_on=False)
        assert "Memory themes" not in out
