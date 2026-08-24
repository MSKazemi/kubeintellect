"""A consolidation worker that got nothing done must not report the same thing twice.

THE DEFECT
----------
Every pass in `run_consolidation_once` returns an `int`, and every one returns `0` both when it
ran with nothing to do and when it raised and its own guard caught it. Measured 2026-08-24 by
driving the real worker against a pool whose every statement raises, with a healthy-but-idle pool
as the control:

    HEALTHY + IDLE     {"backfilled": 0, "stale_edges_closed": 0, "detector_candidates": 0,
                        "prefs_inferred": 0, "prefs_forgotten": 0}
    EVERY QUERY FAILS  {"backfilled": 0, "stale_edges_closed": 0, "detector_candidates": 0,
                        "prefs_inferred": 0, "prefs_forgotten": 0}     ← identical

Each pass does emit its own `WARNING`, so this was never a silent outage in the *log*. It was a
silent outage in the **machine-readable** result — the dict returned to callers and documented as
being "for tests/digest" — and the worker's own summary line was gated on `if any(stats.values())`,
so the one line that could have said *the pass completed, here is what it did* fired for neither
state. An operator's only recourse was to correlate five WARNINGs from three loggers, every 600
seconds, forever.

The failure discipline itself was right and is unchanged: a pass that raises must not stop the
loop or the passes after it. What changed is that the guard now says so.

WHAT IS ASSERTED
----------------
1. `pass_health` records, drains once, and does not accumulate across passes.
2. All eight guarded passes report their own name — driven individually, because a register only
   consolidation.py writes to would leave the other five modules unproven.
3. The worker separates the two states, in both directions: a dead pass differs from an idle one,
   AND an idle pass still reports zero failures rather than inventing one.
4. The summary line fires on failure (WARNING) and on real work (INFO), and stays quiet when
   there is genuinely nothing to say.
"""

from __future__ import annotations

import logging

import pytest
from app.core.config import settings
from app.memory import (
    consolidation,
    episodes,
    kg,
    pass_health,
    preferences,
    promotion,
    prospective,
    service,
    summaries,
)


class _Pool:
    """A pool that is connected and answering — or connected and failing every statement.

    `dead=True` is schema drift, a revoked grant, a bad migration: the exact class of outage
    that leaves `memory_active()` True while nothing works.
    """

    def __init__(self, dead: bool = False, rowcount: int = 0) -> None:
        self.dead = dead
        self.rowcount = rowcount

    def _boom(self) -> None:
        if self.dead:
            raise RuntimeError('relation "episodes" does not exist')

    async def execute(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
        self._boom()
        return f"UPDATE {self.rowcount}"

    async def fetch(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
        self._boom()
        return []

    async def fetchval(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
        self._boom()
        return 0

    async def fetchrow(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
        self._boom()
        return None


@pytest.fixture(autouse=True)
def _clean_register():
    pass_health.reset()
    yield
    pass_health.reset()


@pytest.fixture
def wired(mocker):
    """Wire a pool everywhere the way `service._activate` does, and turn the flagged passes on."""

    def _wire(dead: bool = False, rowcount: int = 0) -> _Pool:
        pool = _Pool(dead, rowcount)
        mocker.patch.object(service, "_pool", pool)
        mocker.patch.object(service, "memory_active", return_value=True)
        episodes.init_episodes(pool)
        preferences.init_preferences(pool)
        kg.init_kg(pool)
        mocker.patch.object(settings, "PREFERENCE_MEMORY_ENABLED", True)
        return pool

    return _wire


# ── 1. the register itself ───────────────────────────────────────────────────────────────────


class TestTheRegister:
    def test_a_recorded_failure_comes_back_with_its_reason(self):
        pass_health.record_failure("prefs_inferred", RuntimeError("permission denied"))
        assert pass_health.drain() == [("prefs_inferred", "permission denied")]

    def test_draining_forgets(self):
        pass_health.record_failure("x", "boom")
        assert pass_health.drain(), "vacuity guard: nothing was recorded to drain"
        assert pass_health.drain() == [], "a stale failure would be reported forever"

    def test_a_long_reason_is_bounded(self):
        pass_health.record_failure("x", "e" * 5000)
        (_, why), = pass_health.drain()
        assert len(why) == 200

    def test_reset_clears(self):
        pass_health.record_failure("x", "boom")
        pass_health.reset()
        assert pass_health.drain() == []


# ── 2. every guarded pass reports its own name ───────────────────────────────────────────────


class TestEveryPassReportsItself:
    @pytest.mark.parametrize(
        ("name", "call"),
        [
            ("backfilled", lambda: episodes.backfill_from_rca_outcomes("c")),
            ("stale_edges_closed", lambda: consolidation._close_stale_edges()),
            ("detector_candidates", lambda: consolidation._propose_detector_candidates()),
            ("prefs_inferred", lambda: preferences.infer_from_behaviour()),
            ("prefs_forgotten", lambda: preferences.decay_and_forget()),
            ("rules_promoted", lambda: promotion.promote_from_episodes()),
            ("prospective_fired", lambda: prospective.run_prospective_once()),
            ("summaries_built", lambda: summaries.build_summary_tree()),
        ],
    )
    async def test_a_failing_pass_records_its_name(self, name, call, wired, mocker):
        wired(dead=True)
        for flag in ("MEMORY_PROMOTION", "MEMORY_PROSPECTIVE", "MEMORY_SUMMARY_TREE"):
            mocker.patch.object(settings, flag, True)
        result = await call()
        assert result == 0, "the pass must still fail safe to a zero counter"
        assert [n for n, _ in pass_health.drain()] == [name]

    @pytest.mark.parametrize(
        ("name", "call"),
        [
            ("backfilled", lambda: episodes.backfill_from_rca_outcomes("c")),
            ("stale_edges_closed", lambda: consolidation._close_stale_edges()),
            ("prefs_inferred", lambda: preferences.infer_from_behaviour()),
            ("prefs_forgotten", lambda: preferences.decay_and_forget()),
        ],
    )
    async def test_a_healthy_pass_records_nothing(self, name, call, wired):
        # The other direction. A register that always fires proves as little as one that never does.
        wired(dead=False)
        await call()
        assert pass_health.drain() == [], f"{name} reported a failure on a healthy pool"


# ── 3. the worker's result separates the two states ──────────────────────────────────────────


class TestTheWorkerResult:
    async def test_dead_and_idle_are_no_longer_identical(self, wired):
        wired(dead=False)
        idle = await consolidation.run_consolidation_once(startup=True)
        wired(dead=True)
        dead = await consolidation.run_consolidation_once(startup=True)
        assert idle != dead, "a dead subsystem still reports exactly what an idle one reports"
        assert set(idle) == set(dead), "the two must differ in values, not in shape"

    async def test_an_idle_pass_reports_zero_failures(self, wired):
        wired(dead=False)
        stats = await consolidation.run_consolidation_once(startup=True)
        assert stats["failed_passes"] == 0

    async def test_a_dead_pass_reports_every_failure(self, wired):
        wired(dead=True)
        stats = await consolidation.run_consolidation_once(startup=True)
        # 5 passes run unflagged: backfill (startup), stale edges, candidates, and both prefs.
        assert stats["failed_passes"] == 5
        assert all(v == 0 for k, v in stats.items() if k != "failed_passes"), (
            "vacuity guard: the counters must still be zero — this is about the report, not the work"
        )

    async def test_the_counter_does_not_leak_into_the_next_pass(self, wired):
        wired(dead=True)
        await consolidation.run_consolidation_once(startup=True)
        wired(dead=False)
        stats = await consolidation.run_consolidation_once(startup=True)
        assert stats["failed_passes"] == 0, "last pass's failures were reported again"

    async def test_memory_off_still_returns_an_empty_dict(self, mocker):
        # The documented early return. `memory_status()` is the authority there, not this counter.
        mocker.patch.object(service, "memory_active", return_value=False)
        assert await consolidation.run_consolidation_once() == {}


# ── 4. the summary line ──────────────────────────────────────────────────────────────────────


def _summary(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "consolidation_pass" in r.getMessage()]


class TestTheSummaryLine:
    async def test_a_failed_pass_is_summarised_at_warning(self, wired, caplog):
        wired(dead=True)
        with caplog.at_level(logging.INFO):
            await consolidation.run_consolidation_once(startup=True)
        recs = _summary(caplog)
        assert len(recs) == 1
        assert recs[0].levelno == logging.WARNING
        msg = recs[0].getMessage()
        assert "INCOMPLETE" in msg
        assert "5 of 5 passes failed" in msg
        # It must name what broke; a bare count would reproduce the defect one level up.
        assert "prefs_inferred" in msg and "does not exist" in msg

    async def test_an_idle_pass_says_nothing(self, wired, caplog):
        wired(dead=False)
        with caplog.at_level(logging.INFO):
            await consolidation.run_consolidation_once(startup=True)
        assert _summary(caplog) == [], "600s of nothing-to-do must not become 600s of log noise"

    async def test_real_work_is_still_summarised_at_info(self, wired, caplog):
        wired(dead=False, rowcount=7)
        with caplog.at_level(logging.INFO):
            await consolidation.run_consolidation_once(startup=True)
        recs = _summary(caplog)
        assert len(recs) == 1, "a pass that did work stopped reporting it"
        assert recs[0].levelno == logging.INFO
        assert "INCOMPLETE" not in recs[0].getMessage()

    async def test_failure_outranks_work(self, wired, caplog):
        # A pass that half-worked and half-failed must report the failure, not the work.
        wired(dead=True)
        pass_health.record_failure("stale_edges_closed", "boom")
        with caplog.at_level(logging.INFO):
            await consolidation.run_consolidation_once(startup=True)
        recs = _summary(caplog)
        assert len(recs) == 1 and recs[0].levelno == logging.WARNING
