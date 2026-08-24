""""The recorder is off" must never be answered as "that episode does not exist".

`fetch_episode` returned `[]` for three unrelated states — the episode genuinely has no rows,
the recorder was never started (`_pool is None`), and the query failed — and the replay endpoint
turned all three into the one answer that is a positive claim about the world:

    GET /v1/episodes/<a real id>/replay  →  404  {"detail": "no recorded episode '<id>'"}

Measured with `flight_recorder._pool = None`. `kq replay` renders that as *"No recorded episode"*
and exits 1. This is the audit surface: an operator asking for the decision log of the incident
they are living through was told it was never recorded, because the recorder happened to be off.

The rule these tests hold: an empty list means no rows, and nothing else.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints import episodes
from app.db import flight_recorder as fr
from app.digest import postmortem

class _Pool:
    """An asyncpg pool whose fetch either works or fails like an unreachable database."""

    def __init__(self, rows=None, exc: Exception | None = None):
        self.rows = rows if rows is not None else []
        self.exc = exc

    async def fetch(self, *_a, **_k):
        if self.exc:
            raise self.exc
        return self.rows

    async def fetchrow(self, *_a, **_k):
        """The chain-anchor read. A double without it raised AttributeError, which
        `head_verdict` reports as an unreadable anchor — turning the 404 below into a 503."""
        if self.exc:
            raise self.exc
        return None


@pytest.fixture
def no_pool(mocker):
    mocker.patch.object(fr, "_pool", None)


@pytest.fixture
def dead_pool(mocker):
    mocker.patch.object(fr, "_pool", _Pool(exc=OSError("connection refused")))


@pytest.fixture
def empty_pool(mocker):
    mocker.patch.object(fr, "_pool", _Pool(rows=[]))


@pytest.mark.asyncio
class TestTheRecorderSaysWhenItCannotAnswer:
    async def test_a_stopped_recorder_raises(self, no_pool):
        with pytest.raises(fr.RecorderUnavailable):
            await fr.fetch_episode("ep-1")

    async def test_an_unreachable_database_raises(self, dead_pool):
        with pytest.raises(fr.RecorderUnavailable) as caught:
            await fr.fetch_episode("ep-1")
        assert "connection refused" in str(caught.value)

    async def test_a_real_empty_episode_still_returns_empty(self, empty_pool):
        """Vacuity guard: without it, "raises when unreadable" would pass for a function that
        always raises, and the genuinely-empty case is the common one."""
        assert await fr.fetch_episode("ep-1") == []


@pytest.mark.asyncio
class TestTheEndpointDoesNotClaimAbsence:
    async def test_it_answers_503_not_404(self, no_pool):
        with pytest.raises(HTTPException) as caught:
            await episodes.replay_episode("ep-1")
        assert caught.value.status_code == 503, "404 is a positive claim that it does not exist"

    async def test_and_says_which_it_is(self, no_pool):
        with pytest.raises(HTTPException) as caught:
            await episodes.replay_episode("ep-1")
        detail = str(caught.value.detail)
        assert "not the same as" in detail
        assert "ep-1" in detail

    async def test_a_genuinely_missing_episode_is_still_404(self, empty_pool):
        """The other direction: 404 must keep working, or the fix has only moved the lie."""
        with pytest.raises(HTTPException) as caught:
            await episodes.replay_episode("ep-1")
        assert caught.value.status_code == 404
        assert "no recorded episode" in str(caught.value.detail)

    async def test_an_unreachable_database_is_503_too(self, dead_pool):
        with pytest.raises(HTTPException) as caught:
            await episodes.replay_episode("ep-1")
        assert caught.value.status_code == 503


@pytest.mark.asyncio
class TestThePostmortemSaysWhyItIsEmpty:
    async def test_it_flags_the_recorder(self, no_pool):
        pm = await postmortem.build_postmortem("ep-1")
        assert pm["recorder_available"] is False
        assert "NOT the same as" in pm["summary"]

    async def test_a_real_empty_episode_reads_as_empty(self, empty_pool):
        pm = await postmortem.build_postmortem("ep-1")
        assert pm["recorder_available"] is True
        assert pm["summary"] == "No recorded events for this episode."

    async def test_the_two_summaries_are_not_the_same_sentence(self, mocker):
        """They used to be: "No recorded events for this episode (or recorder unavailable)."
        left every reader to guess which of the two they had."""
        mocker.patch.object(fr, "_pool", _Pool(rows=[]))
        empty = (await postmortem.build_postmortem("ep-1"))["summary"]
        mocker.patch.object(fr, "_pool", None)
        broken = (await postmortem.build_postmortem("ep-1"))["summary"]
        assert empty != broken

    async def test_the_chain_verdict_stays_false_when_nothing_was_read(self, no_pool):
        """`verify_chain([])` is True — "intact" over nothing. The unavailable path must never
        reach it."""
        pm = await postmortem.build_postmortem("ep-1")
        assert pm["chain_valid"] is False


class TestNoCallerTreatsUnavailableAsEmpty:
    """A guard, not a behaviour test: every production caller of `fetch_episode` must handle
    `RecorderUnavailable`, or it will render the outage as an absence again."""

    def test_every_caller_handles_it(self):
        import ast
        import pathlib

        root = pathlib.Path(episodes.__file__).parents[4]        # …/packages/kubeintellect-server
        callers = []
        for path in sorted((root / "app").rglob("*.py")):
            src = path.read_text(encoding="utf-8")
            if "fetch_episode(" not in src or path.name == "flight_recorder.py":
                continue
            callers.append((path, src, ast.parse(src)))

        assert len(callers) >= 2, f"the scan found only {[str(p) for p, _, _ in callers]}"
        for path, src, _tree in callers:
            assert "RecorderUnavailable" in src, (
                f"{path.name} calls fetch_episode without handling RecorderUnavailable — an "
                f"outage there is rendered as 'this episode has no records'"
            )
