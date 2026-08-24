"""The first question of an incident is "what changed?", and a failed read answered "nothing".

`_hierarchy_context` builds the coordinator's memory block. Since pass 46 its **episodes** half
has been honest: a failed recall appends an explicit *"## Memory unavailable — this is not the
same as there being none"* block and sets `memory_degraded`. Four lines below, in the same
function, its **changes** half did the opposite.

Measured 2026-08-24, at the layer that actually fails in production:

    KG query FAILED         -> changes()=[]   block=''
    cluster genuinely calm  -> changes()=[]   block=''
    KG not running          ->                block=''      (no warning at all)

`kg.changes` swallowed every exception and returned `[]`; `recent_changes_block` rendered that
as `""`; and the caller renders `""` by omitting the whole "## Recent cluster changes (last
15m)" section. An omitted section is not neutral here — **it is the exact shape a calm cluster
makes**, so a Postgres outage reached the model as evidence that nothing had changed, in the
one block whose job is to stop it ruling out a recent change.

`episodes.MemoryUnavailable` already carried this argument in its own docstring for the sibling
lookup. `kg.KGUnavailable` is its twin.

Deliberately unchanged: **the write path still swallows.** `upsert_entity`, `open_edge`,
`close_edge` and the ingest helpers keep returning `None` on failure — a failed observation
write must never kill a turn, and nothing downstream reads their return value as a fact. The
split is not read-vs-write for its own sake: it is that a read feeding a prompt has a silence
the model interprets.

Also deliberately unchanged: **no pool is still `[]`.** A KG that was never started is a
configuration state, not a failed query — the same split `episodes.recall_episodes` makes.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.memory import episodes, kg


class DeadPool:
    async def fetch(self, *_a):
        raise OSError("connection refused to postgres:5432")

    async def fetchrow(self, *_a):
        raise OSError("connection refused to postgres:5432")


class CalmPool:
    async def fetch(self, *_a):
        return []

    async def fetchrow(self, *_a):
        return None


@pytest.fixture(autouse=True)
def _restore_kg_pool():
    before = kg._pool
    yield
    kg._pool = before


# ── 1. the graph read separates its two answers ───────────────────────────────────────────────


class TestTheReadSaysWhenItCouldNotRead:
    async def test_a_failed_query_raises(self):
        kg._pool = DeadPool()
        with pytest.raises(kg.KGUnavailable) as caught:
            await kg.changes("c1", 0.0, 60.0)
        assert "could not be read" in str(caught.value)

    async def test_a_calm_window_is_still_empty(self):
        """Vacuity guard: a read that always raised would pass the test above."""
        kg._pool = CalmPool()
        assert await kg.changes("c1", 0.0, 60.0) == []

    async def test_no_pool_is_a_configuration_state_not_a_failure(self):
        kg._pool = None
        assert await kg.changes("c1", 0.0, 60.0) == []

    async def test_the_block_propagates_instead_of_rendering_empty(self):
        kg._pool = DeadPool()
        with pytest.raises(kg.KGUnavailable):
            await kg.recent_changes_block("c1", minutes=15)

    async def test_the_block_is_empty_for_a_calm_cluster(self):
        kg._pool = CalmPool()
        assert await kg.recent_changes_block("c1", minutes=15) == ""


class TestTheWritePathStillSwallows:
    """The rule this file is an exception to, pinned so the exception stays narrow."""

    async def test_upsert_entity_still_returns_none(self):
        kg._pool = DeadPool()
        assert await kg.upsert_entity("c1", "Pod", "web-1") is None

    async def test_open_edge_still_returns_none(self):
        kg._pool = DeadPool()
        assert await kg.open_edge("c1", "s", "runs_on", "d") is None

    async def test_close_edge_still_returns_none(self):
        kg._pool = DeadPool()
        assert await kg.close_edge("c1", "s", "runs_on") is None


# ── 2. what the model is told ─────────────────────────────────────────────────────────────────


def _ctx(mocker, *, changes_block, recall=None):
    """`_hierarchy_context` with the episodes half held steady and the changes half varied."""
    from app.agent.nodes import memory_loader
    from app.memory import service

    mocker.patch.object(service, "memory_active", lambda: True)
    mocker.patch.object(settings, "MEMORY_SUMMARY_TREE", False)
    mocker.patch.object(episodes, "recall_episodes",
                        recall or mocker.AsyncMock(return_value=[]))
    mocker.patch.object(kg, "recent_changes_block", changes_block)
    return memory_loader


def _state():
    return {"messages": [type("M", (), {"content": "payments crashlooping"})()]}


class TestTheModelIsToldTheChangeLogFailed:
    async def test_a_failed_change_read_becomes_an_explicit_block(self, mocker):
        ml = _ctx(mocker, changes_block=mocker.AsyncMock(
            side_effect=kg.KGUnavailable("the cluster change log could not be read: pg down")))
        out = await ml._hierarchy_context(_state())
        assert "Recent changes unavailable" in out
        assert "not the same as nothing having changed" in out

    async def test_it_names_the_inference_it_is_preventing(self, mocker):
        """A "could not read" line the model can read past is not a fix. The block has to deny
        the specific conclusion: that a recent change can be ruled out as the cause."""
        ml = _ctx(mocker, changes_block=mocker.AsyncMock(side_effect=kg.KGUnavailable("x")))
        out = await ml._hierarchy_context(_state())
        assert "do not rule out a recent change" in out.lower()

    async def test_a_calm_cluster_still_says_nothing(self, mocker):
        """The fix must not make every quiet cluster shout — the sibling rule, kept."""
        ml = _ctx(mocker, changes_block=mocker.AsyncMock(return_value=""))
        out = await ml._hierarchy_context(_state())
        assert "Recent changes unavailable" not in out
        assert "Recent cluster changes" not in out

    async def test_the_two_are_now_distinguishable(self, mocker):
        """The whole defect in one assertion: these two produced identical prompts."""
        ml = _ctx(mocker, changes_block=mocker.AsyncMock(return_value=""))
        calm = await ml._hierarchy_context(_state())
        ml = _ctx(mocker, changes_block=mocker.AsyncMock(side_effect=kg.KGUnavailable("x")))
        broken = await ml._hierarchy_context(_state())
        assert calm != broken

    async def test_real_changes_still_render(self, mocker):
        ml = _ctx(mocker, changes_block=mocker.AsyncMock(
            return_value="14:02:11 opened Pod/s/web-1 -runs_on-> Node//worker-2"))
        out = await ml._hierarchy_context(_state())
        assert "Recent cluster changes (last 15m)" in out
        assert "web-1 -runs_on-> Node//worker-2" in out
        assert "Recent changes unavailable" not in out


class TestTheTurnSurvivesIt:
    async def test_the_failure_never_escapes_the_node(self, mocker):
        """(a) of the invariant `TestAMemoryOutageIsVisibleButNotFatal` states: an investigation
        without memory beats no investigation. Raising out of here would break the turn."""
        ml = _ctx(mocker, changes_block=mocker.AsyncMock(side_effect=kg.KGUnavailable("pg down")))
        out = await ml._hierarchy_context(_state())
        assert isinstance(out, str) and out

    async def test_both_halves_can_fail_at_once(self, mocker):
        """One Postgres serves both. The realistic outage takes out recall and changes together,
        and each must still say so in its own words."""
        ml = _ctx(
            mocker,
            changes_block=mocker.AsyncMock(side_effect=kg.KGUnavailable("pg down")),
            recall=mocker.AsyncMock(side_effect=episodes.MemoryUnavailable("pg down")),
        )
        out = await ml._hierarchy_context(_state())
        assert "Memory unavailable" in out
        assert "Recent changes unavailable" in out

    async def test_the_outage_is_recorded_as_degraded(self, mocker):
        """`memory_degraded` is the operator-side half — the log line an on-call reads after the
        model has already answered."""
        ml = _ctx(mocker, changes_block=mocker.AsyncMock(side_effect=kg.KGUnavailable("pg down")))
        log = mocker.patch.object(ml, "logger")
        await ml._hierarchy_context(_state())
        joined = " ".join(str(c) for c in log.info.call_args_list)
        assert "degraded=True" in joined
