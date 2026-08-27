"""A detector read that failed must not be assigned into the live watchtower.

THE DEFECT
----------
`load_db_detectors` documented itself as "fail-open (no pool / query error → empty tuples)".
Fail-open is right for a *read*. But its only caller, `_refresh_db_detectors`, does not read —
it **assigns**:

    _engine.detectors = tuple(load_detectors()) + active
    _engine.shadow_detectors = shadow

so the empty tuple a failed query returns silently replaced every promoted detector in the
running engine. Measured 2026-08-24 against a `fetch` that raises, on a refresh loop that runs
every `DB_DETECTOR_REFRESH_SECONDS`:

    DB reachable, 3 promoted    engine.detectors = 23   (playbooks 20 + db 3)
    next refresh, query raises  engine.detectors = 20   (playbooks 20 + db 0)   ← disarmed
    DB comes back               engine.detectors = 23

Nothing reported the consequence. The engine logged `query failed` from a different module, and
`_refresh_db_detectors`'s own summary was gated on `if active or shadow:` — false for exactly the
case where coverage had just been removed. A transient DNS blip therefore disarmed the promoted
detectors for a whole refresh interval, silently.

The correct handler already existed in `_refresh_db_detectors` — `except ...: return`, which
keeps the loaded set — and was unreachable, because `load_db_detectors` swallowed the error
instead of raising. `DetectorStoreUnavailable` was built in this same subsystem for precisely
this distinction and had only ever been applied to the read path (`review.list_detectors`).

WHAT IS ASSERTED
----------------
1. A failed query raises; no pool and an empty table still return empty tuples, because those
   genuinely mean "there are no DB detectors".
2. A failed refresh KEEPS the loaded set, and a successful one still replaces it — both
   directions, since a refresh that never updates is as broken as one that always clears.
3. Coverage going away is logged. `if active or shadow:` announced arrival and hid removal.
4. Rows dropped for being malformed are counted and named, instead of vanishing.
"""

from __future__ import annotations

import json
import logging

import pytest
from app.detectors import service as dsvc
from app.detectors.engine import DetectorEngine, load_db_detectors, load_detectors
from app.detectors.review import DetectorStoreUnavailable
from app.memory import service as msvc

_LIVE = {"watch_predicates": [{"kind": "Pod", "status_regex": "^OOMKilled$"}]}
_UNCOMPILABLE = {"watch_predicates": [{"kind": "Pod", "status_regex": "^OOM("}]}


class _Pool:
    def __init__(self, rows=None, boom: bool = False) -> None:
        self.rows = rows or []
        self.boom = boom
        self.calls = 0

    async def fetch(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
        self.calls += 1
        if self.boom:
            raise RuntimeError("could not translate host name to address")
        return self.rows


def _row(name: str, predicate=None, status: str = "active") -> dict:
    return {
        "name": name,
        "predicate": json.dumps(_LIVE) if predicate is None else predicate,
        "status": status,
    }


@pytest.fixture
def engine(mocker):
    """A running sensorium with only the playbook-compiled detectors loaded."""
    base = tuple(load_detectors())
    mocker.patch.object(dsvc, "_engine", DetectorEngine(base))
    mocker.patch.object(dsvc, "_last_db_counts", (0, 0))
    return base


def _db_count(base) -> int:
    return len(dsvc._engine.detectors) - len(base)


# ── 1. the three answers `load_db_detectors` can give ────────────────────────────────────────


class TestTheReadDistinguishesItsAnswers:
    async def test_a_failed_query_raises(self, mocker):
        pool = _Pool(boom=True)
        mocker.patch.object(msvc, "_pool", pool)
        with pytest.raises(DetectorStoreUnavailable):
            await load_db_detectors()
        assert pool.calls == 1, "vacuity guard: the query was never attempted"

    async def test_no_pool_is_still_empty_tuples(self, mocker):
        # Not an outage: memory is not configured, so there are genuinely no DB detectors.
        mocker.patch.object(msvc, "_pool", None)
        assert await load_db_detectors() == ((), ())

    async def test_an_empty_table_is_still_empty_tuples(self, mocker):
        mocker.patch.object(msvc, "_pool", _Pool(rows=[]))
        assert await load_db_detectors() == ((), ())

    async def test_a_healthy_read_returns_what_is_stored(self, mocker):
        mocker.patch.object(
            msvc, "_pool",
            _Pool(rows=[_row("nl:a"), _row("nl:b"), _row("nl:s", status="shadow")]),
        )
        active, shadow = await load_db_detectors()
        assert (len(active), len(shadow)) == (2, 1), "vacuity guard: nothing loaded at all"


# ── 2. a failed refresh keeps the watchtower armed ───────────────────────────────────────────


class TestTheRefreshKeepsWhatItHas:
    async def test_a_failed_refresh_keeps_the_loaded_detectors(self, engine, mocker):
        mocker.patch.object(msvc, "_pool", _Pool(rows=[_row(f"nl:{i}") for i in range(3)]))
        await dsvc._refresh_db_detectors("global")
        assert _db_count(engine) == 3, "vacuity guard: nothing was loaded to lose"

        mocker.patch.object(msvc, "_pool", _Pool(boom=True))
        await dsvc._refresh_db_detectors("global")
        assert _db_count(engine) == 3, "a failed read disarmed the live watchtower"

    async def test_a_failed_refresh_keeps_shadow_detectors_too(self, engine, mocker):
        mocker.patch.object(msvc, "_pool", _Pool(rows=[_row("nl:s", status="shadow")]))
        await dsvc._refresh_db_detectors("global")
        assert len(dsvc._engine.shadow_detectors) == 1

        mocker.patch.object(msvc, "_pool", _Pool(boom=True))
        await dsvc._refresh_db_detectors("global")
        assert len(dsvc._engine.shadow_detectors) == 1

    async def test_a_successful_refresh_still_replaces_the_set(self, engine, mocker):
        # The other direction. Keeping the old set on failure must not become keeping it always.
        mocker.patch.object(msvc, "_pool", _Pool(rows=[_row(f"nl:{i}") for i in range(3)]))
        await dsvc._refresh_db_detectors("global")
        mocker.patch.object(msvc, "_pool", _Pool(rows=[_row("nl:only")]))
        await dsvc._refresh_db_detectors("global")
        assert _db_count(engine) == 1, "a real change was not applied"

    async def test_a_deliberate_removal_is_applied(self, engine, mocker):
        # An empty table is an answer, not an outage: a human demoting every detector must work.
        mocker.patch.object(msvc, "_pool", _Pool(rows=[_row("nl:a")]))
        await dsvc._refresh_db_detectors("global")
        mocker.patch.object(msvc, "_pool", _Pool(rows=[]))
        await dsvc._refresh_db_detectors("global")
        assert _db_count(engine) == 0

    async def test_a_stopped_sensorium_forgets_its_counts(self, engine, mocker):
        mocker.patch.object(msvc, "_pool", _Pool(rows=[_row("nl:a")]))
        await dsvc._refresh_db_detectors("global")
        assert dsvc._last_db_counts == (1, 0)
        await dsvc.stop_sensorium()
        # `None`, not `(0, 0)`: the sentinel now distinguishes "no refresh has completed in this
        # process" from "the last refresh found nothing". Both reset the counts, but only `None`
        # makes the next refresh announce what it loaded — including zero, which is what a
        # cluster-id mismatch looks like and what a bare `(0, 0)` kept silent.
        assert dsvc._last_db_counts is None, "a restart would inherit stale counts"


# ── 3. removal is reported, not only arrival ─────────────────────────────────────────────────


def _svc_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "db detector" in r.getMessage()]


class TestCoverageChangesAreReported:
    async def test_a_failed_refresh_says_what_it_kept(self, engine, mocker, caplog):
        mocker.patch.object(msvc, "_pool", _Pool(rows=[_row(f"nl:{i}") for i in range(3)]))
        await dsvc._refresh_db_detectors("global")
        mocker.patch.object(msvc, "_pool", _Pool(boom=True))
        with caplog.at_level(logging.INFO):
            await dsvc._refresh_db_detectors("global")
        kept = [m for m in _svc_lines(caplog) if "KEEPING" in m]
        assert len(kept) == 1 and "3 active" in kept[0]

    async def test_coverage_dropping_to_zero_is_logged(self, engine, mocker, caplog):
        mocker.patch.object(msvc, "_pool", _Pool(rows=[_row("nl:a")]))
        await dsvc._refresh_db_detectors("global")
        mocker.patch.object(msvc, "_pool", _Pool(rows=[]))
        caplog.clear()          # the arrival was already logged; this is about the removal
        with caplog.at_level(logging.INFO):
            await dsvc._refresh_db_detectors("global")
        lines = _svc_lines(caplog)
        assert len(lines) == 1, "the removal of every promoted detector was not reported"
        assert "0 active" in lines[0] and "was 1/0" in lines[0]

    async def test_a_steady_state_of_none_stays_quiet(self, engine, mocker, caplog):
        # The other direction: a cluster that has never had a DB detector must not log every
        # refresh interval forever. It must, however, say so ONCE — a startup that loads zero
        # detectors is exactly what a cluster-id mismatch looks like, and the previous "stay
        # completely quiet" behaviour is what hid one through a 24-hour shadow soak.
        mocker.patch.object(msvc, "_pool", _Pool(rows=[]))
        dsvc._last_db_counts = None  # as at process start
        with caplog.at_level(logging.INFO):
            await dsvc._refresh_db_detectors("global")
            await dsvc._refresh_db_detectors("global")
            await dsvc._refresh_db_detectors("global")
        lines = _svc_lines(caplog)
        assert len(lines) == 1, f"expected exactly one line, got {lines}"
        assert "0 active, 0 shadow" in lines[0]


# ── 4. rows that were dropped are named ──────────────────────────────────────────────────────


class TestDroppedRowsAreCounted:
    @pytest.mark.parametrize(
        ("predicate", "reason"),
        [
            ("{not json", "not valid JSON"),
            (json.dumps({"pattern": "x"}), "not a detect block"),
            (json.dumps(_UNCOMPILABLE), "did not compile"),
        ],
    )
    async def test_a_malformed_row_is_reported(self, predicate, reason, mocker, caplog):
        mocker.patch.object(msvc, "_pool", _Pool(rows=[_row("nl:bad", predicate)]))
        with caplog.at_level(logging.WARNING):
            active, shadow = await load_db_detectors()
        assert (active, shadow) == ((), ())
        msgs = [r.getMessage() for r in caplog.records if "were not loaded" in r.getMessage()]
        assert len(msgs) == 1
        assert "1 of 1" in msgs[0] and "nl:bad" in msgs[0] and reason in msgs[0]

    async def test_a_healthy_table_reports_nothing_skipped(self, mocker, caplog):
        mocker.patch.object(msvc, "_pool", _Pool(rows=[_row("nl:a"), _row("nl:b")]))
        with caplog.at_level(logging.WARNING):
            await load_db_detectors()
        assert [r for r in caplog.records if "were not loaded" in r.getMessage()] == []

    async def test_the_good_rows_still_load_alongside_a_bad_one(self, mocker):
        mocker.patch.object(
            msvc, "_pool", _Pool(rows=[_row("nl:bad", "{not json"), _row("nl:good")]),
        )
        active, _ = await load_db_detectors()
        assert len(active) == 1, "one malformed row must not drop the whole table"
