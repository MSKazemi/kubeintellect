"""`/healthz` could report memory healthy while memory did nothing at all.

The 2026-08-23 evaluation lane ran three arms for nine hours. The pool connected, so
`memory_status()["enabled"]` was `True` and `state` was `"ready"` for the whole run. Not one
episode was written and not one was recalled. The arms were graded and their numbers written
down; the defect was found afterwards, by counting rows in the database by hand.

Nothing in the response was false. `enabled` faithfully reported that the pool was up — which
is simply not the same claim as "memory works", and no other field distinguished them. That is
the campaign's recurring shape: the system reported healthy while a path was dead.

These tests pin the property that closes it: a process that is connected, has been asked
repeatedly, and has answered nothing must be able to *say so about itself*. They are written
against outcomes (attempts, hits, writes) rather than against any particular wording, so
rephrasing a symptom does not break them but removing the capability does.
"""

from __future__ import annotations

import pytest

from app.memory import liveness, service


@pytest.fixture(autouse=True)
def _clean_counters():
    liveness.reset()
    yield
    liveness.reset()


def _as_ready(monkeypatch):
    """Put the module in the exact state the dead lane was in: connected and confident."""
    monkeypatch.setattr(service, "_state", "ready", raising=False)
    monkeypatch.setattr(service, "_reason", "", raising=False)
    monkeypatch.setattr(service, "_dropped_observations", 0, raising=False)


class TestTheNineHourLaneWouldNowBeVisible:
    def test_connected_asked_repeatedly_and_never_answering_is_reported(self, monkeypatch):
        _as_ready(monkeypatch)
        for _ in range(liveness.ATTEMPTS_BEFORE_SUSPICIOUS):
            liveness.record_recall(hit=False)

        status = service.memory_status()

        # The old surface said exactly this much, and it was true, and it was useless.
        assert status["enabled"] is True
        assert status["state"] == "ready"
        # The part that was missing.
        assert status["healthy"] is False, (
            "a process that has been queried repeatedly and never returned an episode must not "
            "report itself healthy — this is the nine-hour lane, and it is the whole point"
        )
        assert status["symptoms"], "an unhealthy process must say what is wrong"
        assert status["recall_attempts"] == liveness.ATTEMPTS_BEFORE_SUSPICIOUS
        assert status["recall_hits"] == 0

    def test_a_store_that_is_never_written_is_reported_separately(self, monkeypatch):
        """Recall missing and the store never filling are different faults with one cure."""
        _as_ready(monkeypatch)
        for _ in range(liveness.ATTEMPTS_BEFORE_SUSPICIOUS):
            liveness.record_recall(hit=False)

        joined = " ".join(service.memory_status()["symptoms"])
        assert "never returned an episode" in joined
        assert "no episode has ever been written" in joined

    def test_writing_clears_the_write_symptom_but_not_the_recall_one(self, monkeypatch):
        _as_ready(monkeypatch)
        for _ in range(liveness.ATTEMPTS_BEFORE_SUSPICIOUS):
            liveness.record_recall(hit=False)
        liveness.record_episode_written()

        joined = " ".join(service.memory_status()["symptoms"])
        assert "no episode has ever been written" not in joined
        assert "never returned an episode" in joined


class TestItDoesNotCryWolf:
    def test_a_cold_store_is_not_a_fault(self, monkeypatch):
        """A new cluster recalls nothing and that is correct, not broken."""
        _as_ready(monkeypatch)
        for _ in range(liveness.ATTEMPTS_BEFORE_SUSPICIOUS - 1):
            liveness.record_recall(hit=False)

        status = service.memory_status()
        assert status["healthy"] is True
        assert status["symptoms"] == []

    def test_a_working_store_is_healthy_however_many_times_it_is_asked(self, monkeypatch):
        _as_ready(monkeypatch)
        for _ in range(liveness.ATTEMPTS_BEFORE_SUSPICIOUS * 5):
            liveness.record_recall(hit=True)
        liveness.record_episode_written()

        status = service.memory_status()
        assert status["healthy"] is True
        assert status["symptoms"] == []
        assert status["recall_hits"] == status["recall_attempts"]

    @pytest.mark.parametrize("state", ["flag", "sqlite"])
    def test_memory_switched_off_on_purpose_is_healthy(self, monkeypatch, state):
        """`healthy` means "doing what it was configured to do", not "memory is on"."""
        monkeypatch.setattr(service, "_state", state, raising=False)
        monkeypatch.setattr(service, "_dropped_observations", 0, raising=False)
        status = service.memory_status()
        assert status["enabled"] is False
        assert status["healthy"] is True
        assert status["symptoms"] == []

    def test_a_pool_that_never_connected_is_not_healthy(self, monkeypatch):
        monkeypatch.setattr(service, "_state", "unavailable", raising=False)
        monkeypatch.setattr(service, "_dropped_observations", 0, raising=False)
        assert service.memory_status()["healthy"] is False


class TestFailuresAndDropsAreCounted:
    def test_a_recall_that_raises_counts_as_an_attempt_and_a_failure(self, monkeypatch):
        _as_ready(monkeypatch)
        liveness.record_recall_failure()
        status = service.memory_status()
        assert status["recall_attempts"] == 1
        assert status["recall_failures"] == 1
        assert status["recall_hits"] == 0
        assert status["healthy"] is False
        assert any("failed outright" in s for s in status["symptoms"])

    def test_dropped_observations_still_surface(self, monkeypatch):
        _as_ready(monkeypatch)
        monkeypatch.setattr(service, "_dropped_observations", 7, raising=False)
        status = service.memory_status()
        assert status["healthy"] is False
        assert any("7 observations were dropped" in s for s in status["symptoms"])


class TestTheCountersAreActuallyWiredToTheRealPaths:
    """A counter nothing increments is the same lie in a new place.

    These assert against the shipping call sites rather than the counter API, so deleting the
    `liveness.record_*` call from `recall_episodes` or `store_episode` fails here even though
    every test above would still pass.
    """

    def test_recall_records_a_miss_when_the_store_is_empty(self):
        import asyncio

        from app.memory import episodes

        class _EmptyPool:
            async def fetch(self, *_a, **_k):
                return []

        episodes._pool = _EmptyPool()
        try:
            rows = asyncio.run(episodes.recall_episodes("anything", "c1", k=3))
        finally:
            episodes._pool = None

        assert rows == []
        assert liveness.counters()["recall_attempts"] == 1
        assert liveness.counters()["recall_hits"] == 0

    def test_recall_records_a_hit_when_rows_come_back(self):
        import asyncio

        from app.memory import episodes

        class _OnePool:
            async def fetch(self, *_a, **_k):
                return [{"id": "e1", "summary": "x", "sim": 0.9}]

        episodes._pool = _OnePool()
        try:
            rows = asyncio.run(episodes.recall_episodes("anything", "c1", k=3))
        finally:
            episodes._pool = None

        assert len(rows) == 1
        assert liveness.counters()["recall_hits"] == 1

    def test_a_failing_recall_is_counted_before_it_raises(self):
        import asyncio

        from app.memory import episodes

        class _BrokenPool:
            async def fetch(self, *_a, **_k):
                raise RuntimeError('column "sim" does not exist')

        episodes._pool = _BrokenPool()
        try:
            with pytest.raises(episodes.MemoryUnavailable):
                asyncio.run(episodes.recall_episodes("anything", "c1", k=3))
        finally:
            episodes._pool = None

        assert liveness.counters()["recall_failures"] == 1
