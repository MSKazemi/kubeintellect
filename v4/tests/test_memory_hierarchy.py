"""P3 memory hierarchy — episodes (L1), consolidation, injection latency gate."""
from __future__ import annotations

import time

from app.core.config import settings
from app.memory import episodes, service, summaries
from app.memory.consolidation import _playbooks_from_key


class FakePool:
    def __init__(self, rows=None, row=None, execute_result="INSERT 0 1"):
        self.rows = rows or []
        self.row = row
        self.execute_result = execute_result
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

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        if self.raise_on == "execute":
            raise RuntimeError("db down")
        return self.execute_result


class TestEpisodes:
    def test_recall_similarity_floor_defaults_to_point_zero_two(self):
        assert settings.MEMORY_RECALL_SIMILARITY_FLOOR == 0.02

    async def test_write_returns_id(self):
        pool = FakePool(row={"id": "ep-uuid-1"})
        episodes.init_episodes(pool)
        try:
            episode_id = await episodes.write_episode(
                cluster_id="c1", trigger_kind="user_query", summary="fixed crashloop",
            )
            assert episode_id == "ep-uuid-1"
        finally:
            episodes.close_episodes()

    async def test_write_redacts_secrets(self):
        pool = FakePool(row={"id": "x"})
        episodes.init_episodes(pool)
        try:
            await episodes.write_episode(
                cluster_id="c1", trigger_kind="user_query",
                summary="set password=hunter2 in the deployment",
            )
            _, _, args = pool.calls[0]
            assert all("hunter2" not in str(a) for a in args)
        finally:
            episodes.close_episodes()

    async def test_write_never_raises(self):
        pool = FakePool()
        pool.raise_on = "fetchrow"
        episodes.init_episodes(pool)
        try:
            assert await episodes.write_episode(
                cluster_id="c1", trigger_kind="user_query", summary="x"
            ) is None
        finally:
            episodes.close_episodes()

    async def test_recall_filters_noise_floor(self):
        rows = [
            {"id": "1", "summary": "crashloop in payments", "root_cause": "bad image",
             "outcome": "resolved", "verified": True, "confidence": 0.9,
             "playbooks": ["CrashLoopBackOff"], "namespace": "payments",
             "started_at": None, "sim": 0.42},
            {"id": "2", "summary": "unrelated", "root_cause": None, "outcome": None,
             "verified": None, "confidence": None, "playbooks": [], "namespace": None,
             "started_at": None, "sim": 0.01},
        ]
        pool = FakePool(rows=rows)
        episodes.init_episodes(pool)
        try:
            out = await episodes.recall_episodes("crashloop payments", "c1")
            assert len(out) == 1 and out[0]["id"] == "1"
        finally:
            episodes.close_episodes()

    async def test_recall_uses_configured_similarity_floor(self, mocker):
        mocker.patch.object(episodes.settings, "MEMORY_RECALL_SIMILARITY_FLOOR", 0.0)
        pool = FakePool(
            rows=[
                {
                    "id": "low-sim",
                    "summary": "rare incident",
                    "root_cause": None,
                    "outcome": None,
                    "verified": None,
                    "confidence": None,
                    "playbooks": [],
                    "namespace": None,
                    "started_at": None,
                    "sim": 0.01,
                }
            ]
        )
        episodes.init_episodes(pool)
        try:
            out = await episodes.recall_episodes("rare incident", "c1")
            assert [row["id"] for row in out] == ["low-sim"]
        finally:
            episodes.close_episodes()

    async def test_recall_hybrid_keeps_lexical_only_match(self, mocker):
        # Memory V5 P1 (ADR-014): on the hybrid path, a row matched by the lexical
        # (ts_rank) channel but with low trgm sim must be KEPT — the RRF SQL already
        # filtered to channel-matched rows, so the trgm noise floor must not re-drop it.
        mocker.patch.object(episodes.settings, "MEMORY_HYBRID_RETRIEVAL", True)
        rows = [
            {"id": "1", "summary": "crashloop in payments", "root_cause": "bad image",
             "outcome": "resolved", "verified": True, "confidence": 0.9,
             "playbooks": [], "namespace": "payments", "started_at": None,
             "sim": 0.42, "lex": 0.10, "rrf": 0.031},
            {"id": "2", "summary": "oomkilled worker", "root_cause": None, "outcome": None,
             "verified": None, "confidence": None, "playbooks": [], "namespace": None,
             "started_at": None, "sim": 0.01, "lex": 0.20, "rrf": 0.030},
        ]
        pool = FakePool(rows=rows)
        episodes.init_episodes(pool)
        try:
            out = await episodes.recall_episodes("crashloop payments", "c1")
            assert {r["id"] for r in out} == {"1", "2"}  # lex-only row 2 retained
            assert any(
                "rrf" in c[1].lower() and c[0] == "fetch" for c in pool.calls
            )  # the hybrid RRF query was issued
        finally:
            episodes.close_episodes()

    async def test_recall_hybrid_never_raises(self, mocker):
        mocker.patch.object(episodes.settings, "MEMORY_HYBRID_RETRIEVAL", True)
        pool = FakePool()
        pool.raise_on = "fetch"
        episodes.init_episodes(pool)
        try:
            assert await episodes.recall_episodes("x", "c1") == []
        finally:
            episodes.close_episodes()

    async def test_recall_uninitialised_returns_empty(self):
        episodes.close_episodes()
        assert await episodes.recall_episodes("anything", "c1") == []

    def test_render_recall_block(self):
        block = episodes.render_recall_block([
            {"summary": "crashloop in payments fixed by image rollback",
             "root_cause": "bad tag", "outcome": "resolved", "verified": True},
        ])
        assert "Similar past episodes" in block
        assert "resolved/verified" in block
        assert "bad tag" in block
        assert episodes.render_recall_block([]) == ""


class TestConsolidation:
    def test_playbooks_from_structured_key(self):
        key = "playbook=CrashLoopBackOff+ServiceNoEndpoints | ns=shop | cluster=kind-x"
        assert _playbooks_from_key(key) == ["CrashLoopBackOff", "ServiceNoEndpoints"]
        assert _playbooks_from_key("query=何かのフリーテキスト") == []

    async def test_candidate_proposed_for_verified_pattern(self, mocker):
        from app.memory import consolidation, service

        pool = FakePool(rows=[{
            "pattern_name": "playbook=CrashLoopBackOff | ns=s | cluster=c1",
            "cluster_id": "c1", "description": "",
        }])
        mocker.patch.object(service, "_pool", pool)
        created = await consolidation._propose_detector_candidates()
        assert created == 1
        insert = [c for c in pool.calls if c[0] == "execute"][0]
        assert "INSERT INTO detectors" in insert[1]
        assert "learned:playbook=CrashLoopBackOff" in str(insert[2])

    async def test_llm_only_playbook_yields_no_candidate(self, mocker):
        from app.memory import consolidation, service

        pool = FakePool(rows=[{
            "pattern_name": "playbook=CommandHardcodedFailure | ns=s | cluster=c1",
            "cluster_id": "c1", "description": "",
        }])
        mocker.patch.object(service, "_pool", pool)
        assert await consolidation._propose_detector_candidates() == 0

    async def test_consolidation_inactive_memory_is_noop(self, mocker):
        from app.memory import consolidation, service

        mocker.patch.object(service, "_pool", None)
        assert await consolidation.run_consolidation_once() == {}


class TestSummaryRecall:
    async def test_recall_uses_configured_similarity_floor(self, mocker):
        mocker.patch.object(summaries.settings, "MEMORY_RECALL_SIMILARITY_FLOOR", 0.0)
        pool = FakePool(
            rows=[
                {
                    "theme_key": "rare incident",
                    "summary": "one episode",
                    "member_count": 1,
                    "verified_count": 0,
                    "last_episode_at": None,
                    "sim": 0.01,
                }
            ]
        )
        mocker.patch.object(service, "_pool", pool)

        out = await summaries.recall_theme_summaries("rare incident", "c1")

        assert [row["theme_key"] for row in out] == ["rare incident"]


class TestInjectionLatencyGate:
    async def test_hierarchy_injection_under_200ms(self, mocker):
        """P3 exit gate: triage-time injection < 200 ms p95 (unit form —
        measured against an instant fake pool; the live form is measured in
        the server log via memory_hierarchy_injected ms=...)."""
        from app.agent.nodes import memory_loader as ml

        pool = FakePool(rows=[])
        episodes.init_episodes(pool)
        mocker.patch("app.memory.service.memory_active", return_value=True)
        mocker.patch("app.memory.kg.recent_changes_block", return_value="")
        mocker.patch("app.cluster_id.get_cluster_id", return_value="c1")
        try:
            durations = []
            for _ in range(20):
                start = time.perf_counter()
                await ml._hierarchy_context({"messages": [], "session_id": "s"})
                durations.append((time.perf_counter() - start) * 1000)
            durations.sort()
            p95 = durations[int(len(durations) * 0.95) - 1]
            assert p95 < 200, f"injection p95 {p95:.1f} ms"
        finally:
            episodes.close_episodes()
