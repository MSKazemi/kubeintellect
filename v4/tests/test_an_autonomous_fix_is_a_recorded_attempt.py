"""The promotion store had no writer, and the column naming its writer had the wrong value.

`promotion_outcomes` (ADR-102, `schema.sql`) is the durable evidence an action class needs to
*earn* a rung — the "earned, not configured" autonomy the project claimed as a differentiator
(narrowed 2026-08-28: what ships is the revoke half only — see `promotion_engine`).
Measured 2026-08-24 and again today: `record_outcome` was called only from its own tests. So
`outcomes_from_store` returned `[]` for every class, `decide()` answered `hold` with the honest
reason *"n 0 < n_min 20"*, and every class sat at its configured rung for ever. The demotion
direction is the sharp end — ADR-102 is *fast down, slow up*, so a class whose agreement had
collapsed could not be demoted either. A correct decision function fed by an empty store.

Wiring a writer needed a way to tell an autonomous attempt from a human-driven one, and the
column that exists for that was wrong. `write_episode(trigger_kind=…)` had three call sites:
`cortex/graph.py` read `state["trigger_source"]`, and **both** V2 coordinator writers hardcoded
`"user_query"`. `CORTEX_V4_ENABLED` is off by default, so in the shipped configuration every
watchtower investigation was stored as a user query. Two consequences, neither loud:

* the digest cannot ask the column and falls back to matching the substring
  *"autonomous investigation"* in `trigger_detail`;
* provenance drives write-admission trust (`security._TRUST`: detector 1.0, user_query 0.4), so
  detector-derived episodes were validated as if a chat client had typed them.

What is pinned here: the mapping is one function all three writers share; the V2 path carries
detector provenance through to the episode; and the promotion writer records a sample only for a
graded, autonomously-attempted fix — never for a report-only run, never for a human's fix, and
never when the post-fix cluster read failed, which `_verify_resolution` reports as `None` for the
same reason it reports verification-disabled as `None`.
"""

from __future__ import annotations

import pytest

from app.autonomy import promotion_source
from app.autonomy.promotion_source import (
    WATCHTOWER_AUTOFIX,
    decide_from_store,
    record_autonomous_attempt,
)
from app.core.config import settings
from app.memory import episodes


class FakePool:
    """Records every statement; returns `row` from fetchrow and `rows` from fetch."""

    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row
        self.calls: list[tuple] = []

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self.rows

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self.row

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "INSERT 0 1"

    def promotion_inserts(self):
        return [c for c in self.calls if "promotion_outcomes" in c[1]]


# ── provenance: one mapping, three writers ───────────────────────────────────


class TestTriggerKind:
    def test_detector_is_the_only_value_that_earns_detector_provenance(self):
        assert episodes.trigger_kind_for("detector") == "detector"

    @pytest.mark.parametrize("value", [None, "", "  ", "user_query", "user", "watchtower",
                                       "Detector", "DETECTOR", "detector-ish"])
    def test_everything_else_is_a_user_query(self, value):
        """Including near-misses: provenance is not something a caller talks its way into.
        (Surrounding whitespace is stripped — that is not a different value.)"""
        assert episodes.trigger_kind_for(value) == "user_query"

    def test_all_three_writers_call_it(self):
        """The defect was three independent derivations; a fourth must not appear silently."""
        import inspect

        from app.agent.nodes import coordinator
        from app.cortex import graph

        for module in (coordinator, graph):
            src = inspect.getsource(module)
            for line in src.splitlines():
                stripped = line.strip()
                if stripped.startswith("trigger_kind="):
                    assert "trigger_kind_for(" in stripped, (
                        f"{module.__name__} derives trigger_kind by hand: {stripped}"
                    )


@pytest.mark.parametrize("source,expected", [("detector", "detector"),
                                             ("user_query", "user_query"),
                                             (None, "user_query")])
def test_the_v2_coordinator_carries_provenance_into_the_episode(monkeypatch, source, expected):
    """The behavioural half of the fix: drive the writer that had the constant in it.

    `CORTEX_V4_ENABLED` is off by default, so this — not `cortex/graph.py` — is the path a
    shipped watchtower investigation actually takes.
    """
    import asyncio

    from app.agent.nodes import coordinator
    from app.db import memory_store
    from app.memory import episodes as ep_mod

    written: list[dict] = []

    class _Handle:
        """Stands in for a coroutine so nothing has to be awaited or garbage-collected."""

    def fake_write_episode(**kw):
        written.append(kw)
        return _Handle()

    monkeypatch.setattr(settings, "REFLEXION_ENABLED", True)
    monkeypatch.setattr(settings, "USE_SQLITE", False)
    monkeypatch.setattr(coordinator, "_ran_mutation", lambda m: (True, ["kubectl scale x"]))
    monkeypatch.setattr(coordinator, "_extract_mutation_pairs", lambda m: [{"cmd": "scale"}])
    monkeypatch.setattr(coordinator, "_infer_namespace", lambda c, p: "payments")
    monkeypatch.setattr(coordinator, "_verify_resolution",
                        lambda ns, pre_state=None: (True, "resolved"))
    monkeypatch.setattr(ep_mod, "write_episode", fake_write_episode)
    monkeypatch.setattr(memory_store, "record_rca_outcome", lambda **kw: _Handle())
    monkeypatch.setattr(asyncio, "create_task", lambda handle: handle)

    state = {"session_id": "s", "cluster_id": "c1", "user_role": "admin"}
    if source is not None:
        state["trigger_source"] = source
    coordinator._maybe_record_direct_outcome(state, [])

    assert len(written) == 1
    assert written[0]["trigger_kind"] == expected
    assert written[0]["verified"] is True
    assert written[0]["outcome"] == "resolved"


# ── the writer's admission rules ─────────────────────────────────────────────


BASE = dict(episode_id="ep-1", trigger_kind="detector", outcome="resolved",
            verified=True, playbooks=["CrashLoopBackOff"], at_seconds=86400.0)


@pytest.mark.asyncio
class TestRecordAutonomousAttempt:
    async def test_a_graded_autonomous_fix_lands_a_row(self):
        pool = FakePool()
        assert await record_autonomous_attempt(pool, **BASE) is True
        (_, sql, args) = pool.promotion_inserts()[0]
        assert args[0] == WATCHTOWER_AUTOFIX
        assert args[1] == 1.0                    # 86400s → day 1
        assert args[2] is True                   # success
        assert args[3] == "ep-1"                 # incident_id points at a readable episode
        assert args[4] == "CrashLoopBackOff"     # incident_type = the fault, the ADR's axis
        assert args[5] is False                  # critical: not attributed by this path

    async def test_a_failed_fix_is_a_sample_too(self):
        """Demotion is the safety half — a failure that is not recorded cannot demote anything."""
        pool = FakePool()
        assert await record_autonomous_attempt(
            pool, **{**BASE, "outcome": "partial", "verified": False}) is True
        assert pool.promotion_inserts()[0][2][2] is False

    async def test_a_humans_fix_is_not_this_action_class(self):
        pool = FakePool()
        assert await record_autonomous_attempt(
            pool, **{**BASE, "trigger_kind": "user_query"}) is False
        assert pool.promotion_inserts() == []

    async def test_an_unverified_outcome_is_not_a_sample(self):
        """`None` means verification was off OR the post-fix read failed. Neither is evidence,
        and a read is most likely to fail right after the disruptive change being graded."""
        pool = FakePool()
        assert await record_autonomous_attempt(pool, **{**BASE, "verified": None}) is False
        assert pool.promotion_inserts() == []

    @pytest.mark.parametrize("outcome", ["report_only", "", None, "rechecked"])
    async def test_an_investigation_that_changed_nothing_is_not_an_attempt(self, outcome):
        pool = FakePool()
        assert await record_autonomous_attempt(pool, **{**BASE, "outcome": outcome}) is False
        assert pool.promotion_inserts() == []

    async def test_a_missing_playbook_does_not_invent_one(self):
        pool = FakePool()
        await record_autonomous_attempt(pool, **{**BASE, "playbooks": None})
        assert pool.promotion_inserts()[0][2][4] == "generic"


# ── the episode write path ───────────────────────────────────────────────────


@pytest.mark.asyncio
class TestWriteEpisodeFeedsTheStore:
    async def _write(self, pool, **kw):
        episodes.init_episodes(pool)
        try:
            return await episodes.write_episode(
                cluster_id="c1", summary="restarted the deployment", **kw)
        finally:
            episodes.close_episodes()

    async def test_the_flag_gates_it(self, monkeypatch):
        monkeypatch.setattr(settings, "KI_V5_STATISTICAL_PROMOTION", False)
        pool = FakePool(row={"id": "ep-9"})
        await self._write(pool, trigger_kind="detector", outcome="resolved", verified=True,
                          playbooks=["OOMKilled"], ended_at=172800.0)
        assert pool.promotion_inserts() == []

    async def test_an_autonomous_verified_fix_reaches_the_store(self, monkeypatch):
        monkeypatch.setattr(settings, "KI_V5_STATISTICAL_PROMOTION", True)
        pool = FakePool(row={"id": "ep-9"})
        await self._write(pool, trigger_kind="detector", outcome="resolved", verified=True,
                          playbooks=["OOMKilled"], ended_at=172800.0)
        args = pool.promotion_inserts()[0][2]
        assert (args[0], args[1], args[2], args[3], args[4]) == (
            WATCHTOWER_AUTOFIX, 2.0, True, "ep-9", "OOMKilled")

    async def test_a_statistics_failure_never_breaks_the_episode_write(self, monkeypatch):
        """Fire-and-forget, like every other memory side effect on this path."""
        monkeypatch.setattr(settings, "KI_V5_STATISTICAL_PROMOTION", True)

        async def boom(*a, **kw):
            raise RuntimeError("promotion store down")

        monkeypatch.setattr(promotion_source, "record_autonomous_attempt", boom)
        pool = FakePool(row={"id": "ep-9"})
        assert await self._write(pool, trigger_kind="detector", outcome="resolved",
                                 verified=True, playbooks=["OOMKilled"]) == "ep-9"


# ── the loop, end to end ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recorded_outcomes_change_what_the_engine_decides(monkeypatch):
    """The point of the writer: `hold — n 0 < n_min` was a statement about an empty table.

    L1->L2 asks for θ 0.95, n_min 20, T_min 7d, 5 distinct incidents, 1 type. n alone is not
    enough: a Wilson LCB over 25 clean samples is 0.902, still under θ — the rung is earned by
    the *lower bound*, not the point estimate. Sixty clean samples reach 0.957 and promote. Both
    halves matter here, because a store with no writer produced the first answer for ever.
    """
    monkeypatch.setattr(settings, "KI_V5_STATISTICAL_PROMOTION", True)

    empty = FakePool(rows=[])
    held = await decide_from_store(empty, WATCHTOWER_AUTOFIX, "L1->L2", "L1", now_days=30.0)
    assert held.action == "hold"
    assert any("n 0" in r for r in held.reasons)

    def clean(n):
        return [{"ts_days": 1.0 + d * 0.5, "success": True, "incident_id": f"ep-{d}",
                 "incident_type": "CrashLoopBackOff", "critical": False} for d in range(n)]

    few = await decide_from_store(FakePool(rows=clean(25)), WATCHTOWER_AUTOFIX, "L1->L2", "L1",
                                  now_days=30.0)
    assert few.action == "hold"
    assert any("LCB" in r for r in few.reasons)   # not "n 0" any more — a real measurement

    earned = await decide_from_store(FakePool(rows=clean(60)), WATCHTOWER_AUTOFIX, "L1->L2", "L1",
                                     now_days=30.0)
    assert earned.action == "promote"
    assert earned.to_rung == "L2"
