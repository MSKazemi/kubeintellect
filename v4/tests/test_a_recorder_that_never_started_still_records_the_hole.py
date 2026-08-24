"""The flight recorder's own invariant did not cover its largest loss mode.

THE MODULE'S PROMISE (its docstring, and the basis of the tamper-evidence claim)
--------------------------------------------------------------------------------
    "A lost batch must not be invisible either. Restoring contiguity alone would make loss
    undetectable — chain intact over a log with holes, which is exactly what the tamper-evidence
    claim promises cannot happen. So every loss is carried forward and written **into the chain**
    as a ``recorder_gap`` record on the next successful flush."

THE HOLE
--------
That machinery hangs entirely off a **flush**. `init_recorder` gave up after one failed connect
— no pool, no queue, no drain task, no retry — and `record()` then began with
`if _queue is None: return`. So a recorder that never started (the pod scheduled ahead of
Postgres, the ordinary rollout case) dropped every event with **no marker, no counter and no
way back**, for the life of the process.

Measured, with the mid-flight loss as the control:

    never started   3 events recorded → _pending_gaps == {}                    ← no evidence
    mid-flight      1 event lost      → _pending_gaps == {'ep': (1, reason)}   ← honest

And nothing reported it: `/healthz` carried `audit` and `memory` but no recorder field, and the
module exported no status accessor. The read side was already honest (`fetch_episode` raises
`RecorderUnavailable`), but only for someone who already suspected it and asked about a specific
episode.

WHAT IS ASSERTED
----------------
1. `recorder_status()` distinguishes ready / flag / sqlite / unavailable — in both directions.
2. A failed connect leaves a reconnect loop running, and it finishes startup when Postgres
   arrives (queue + drain), then stops.
3. **The payoff**: events lost while the recorder was down are written into the chain as a
   `recorder_gap` on the first successful flush after recovery — the invariant now holds for
   this loss mode too. Skipped kinds and configuration-off are not losses and carry nothing.
4. `/healthz` carries it.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from app.api.v1.endpoints.health import router as health_router
from app.core import readiness
from app.core.config import settings
from app.db import flight_recorder as fr
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakePool:
    """Captures what would reach `decision_log`."""

    def __init__(self):
        self.rows: list[tuple] = []
        self.heads: list[tuple] = []

    async def executemany(self, sql, rows):
        # Two statements per flush now: the events, then the chain-head anchor that makes a
        # truncation of them detectable. They carry different tuple shapes.
        (self.heads if "decision_log_head" in sql else self.rows).extend(rows)

    async def fetchrow(self, *a, **k):
        return None

    async def close(self):
        ...


@pytest.fixture(autouse=True)
def _clean_module_state():
    saved = (fr._pool, fr._queue, fr._drain_task, fr._reconnect_task,
             dict(fr._chains), dict(fr._pending_gaps),
             fr._state, fr._reason, fr._lost_while_down)
    fr._pool = fr._queue = fr._drain_task = fr._reconnect_task = None
    fr._chains.clear()
    fr._pending_gaps.clear()
    fr._state, fr._reason, fr._lost_while_down = "starting", "", 0
    yield
    for t in (fr._drain_task, fr._reconnect_task):
        if t:
            t.cancel()
    fr._chains.clear()
    fr._pending_gaps.clear()
    (fr._pool, fr._queue, fr._drain_task, fr._reconnect_task,
     chains, gaps, fr._state, fr._reason, fr._lost_while_down) = saved
    fr._chains.update(chains)
    fr._pending_gaps.update(gaps)


@pytest.fixture
def postgres(mocker):
    mocker.patch.object(settings, "USE_SQLITE", False)
    mocker.patch.object(settings, "FLIGHT_RECORDER_ENABLED", True)
    mocker.patch.object(fr, "_drain", mocker.AsyncMock())


def _connect_fails(mocker):
    return mocker.patch.object(
        fr.asyncpg, "create_pool",
        mocker.AsyncMock(side_effect=OSError("Connect call failed ('10.0.0.5', 5432)")),
    )


def _connect_works(mocker, pool=None):
    pool = pool or FakePool()
    mocker.patch.object(fr.asyncpg, "create_pool", mocker.AsyncMock(return_value=pool))
    return pool


# ── 1. the state is askable ───────────────────────────────────────────────────
class TestTheRecorderStateIsAskable:
    @pytest.mark.asyncio
    async def test_a_failed_connect_reports_disabled_and_why(self, postgres, mocker):
        _connect_fails(mocker)
        await fr.init_recorder()
        st = fr.recorder_status()
        assert st["enabled"] is False
        assert st["state"] == "unavailable"
        assert "10.0.0.5" in st["reason"]

    @pytest.mark.asyncio
    async def test_a_running_recorder_reports_enabled(self, postgres, mocker):
        """Vacuity guard: a status that always reads 'down' asserts nothing about failure."""
        _connect_works(mocker)
        await fr.init_recorder()
        st = fr.recorder_status()
        assert st["enabled"] is True and st["state"] == "ready" and st["reason"] == ""

    @pytest.mark.asyncio
    async def test_off_by_flag_and_sqlite_are_not_outages(self, mocker):
        mocker.patch.object(settings, "FLIGHT_RECORDER_ENABLED", False)
        await fr.init_recorder()
        assert fr.recorder_status()["state"] == "flag"

        fr._state, fr._reason = "starting", ""
        mocker.patch.object(settings, "FLIGHT_RECORDER_ENABLED", True)
        mocker.patch.object(settings, "USE_SQLITE", True)
        await fr.init_recorder()
        assert fr.recorder_status()["state"] == "sqlite"
        assert fr.recorder_status()["state"] != "unavailable"

    @pytest.mark.asyncio
    async def test_shutdown_stops_claiming_enabled(self, postgres, mocker):
        _connect_works(mocker)
        await fr.init_recorder()
        assert fr.recorder_status()["enabled"] is True
        await fr.close_recorder()
        assert fr.recorder_status()["enabled"] is False


# ── 2. the outage is not permanent ────────────────────────────────────────────
class TestTheOutageIsNotPermanent:
    @pytest.mark.asyncio
    async def test_a_failed_connect_leaves_something_running_to_retry(self, postgres, mocker):
        _connect_fails(mocker)
        await fr.init_recorder()
        assert fr._reconnect_task is not None and not fr._reconnect_task.done(), (
            "with nothing retrying, a rollout race disables tamper-evident recording "
            "for the life of the process"
        )

    @pytest.mark.asyncio
    async def test_a_successful_connect_starts_no_retry_loop(self, postgres, mocker):
        """Vacuity guard: the loop is a response to failure, not permanent scenery."""
        _connect_works(mocker)
        await fr.init_recorder()
        assert fr._reconnect_task is None

    @pytest.mark.asyncio
    async def test_reconnecting_finishes_startup(self, postgres, mocker):
        mocker.patch.object(fr, "_RETRY_INTERVAL_S", 0.01)
        _connect_fails(mocker)
        await fr.init_recorder()
        assert fr._queue is None

        _connect_works(mocker)
        await asyncio.sleep(0.1)
        assert fr.recorder_status()["state"] == "ready"
        assert fr._queue is not None, (
            "a pool with no queue and no drain task records nothing — reconnecting has to "
            "finish startup, not just open a connection"
        )
        assert fr._reconnect_task.done(), "the retry loop kept running after it succeeded"


# ── 3. the invariant now covers this loss mode ────────────────────────────────
class TestALossWithNoQueueIsStillCarried:
    @pytest.mark.asyncio
    async def test_events_lost_while_down_are_carried(self, postgres, mocker):
        _connect_fails(mocker)
        await fr.init_recorder()
        for i in range(3):
            fr.record("ep-live-incident", "tool_call", {"n": i})
        assert fr._pending_gaps == {
            "ep-live-incident": (3, "Connect call failed ('10.0.0.5', 5432)")
        }, f"the loss left no trace at all: {fr._pending_gaps}"
        assert fr.recorder_status()["lost_while_down"] == 3

    @pytest.mark.asyncio
    async def test_the_hole_reaches_the_chain_once_recording_resumes(self, postgres, mocker):
        """The whole point: the outage must end up *inside* the tamper-evident log."""
        _connect_fails(mocker)
        await fr.init_recorder()
        fr.record("ep-live-incident", "tool_call", {"n": 0})
        fr.record("ep-live-incident", "tool_call", {"n": 1})

        pool = _connect_works(mocker)
        mocker.patch.object(fr, "_RETRY_INTERVAL_S", 0.01)
        await asyncio.sleep(0.1)
        await fr._flush([("ep-live-incident", "final", {"answer": "ok"})])

        kinds = [r[2] for r in pool.rows]
        assert fr.GAP_KIND in kinds, f"the outage left no gap record in the chain: {kinds}"
        gap = json.loads(next(r[3] for r in pool.rows if r[2] == fr.GAP_KIND))
        assert gap["dropped"] == 2
        assert kinds.index(fr.GAP_KIND) < kinds.index("final"), (
            "the gap must be written before the events that followed it, or the replay "
            "shows the hole in the wrong place"
        )
        assert fr.verify_chain([
            {"episode_id": r[0], "seq": r[1], "kind": r[2], "payload": r[3],
             "prev_hash": r[4], "hash": r[5]} for r in pool.rows
        ]) is True, "the gap record must be chained like any other row"

    @pytest.mark.asyncio
    async def test_a_recorded_event_carries_no_gap(self, postgres, mocker):
        """Vacuity guard: the ledger must track loss, not traffic."""
        _connect_works(mocker)
        await fr.init_recorder()
        fr.record("ep1", "tool_call", {"n": 0})
        assert fr._pending_gaps == {}
        assert fr.recorder_status()["lost_while_down"] == 0
        assert fr._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_configuration_off_carries_nothing(self, mocker):
        """No chain exists, so there is no hole to be honest about — and marking every event
        of a deliberately-unrecorded deployment as 'lost' would be its own false alarm."""
        mocker.patch.object(settings, "FLIGHT_RECORDER_ENABLED", False)
        await fr.init_recorder()
        fr.record("ep1", "tool_call", {"n": 0})
        assert fr._pending_gaps == {}
        assert fr.recorder_status()["lost_while_down"] == 0

    @pytest.mark.asyncio
    async def test_skipped_kinds_are_not_losses(self, postgres, mocker):
        """Token frames are deliberately not recorded; counting them would inflate every
        outage by the size of the stream."""
        _connect_fails(mocker)
        await fr.init_recorder()
        for kind in fr._SKIP_KINDS:
            fr.record("ep1", kind, {})
        assert fr.recorder_status()["lost_while_down"] == 0

    @pytest.mark.asyncio
    async def test_the_gap_ledger_is_bounded(self, postgres, mocker):
        """A long outage across many episodes must not leak — but the total stays true."""
        mocker.patch.object(fr, "_MAX_TRACKED_EPISODES", 5)
        _connect_fails(mocker)
        await fr.init_recorder()
        for i in range(20):
            fr.record(f"ep{i}", "tool_call", {})
        assert len(fr._pending_gaps) == 5
        assert fr.recorder_status()["lost_while_down"] == 20, (
            "overflow must still be counted, or the bound becomes a second silent loss"
        )

    @pytest.mark.asyncio
    async def test_an_event_the_queue_rejects_is_also_carried(self, postgres, mocker):
        """The same hole one level down: `put_nowait` failing used to be a bare `pass`, so the
        event was lost without ever reaching the flush that owns the gap ledger."""
        _connect_works(mocker)
        await fr.init_recorder()

        class RejectingQueue:
            def put_nowait(self, _item):
                raise RuntimeError("queue is closed")

        fr._queue = RejectingQueue()
        fr.record("ep1", "tool_call", {"n": 0})
        assert fr._pending_gaps == {"ep1": (1, "queue is closed")}
        assert fr.recorder_status()["lost_while_down"] == 1

    @pytest.mark.asyncio
    async def test_the_silence_is_broken_at_least_once(self, postgres, mocker, caplog):
        _connect_fails(mocker)
        await fr.init_recorder()
        caplog.clear()
        with caplog.at_level("WARNING"):
            fr.record("ep1", "tool_call", {})
        assert any("event(s) not recorded" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_warning_does_not_flood(self, postgres, mocker, caplog):
        """Vacuity guard: one warning per recorded event would be its own outage."""
        _connect_fails(mocker)
        await fr.init_recorder()
        caplog.clear()
        with caplog.at_level("WARNING"):
            for i in range(30):
                fr.record("ep1", "tool_call", {"n": i})
        drops = [r for r in caplog.records if "event(s) not recorded" in r.message]
        assert len(drops) == 1, f"30 lost events produced {len(drops)} warnings"


# ── 4. the probe carries it ───────────────────────────────────────────────────
@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(health_router)
    readiness.set_ready(True)
    return TestClient(app)


class TestHealthzCarriesIt:
    def test_healthz_reports_a_dead_recorder(self, client, mocker):
        mocker.patch.object(fr, "_state", "unavailable")
        mocker.patch.object(fr, "_reason", "Connect call failed ('10.0.0.5', 5432)")
        mocker.patch.object(fr, "_lost_while_down", 77)
        body = client.get("/healthz").json()
        assert body["recorder"]["enabled"] is False, (
            "/healthz answered 'ok' for a server recording no decisions at all"
        )
        assert body["recorder"]["lost_while_down"] == 77

    def test_healthz_reports_a_live_recorder(self, client, mocker):
        """Vacuity guard: the field must track the state, not be a constant."""
        mocker.patch.object(fr, "_state", "ready")
        assert client.get("/healthz").json()["recorder"]["enabled"] is True

    def test_healthz_still_answers_200_while_the_recorder_is_down(self, client, mocker):
        mocker.patch.object(fr, "_state", "unavailable")
        assert client.get("/healthz").status_code == 200
