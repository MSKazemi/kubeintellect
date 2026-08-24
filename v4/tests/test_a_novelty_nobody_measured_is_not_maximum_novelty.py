"""A novelty score that never ran was stored as the strongest novelty claim the module makes.

`episodes.surprise` is a KG-novelty proxy in [0,1] where **1.0 means nothing similar has ever
been seen**. `_surprise_novelty` returned `1.0` from all three paths where it did not measure
anything:

* the `similarity()` query raised — on a database without the `pg_trgm` extension that is
  *every* write, so the whole column fills with a maximum no measurement produced;
* there was no pool to ask;
* the episode text was empty, so there was nothing to compare.

Measured 2026-08-24: with `similarity()` raising, `write_episode` inserts `surprise=1.0` while
the module logs `surprise scoring failed`. One row, two contradictory statements — and the row
is the audit record.

`None` is not a hedge here, it is the value the schema already reserves: `episodes.surprise` is
a nullable REAL, and NULL already means "not scored" for every pre-P6 row. The flag-off path
writes NULL for exactly this reason. Only the failure path fabricated a number.

Deliberately unchanged: **fail-open**. A failed score must never drop a write — that was right
before and is right now. What changes is only that the stored record stops claiming a
measurement happened. The gate reads `surprise is not None` first, so an unmeasured episode is
never gated on a check that failed.
"""
from __future__ import annotations

import pytest

from app.memory import episodes


class _Pool:
    """Answers the novelty query however the test asks; everything else is the INSERT."""

    def __init__(self, top=None, novelty_exc: Exception | None = None):
        self.top = top
        self.novelty_exc = novelty_exc
        self.calls: list[tuple] = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if "max(similarity" in sql:
            if self.novelty_exc:
                raise self.novelty_exc
            return {"top": self.top}
        return {"id": "e1"}


_NO_TRGM = Exception("function similarity(text, unknown) does not exist")


@pytest.fixture(autouse=True)
def _restore_pool():
    """`episodes._pool` is module state and every test here writes it."""
    before = episodes._pool
    yield
    episodes._pool = before


@pytest.fixture
def scored(mocker):
    """Write one episode against a given pool and hand back the INSERT's three P6 columns."""
    mocker.patch.object(episodes.settings, "MEMORY_IMPORTANCE", True)

    async def _write(pool, **kw):
        episodes.init_episodes(pool)
        try:
            eid = await episodes.write_episode(
                cluster_id="c1", trigger_kind=kw.pop("trigger_kind", "detector"),
                summary=kw.pop("summary", "OOMKilled on web-1"),
                outcome=kw.pop("outcome", "resolved"),
                verified=kw.pop("verified", True), **kw)
        finally:
            episodes.close_episodes()
        insert = next((c for c in pool.calls if "INSERT INTO episodes" in c[0]), None)
        return eid, (insert[1][-3:] if insert else None)

    return _write


# ── 1. the scorer stops answering when it did not score ───────────────────────────────────────


class TestTheScorerSaysWhenItDidNotScore:
    async def test_a_failed_query_is_not_maximum_novelty(self):
        episodes.init_episodes(_Pool(novelty_exc=_NO_TRGM))
        assert await episodes._surprise_novelty("c1", "OOMKilled") is None

    async def test_no_pool_is_not_maximum_novelty(self):
        episodes._pool = None
        assert await episodes._surprise_novelty("c1", "OOMKilled") is None

    async def test_empty_text_is_not_maximum_novelty(self):
        episodes.init_episodes(_Pool(top=0.9))
        assert await episodes._surprise_novelty("c1", "   ") is None

    async def test_a_real_score_is_still_a_number(self):
        """Vacuity guard: a scorer that always returned None would pass everything above."""
        episodes.init_episodes(_Pool(top=0.95))
        assert await episodes._surprise_novelty("c1", "OOMKilled") == pytest.approx(0.05)

    async def test_an_empty_table_is_genuinely_novel(self):
        """`top IS NULL` is a *performed* query that found nothing similar. That is a real
        1.0, and it must stay one — otherwise None swallows the answer it is meant to protect."""
        episodes.init_episodes(_Pool(top=None))
        assert await episodes._surprise_novelty("c1", "a brand new symptom") == 1.0

    async def test_the_range_still_holds(self):
        episodes.init_episodes(_Pool(top=1.0))
        assert await episodes._surprise_novelty("c1", "exact duplicate") == 0.0


# ── 2. what reaches the audit record ──────────────────────────────────────────────────────────


class TestTheColumnCarriesTheThirdState:
    async def test_an_unmeasured_episode_stores_null(self, scored):
        eid, cols = await scored(_Pool(novelty_exc=_NO_TRGM))
        assert eid == "e1"
        _importance, surprise, _trust = cols
        assert surprise is None, (
            "the row claims maximum novelty for a score the same write logged as failed")

    async def test_a_measured_episode_still_stores_its_number(self, scored):
        _eid, (_importance, surprise, _trust) = await scored(_Pool(top=0.3))
        assert surprise == pytest.approx(0.7)

    async def test_importance_is_unaffected_by_a_failed_novelty_score(self, scored):
        """The two P6 signals are independent; one failing must not blank the other."""
        _eid, (importance, surprise, _trust) = await scored(_Pool(novelty_exc=_NO_TRGM))
        assert importance is not None and surprise is None

    async def test_the_warning_names_the_consequence(self, scored, caplog):
        """A log line that only says "failed" leaves the reader to guess what was stored."""
        with caplog.at_level("WARNING"):
            await scored(_Pool(novelty_exc=_NO_TRGM))
        text = caplog.text
        assert "surprise scoring failed" in text
        assert "NULL" in text and "not as fully novel" in text


# ── 3. fail-open is unchanged ─────────────────────────────────────────────────────────────────


class TestAFailedScoreStillNeverBlocksAWrite:
    async def test_a_low_value_write_survives_an_unmeasured_score(self, scored):
        """The case that would regress if `None` were read as "below the floor": unverified +
        report_only is exactly what the gate drops when the score IS low."""
        eid, cols = await scored(
            _Pool(novelty_exc=_NO_TRGM),
            trigger_kind="user_query", summary="pod restarted again",
            outcome="report_only", verified=False)
        assert eid == "e1", "a failed novelty score dropped a write"
        assert cols[1] is None

    async def test_the_gate_still_drops_a_measured_duplicate(self, scored):
        """Vacuity guard the other way: the gate must still be able to fire."""
        eid, cols = await scored(
            _Pool(top=0.99),
            trigger_kind="user_query", summary="pod restarted again",
            outcome="report_only", verified=False)
        assert eid is None and cols is None

    async def test_a_measured_novel_low_value_write_is_kept(self, scored):
        eid, _cols = await scored(
            _Pool(top=0.0),
            trigger_kind="user_query", summary="a brand new symptom",
            outcome="report_only", verified=False)
        assert eid == "e1"

    async def test_a_verified_write_is_kept_either_way(self, scored):
        for pool in (_Pool(top=0.99), _Pool(novelty_exc=_NO_TRGM)):
            eid, _cols = await scored(pool, outcome="resolved", verified=True)
            assert eid == "e1"
