"""One dead predicate must not delete the detector's live ones.

The load-time liveness gate (2026-08-25) refused any stored detector carrying a predicate that
could never fire, and refused the whole row. On the F3 soak cluster that turned out to delete
evidence rather than noise:

    nl:soak-replicas-short
        trend: kube_deployment_status_replicas{deployment="your-deployment-name"}  ← dead
        watch: {kind: Pod, status_regex: "^Running$"}                              ← very much alive

`^Running$` matches every healthy pod on the cluster. It is a false positive by construction, and
the lane it lives on — `f3-shadow-soak` — has `false_positive_rate` as its primary endpoint.
Refusing the row for the template therefore removed a false-positive source and moved the
measured rate **toward** the pre-registered direction. A gate that improves the result by
deleting the evidence against it is the worst possible failure mode for a gate written to make
dead predicates visible.

So the refusal is per predicate. The dead one is dropped and logged; the live ones load; the
detector records that it is not the detector the store holds, because whoever reads its firing
count is looking at a predicate that did not run.

A row with NO live predicate left is still refused whole — that is a dead detector, and there is
nothing to keep.
"""
from __future__ import annotations

import json

import pytest

from app.detectors.engine import load_db_detectors
from app.detectors.models import DetectBlock

TEMPLATE_TREND = {
    "metric": 'kube_deployment_status_replicas{deployment="your-deployment-name"}',
    "min_r2": 0.8, "direction": "falling", "threshold": 0,
    "window_minutes": 5, "fire_if_eta_within_minutes": 5, "projection_horizon_minutes": 10,
}
LIVE_TREND = {
    "metric": "kube_job_status_failed",
    "min_r2": 0.8, "direction": "rising", "threshold": 0,
    "window_minutes": 5, "fire_if_eta_within_minutes": 5, "projection_horizon_minutes": 10,
}
LIVE_WATCH = {"kind": "Pod", "status_regex": "^Running$"}
DEAD_WATCH = {"kind": "Pod"}          # a Pod predicate with no status_regex can match nothing


class _Pool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, _sql, *_args):
        return [{"name": n, "predicate": json.dumps(p), "status": s} for n, p, s in self._rows]


@pytest.fixture
def store(monkeypatch):
    def _install(rows):
        from app.memory import service
        monkeypatch.setattr(service, "_pool", _Pool(rows))
    return _install


async def _shadow(store_rows) -> dict[str, DetectBlock]:
    _active, shadow = await load_db_detectors("f3-shadow-soak-r3")
    return {b.playbook: b for b in shadow}


class TestOneDeadPredicateDoesNotDeleteTheLiveOnes:
    @pytest.mark.asyncio
    async def test_the_detector_still_loads(self, store):
        store([("nl:soak-replicas-short",
                {"watch_predicates": [LIVE_WATCH], "trend_predicates": [TEMPLATE_TREND]},
                "shadow")])
        assert "nl:soak-replicas-short" in await _shadow(store)

    @pytest.mark.asyncio
    async def test_the_live_watch_predicate_survives(self, store):
        store([("d", {"watch_predicates": [LIVE_WATCH], "trend_predicates": [TEMPLATE_TREND]},
                "shadow")])
        block = (await _shadow(store))["d"]
        assert len(block.watch_predicates) == 1

    @pytest.mark.asyncio
    async def test_the_dead_trend_predicate_is_gone(self, store):
        store([("d", {"watch_predicates": [LIVE_WATCH], "trend_predicates": [TEMPLATE_TREND]},
                "shadow")])
        assert (await _shadow(store))["d"].trend_predicates == ()

    @pytest.mark.asyncio
    async def test_the_block_records_what_was_dropped_and_why(self, store):
        store([("d", {"watch_predicates": [LIVE_WATCH], "trend_predicates": [TEMPLATE_TREND]},
                "shadow")])
        dropped = (await _shadow(store))["d"].dropped_predicates
        assert len(dropped) == 1
        assert "your-deployment-name" in dropped[0]

    @pytest.mark.asyncio
    async def test_a_dead_watch_predicate_beside_a_live_trend_is_the_same_deal(self, store):
        store([("d", {"watch_predicates": [DEAD_WATCH], "trend_predicates": [LIVE_TREND]},
                "shadow")])
        block = (await _shadow(store))["d"]
        assert block.watch_predicates == ()
        assert len(block.trend_predicates) == 1
        assert block.dropped_predicates

    @pytest.mark.asyncio
    async def test_only_the_dead_one_of_two_trend_predicates_is_dropped(self, store):
        store([("d", {"trend_predicates": [TEMPLATE_TREND, LIVE_TREND]}, "shadow")])
        block = (await _shadow(store))["d"]
        assert len(block.trend_predicates) == 1
        assert block.trend_predicates[0].metric == "kube_job_status_failed"

    @pytest.mark.asyncio
    async def test_the_refused_predicate_is_not_evaluated(self, store):
        """Kept separate from `dropped_predicates` being populated: the record and the effect
        are two claims, and the one that matters is that nothing evaluates the dead half."""
        store([("d", {"watch_predicates": [LIVE_WATCH], "trend_predicates": [TEMPLATE_TREND]},
                "shadow")])
        block = (await _shadow(store))["d"]
        assert all("your-deployment-name" not in tp.metric for tp in block.trend_predicates)


class TestADetectorWithNothingLiveLeftIsStillRefused:
    @pytest.mark.asyncio
    async def test_a_trend_only_template_detector_does_not_load(self, store):
        store([("nl:soak-pod-count-swings", {"trend_predicates": [TEMPLATE_TREND]}, "shadow")])
        assert "nl:soak-pod-count-swings" not in await _shadow(store)

    @pytest.mark.asyncio
    async def test_a_watch_only_dead_detector_does_not_load(self, store):
        store([("d", {"watch_predicates": [DEAD_WATCH]}, "shadow")])
        assert "d" not in await _shadow(store)

    @pytest.mark.asyncio
    async def test_every_predicate_dead_across_both_kinds_does_not_load(self, store):
        store([("d", {"watch_predicates": [DEAD_WATCH], "trend_predicates": [TEMPLATE_TREND]},
                "shadow")])
        assert "d" not in await _shadow(store)


class TestAFullyLivedDetectorIsUntouched:
    @pytest.mark.asyncio
    async def test_it_loads_with_every_predicate(self, store):
        store([("d", {"watch_predicates": [LIVE_WATCH], "trend_predicates": [LIVE_TREND]},
                "shadow")])
        block = (await _shadow(store))["d"]
        assert len(block.watch_predicates) == 1 and len(block.trend_predicates) == 1

    @pytest.mark.asyncio
    async def test_it_records_nothing_as_dropped(self, store):
        store([("d", {"watch_predicates": [LIVE_WATCH]}, "shadow")])
        assert (await _shadow(store))["d"].dropped_predicates == ()

    def test_a_playbook_compiled_block_defaults_to_nothing_dropped(self):
        """Only the DB loader ever fills this. A playbook file is not partially loaded."""
        assert DetectBlock(playbook="p").dropped_predicates == ()


class TestTheSoakRowsSpecifically:
    """The eight rows this gate was written for, with the real stored predicates."""

    @pytest.mark.asyncio
    async def test_replicas_short_loads_because_its_watch_predicate_is_live(self, store):
        store([("nl:soak-replicas-short",
                {"watch_predicates": [{"kind": "Pod", "status_regex": "^Running$"}],
                 "trend_predicates": [TEMPLATE_TREND]}, "shadow")])
        loaded = await _shadow(store)
        assert "nl:soak-replicas-short" in loaded
        assert loaded["nl:soak-replicas-short"].watch_predicates[0].status_regex.pattern \
            == "^Running$"

    @pytest.mark.asyncio
    async def test_pod_count_swings_does_not_because_it_has_no_watch_predicate(self, store):
        swings = dict(TEMPLATE_TREND,
                      metric='kube_deployment_status_replicas{deployment="your_service_name"}')
        store([("nl:soak-pod-count-swings", {"trend_predicates": [swings]}, "shadow")])
        assert "nl:soak-pod-count-swings" not in await _shadow(store)

    @pytest.mark.asyncio
    async def test_the_two_rows_are_not_the_same_case(self, store):
        """Both were "refused at load" yesterday. Only one of them should have been."""
        swings = dict(TEMPLATE_TREND,
                      metric='kube_deployment_status_replicas{deployment="your_service_name"}')
        store([
            ("nl:soak-replicas-short",
             {"watch_predicates": [{"kind": "Pod", "status_regex": "^Running$"}],
              "trend_predicates": [TEMPLATE_TREND]}, "shadow"),
            ("nl:soak-pod-count-swings", {"trend_predicates": [swings]}, "shadow"),
        ])
        loaded = await _shadow(store)
        assert set(loaded) == {"nl:soak-replicas-short"}
