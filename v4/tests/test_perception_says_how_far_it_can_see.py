"""A5 — the sensorium can now be scoped to fit a large cluster, and says so when it is.

`get pods -A --watch` relists the entire cluster on every reconnect, so past a few thousand pods
the honest options are to watch less or to accept counted loss. ADR-020 chose scope as the lever.

Scoping is the dangerous kind of fix: it works by creating a blind spot. A scoped sensorium is
behaving exactly as configured, every stream is connected, Prometheus answers, nothing is shed —
and it still cannot have seen anything outside its namespaces. Reporting `active` with an empty
findings list and *not* saying that would turn a setting into a false all-clear, which is the
failure mode `perception.py` exists to end. So the claims here are:

* the scope actually reaches `kubectl` — one stream per resource per namespace, each named;
* the same setting drives what is watched and what is reported, so the two cannot drift;
* the scope appears as a perception gap, in the one classifier both surfaces read;
* the unscoped default says nothing, because there is nothing to say;
* the queue warns while it is filling, not only after it has already dropped something.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.config import settings
from app.detectors.perception import (
    ACTIVE,
    PerceptionState,
    perception_gaps,
)
from app.sensorium import k8s_watcher
from app.sensorium.k8s_watcher import _watch_specs, watch_namespaces


@pytest.fixture
def scoped(monkeypatch):
    def _set(value: str):
        monkeypatch.setattr(settings, "SENSORIUM_WATCH_NAMESPACES", value, raising=False)
    return _set


def _state(**over) -> PerceptionState:
    base = dict(sensorium=ACTIVE, detectors=20, predictive="off", predictive_detectors=0,
                predictive_error=None, streams=[{"name": "get pods -A", "connected": True}])
    base.update(over)
    return PerceptionState(**base)


class TestTheScopeReachesKubectl:
    def test_unscoped_is_all_namespaces(self, scoped):
        scoped("")
        specs = _watch_specs()
        assert [name for name, _a, _n in specs] == ["get pods -A", "get events -A"]
        for _name, args, _n in specs:
            assert "-A" in args and "-n" not in args

    def test_one_stream_per_resource_per_namespace(self, scoped):
        scoped("prod,payments")
        specs = _watch_specs()
        assert len(specs) == 4
        assert {name for name, _a, _n in specs} == {
            "get pods -n prod", "get pods -n payments",
            "get events -n prod", "get events -n payments",
        }

    def test_the_namespace_is_passed_to_kubectl(self, scoped):
        scoped("prod")
        args = next(a for _n, a, _f in _watch_specs() if "pods" in a)
        assert args[:4] == ["get", "pods", "-n", "prod"]
        assert "-A" not in args
        assert "--watch" in args and "-o" in args

    def test_the_stream_name_carries_the_namespace(self, scoped):
        """`stream_health()` must be able to say WHICH scope failed, not just 'get pods'."""
        scoped("prod,payments")
        names = [name for name, _a, _n in _watch_specs()]
        assert len(set(names)) == len(names), "two streams sharing a name overwrite each other"

    def test_whitespace_and_empties_are_tolerated(self, scoped):
        scoped(" prod , , payments ")
        assert watch_namespaces() == ("prod", "payments")

    def test_the_normaliser_still_matches_the_resource(self, scoped):
        scoped("prod")
        for name, _args, normalise in _watch_specs():
            expected = "_pod_observation" if "pods" in name else "_event_observation"
            assert normalise.__name__ == expected


class TestTheReportedScopeIsTheWatchedScope:
    def test_one_setting_drives_both(self, scoped):
        """A second source of truth here would let the product claim a scope it does not watch."""
        scoped("prod")
        assert watch_namespaces() == ("prod",)
        assert all("-n" in args for _n, args, _f in _watch_specs())

    def test_perception_reads_the_same_function(self):
        import inspect
        src = inspect.getsource(
            __import__("app.detectors.perception", fromlist=["x"]).perception_state)
        assert "watch_namespaces" in src


class TestAScopedSensoriumSaysSo:
    def test_the_scope_is_a_gap(self):
        gaps = perception_gaps(_state(watch_namespaces=("prod", "payments")))
        assert any("watches only 2 namespace(s)" in g for g in gaps)
        assert any("prod, payments" in g for g in gaps)

    def test_the_gap_says_an_empty_list_is_not_an_all_clear(self):
        """The operator reading the findings is usually not the one who set the flag."""
        gap = next(g for g in perception_gaps(_state(watch_namespaces=("prod",))) if "only" in g)
        assert "not a statement about the cluster" in gap

    def test_a_healthy_scoped_sensorium_still_reports_the_gap(self):
        """Every instrument reads fine — that is exactly why this one has to be said."""
        state = _state(watch_namespaces=("prod",), shed_total=0, predictive="off")
        assert state.watching is True
        assert perception_gaps(state) != []

    def test_the_default_says_nothing(self):
        assert perception_gaps(_state()) == []

    def test_it_is_reported_alongside_the_other_ways_to_be_blind(self):
        state = _state(watch_namespaces=("prod",), shed_total=5, queue_high_water=10000)
        gaps = perception_gaps(state)
        assert len(gaps) == 2, gaps
        assert any("dropped 5 event(s)" in g for g in gaps)


class TestTheQueueWarnsBeforeItLoses:
    @pytest.fixture(autouse=True)
    def _reset(self):
        k8s_watcher.reset_queue_stats()
        yield
        k8s_watcher.reset_queue_stats()

    def test_it_warns_at_the_pressure_ratio_with_nothing_shed(self, caplog):
        """Until now the FIRST signal was shed_total — which only speaks after the loss."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        for _ in range(8):
            queue.put_nowait(object())
        with caplog.at_level("WARNING"):
            k8s_watcher._warn_if_under_pressure(queue)
        assert "nothing has been shed YET" in caplog.text
        assert k8s_watcher.queue_stats()["shed_total"] == 0

    def test_it_names_both_levers(self, caplog):
        queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        for _ in range(9):
            queue.put_nowait(object())
        with caplog.at_level("WARNING"):
            k8s_watcher._warn_if_under_pressure(queue)
        assert "SENSORIUM_WATCH_NAMESPACES" in caplog.text
        assert "SENSORIUM_QUEUE_MAXSIZE" in caplog.text

    def test_it_is_quiet_below_the_ratio(self, caplog):
        queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        for _ in range(5):
            queue.put_nowait(object())
        with caplog.at_level("WARNING"):
            k8s_watcher._warn_if_under_pressure(queue)
        assert caplog.text == ""

    def test_it_warns_once_not_per_observation(self, caplog):
        """A warning that is itself proportional to the load is part of the problem."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        for _ in range(9):
            queue.put_nowait(object())
        with caplog.at_level("WARNING"):
            for _ in range(50):
                k8s_watcher._warn_if_under_pressure(queue)
        assert caplog.text.count("nothing has been shed YET") == 1

    def test_an_unbounded_queue_never_warns(self, caplog):
        with caplog.at_level("WARNING"):
            k8s_watcher._warn_if_under_pressure(asyncio.Queue())
        assert caplog.text == ""

    def test_the_ceiling_is_reported_so_the_high_water_can_be_read(self):
        stats = k8s_watcher.queue_stats()
        assert stats["maxsize"] == settings.SENSORIUM_QUEUE_MAXSIZE
        assert {"shed_total", "high_water", "maxsize"} <= set(stats)


class TestTheDecisionIsWrittenDown:
    def test_the_adr_exists_and_states_the_ceiling_is_unchanged(self):
        adr = Path(__file__).resolve().parents[1] / "design" / "adr" / \
            "020-v4-perception-at-scale.md"
        text = adr.read_text(encoding="utf-8")
        assert "relists the world" in text
        assert "🔴 → 🟡, not 🟢" in text
        assert "No supported cluster size is claimed" in text

    def test_the_setting_documents_the_blind_spot(self):
        src = Path(__file__).resolve().parents[1] / "packages" / "kubeintellect-server" / \
            "app" / "core" / "config.py"
        text = src.read_text(encoding="utf-8")
        block = text[text.index("SENSORIUM_WATCH_NAMESPACES") - 900:]
        assert "creates a blind spot BY DESIGN" in block
        assert "ADR-020" in block

    def test_the_docs_tell_an_operator_which_lever_to_pull(self):
        docs = Path(__file__).resolve().parents[1] / "docs" / "configuration.md"
        text = docs.read_text(encoding="utf-8")
        assert "SENSORIUM_WATCH_NAMESPACES" in text
