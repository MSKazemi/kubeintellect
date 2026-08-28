"""Memory V5 P6 (ADR-017) — prospective memory: schedule / fire / record re-checks (R6.4).

A fake pool is patched onto the memory service (the module reads `service._pool`, like the
consolidation worker). SQL is validated against a live DB at the gate.
"""
from __future__ import annotations

from app.memory import prospective, service


class FakePool:
    def __init__(self, fetchrow=None, due_rows=None):
        self._fetchrow = fetchrow
        self._due_rows = due_rows or []
        self.calls: list[tuple] = []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self._fetchrow

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self._due_rows

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "UPDATE 1"


def _reset_dispatch():
    prospective.set_dispatch(None)


class TestScheduleRecheck:
    async def test_upserts_and_returns_id(self, mocker):
        pool = FakePool(fetchrow={"id": "p1"})
        mocker.patch.object(service, "_pool", pool)
        pid = await prospective.schedule_recheck(
            cluster_id="c1", condition="did the fix hold?", due_at=1_000_000.0,
            namespace="dev",
        )
        assert pid == "p1"
        _, sql, args = pool.calls[0]
        assert "ON CONFLICT (cluster_id, dedup_key)" in sql          # deduped upsert
        assert args[0] == "c1" and args[1] == "dev"
        assert args[4] == "did the fix hold?"                        # dedup_key defaults to condition

    async def test_skips_empty_cluster_or_condition(self, mocker):
        pool = FakePool(fetchrow={"id": "p1"})
        mocker.patch.object(service, "_pool", pool)
        assert await prospective.schedule_recheck(
            cluster_id="", condition="x", due_at=1.0) is None
        assert await prospective.schedule_recheck(
            cluster_id="c1", condition="  ", due_at=1.0) is None
        assert pool.calls == []

    async def test_never_raises(self, mocker):
        class Boom:
            async def fetchrow(self, *a):
                raise RuntimeError("db down")
        mocker.patch.object(service, "_pool", Boom())
        assert await prospective.schedule_recheck(
            cluster_id="c1", condition="x", due_at=1.0) is None


class TestRunProspectiveOnce:
    async def test_flag_off_is_noop(self, mocker):
        pool = FakePool(due_rows=[{"id": "p1", "namespace": "dev"}])
        mocker.patch.object(service, "_pool", pool)
        mocker.patch.object(prospective.settings, "MEMORY_PROSPECTIVE", False)
        assert await prospective.run_prospective_once() == 0
        assert pool.calls == []                                      # gated before any query

    async def test_fires_due_and_records_outcome(self, mocker):
        _reset_dispatch()
        due = [{"id": "p1", "cluster_id": "c1", "namespace": "dev",
                "condition": "did the fix hold?", "check_query": "CrashLoopBackOff",
                "source_episode_id": None}]
        pool = FakePool(due_rows=due)
        mocker.patch.object(service, "_pool", pool)
        mocker.patch.object(prospective.settings, "MEMORY_PROSPECTIVE", True)
        # dev resolves to A1+ under the default ladder.
        mocker.patch.object(prospective, "level_for_namespace", lambda ns: "A1")
        # The default dispatcher re-reads the cluster (2026-08-28); before that it returned
        # 'rechecked' having read nothing. Give it a healthy namespace to grade.
        mocker.patch("app.agent.nodes.context_fetcher._kubectl_snapshot", side_effect=lambda a: (
            True,
            "NAMESPACE  NAME   READY  STATUS   RESTARTS  AGE\ndev  api-0  1/1  Running  0  4h\n"
            if a[1] == "pods" else "No resources found in dev namespace.\n"))
        n = await prospective.run_prospective_once()
        assert n == 1
        record = next(c for c in pool.calls if c[0] == "execute")
        assert record[2][1] == "resolved" and record[2][2] == "done"  # outcome + terminal status

    async def test_a0_namespace_never_fires(self, mocker):
        _reset_dispatch()
        called = {"n": 0}

        async def dispatch(row, level):
            called["n"] += 1
            return "rechecked"

        prospective.set_dispatch(dispatch)
        due = [{"id": "p1", "cluster_id": "c1", "namespace": "kube-system",
                "condition": "x", "check_query": None, "source_episode_id": None}]
        pool = FakePool(due_rows=due)
        mocker.patch.object(service, "_pool", pool)
        mocker.patch.object(prospective.settings, "MEMORY_PROSPECTIVE", True)
        mocker.patch.object(prospective, "level_for_namespace", lambda ns: "A0")
        n = await prospective.run_prospective_once()
        assert n == 1 and called["n"] == 0                          # dispatch NOT invoked
        record = next(c for c in pool.calls if c[0] == "execute")
        assert record[2][1] == "skipped_a0" and record[2][2] == "cancelled"
        _reset_dispatch()

    async def test_dispatch_error_keeps_pending_for_retry(self, mocker):
        async def boom(row, level):
            raise RuntimeError("investigation crashed")

        prospective.set_dispatch(boom)
        due = [{"id": "p1", "cluster_id": "c1", "namespace": "dev",
                "condition": "x", "check_query": None, "source_episode_id": None}]
        pool = FakePool(due_rows=due)
        mocker.patch.object(service, "_pool", pool)
        mocker.patch.object(prospective.settings, "MEMORY_PROSPECTIVE", True)
        mocker.patch.object(prospective, "level_for_namespace", lambda ns: "A1")
        n = await prospective.run_prospective_once()
        assert n == 1
        record = next(c for c in pool.calls if c[0] == "execute")
        assert record[2][1] == "error" and record[2][2] == "pending"  # retried next pass
        _reset_dispatch()

    async def test_claim_sql_is_atomic_skip_locked(self):
        # The scheduler must claim-and-flip in one statement so two passes never double-fire.
        assert "FOR UPDATE SKIP LOCKED" in prospective._SQL_CLAIM_DUE
        assert "SET status = 'fired'" in prospective._SQL_CLAIM_DUE

    async def test_never_raises_on_claim_error(self, mocker):
        class Boom:
            async def fetch(self, *a):
                raise RuntimeError("db down")
        mocker.patch.object(service, "_pool", Boom())
        mocker.patch.object(prospective.settings, "MEMORY_PROSPECTIVE", True)
        assert await prospective.run_prospective_once() == 0
