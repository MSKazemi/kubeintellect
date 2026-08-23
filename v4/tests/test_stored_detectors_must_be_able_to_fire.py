r"""A detector stored in the DB must be able to fire before it is loaded or promoted.

`validate_detect_block` (added when the NL-authoring path was found to accept four kinds of
permanent no-op) guards exactly one door. It is not the only way a row reaches the `detectors`
table: `memory/consolidation.py` writes there too, `review.promote_candidate` only flips
`status` without re-reading the predicate, and any row stored before that gate existed is still
sitting in the table. So the check has to live where every stored detector actually becomes
live -- `load_db_detectors` -- and at the moment a human acts on one.

Before this file, three dead rows loaded as real detectors:

    active : ['nl:dead-kind', 'nl:dead-pod', 'nl:live']
    shadow : ['nl:dead-114']

A dead SHADOW row is the worst of them. Shadow detectors exist to accrue precision before a
human promotes them, and a detector that cannot fire accrues zero firings -- which is exactly
what a detector watching a condition that never occurred looks like. The reviewer is then shown
a clean record and a promote button, and the endpoint answers `status: active` about something
that will never match anything.
"""
from __future__ import annotations

import json

import pytest

from app.detectors import review
from app.detectors.engine import load_db_detectors
from app.memory import service

_LIVE = {"watch_predicates": [{"kind": "Pod", "status_regex": "^OOMKilled$"}]}
_DEAD = {
    # #114's exact mistake — a space inside an anchored alternation
    "dead-114": {"watch_predicates": [
        {"kind": "Event",
         "reason_regex": "^(FailedGetResourceMetric | FailedComputeMetricsReplicas)$"}]},
    # a kind the engine's matches() has never handled
    "dead-kind": {"watch_predicates": [{"kind": "Deployment", "status_regex": "^Stuck$"}]},
    # Pod with nothing to test
    "dead-pod": {"watch_predicates": [{"kind": "Pod"}]},
}


def _pool_with(rows):
    class FakePool:
        async def fetch(self, _sql, *_a):
            return rows

        async def fetchrow(self, _sql, *args):
            wanted = args[-1]
            return next((r for r in rows if r["name"] == wanted), None)

        async def execute(self, _sql, *_a):
            return "UPDATE 1"

    return FakePool()


def _row(name, block, status):
    return {"name": name, "predicate": json.dumps(block), "status": status}


class TestTheLoadPath:
    @pytest.mark.parametrize("name", sorted(_DEAD))
    @pytest.mark.parametrize("status", ["active", "shadow"])
    async def test_a_dead_row_is_not_loaded(self, mocker, name, status):
        mocker.patch.object(service, "_pool", _pool_with([_row(name, _DEAD[name], status)]))
        active, shadow = await load_db_detectors()
        assert active == () and shadow == (), (
            f"{name} compiles but can never match, and was loaded as a {status} detector"
        )

    async def test_a_live_row_still_loads(self, mocker):
        mocker.patch.object(service, "_pool", _pool_with([
            _row("nl:live", _LIVE, "active"),
            _row("nl:live-shadow", _LIVE, "shadow"),
        ]))
        active, shadow = await load_db_detectors()
        assert [b.playbook for b in active] == ["nl:live"]
        assert [b.playbook for b in shadow] == ["nl:live-shadow"]

    async def test_one_dead_row_does_not_take_the_live_ones_with_it(self, mocker):
        """The filter must be per-row: a bad candidate cannot cost the cluster its coverage."""
        mocker.patch.object(service, "_pool", _pool_with([
            _row("nl:dead", _DEAD["dead-114"], "active"),
            _row("nl:live", _LIVE, "active"),
        ]))
        active, _ = await load_db_detectors()
        assert [b.playbook for b in active] == ["nl:live"]


class TestThePromotionGate:
    @pytest.mark.parametrize("name", sorted(_DEAD))
    async def test_promoting_a_dead_candidate_is_refused(self, mocker, name):
        mocker.patch.object(service, "_pool", _pool_with([_row(name, _DEAD[name], "shadow")]))
        with pytest.raises(review.DetectorCannotFire) as exc:
            await review.promote_candidate(name, reviewer="mohsen")
        assert "can never fire" in str(exc.value)

    async def test_promoting_a_live_candidate_still_works(self, mocker):
        mocker.patch.object(service, "_pool", _pool_with([_row("nl:live", _LIVE, "shadow")]))
        assert await review.promote_candidate("nl:live", reviewer="mohsen") is True

    async def test_an_unreadable_row_is_not_called_dead(self, mocker):
        """Refusing on a store failure would be the same mistake in the other direction —
        `DetectorStoreUnavailable` exists in this module for exactly that reason."""
        class Broken:
            async def fetchrow(self, *_a):
                raise RuntimeError("connection reset")

            async def execute(self, *_a):
                return "UPDATE 1"

        mocker.patch.object(service, "_pool", Broken())
        assert await review.promote_candidate("nl:whatever", reviewer="mohsen") is True

    async def test_a_missing_row_is_still_a_404_not_a_409(self, mocker):
        """Not-found and cannot-fire are different answers and must stay different."""
        class Empty:
            async def fetchrow(self, *_a):
                return None

            async def execute(self, *_a):
                return "UPDATE 0"

        mocker.patch.object(service, "_pool", Empty())
        assert await review.promote_candidate("nl:ghost", reviewer="mohsen") is False

    async def test_demotion_is_never_blocked(self, mocker):
        """Whatever else is true, an operator must always be able to switch a detector off."""
        mocker.patch.object(service, "_pool",
                            _pool_with([_row("nl:dead", _DEAD["dead-114"], "active")]))
        assert await review.demote_candidate("nl:dead", reviewer="mohsen") is True
