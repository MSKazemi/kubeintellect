r"""A predicate that fires on a healthy object is the mirror image of a dead one.

The dead-predicate family got three gates and 50-odd tests because a detector that can never fire
contributes silence, and silence reads as a healthy cluster. The opposite failure had none, and
it is worse in the direction that matters to H4: a predicate matching a HEALTHY status produces a
finding about every object of its kind on the cluster, for ever, and every one of them counts as
a false positive on a lane whose pre-registered endpoint IS the false-positive rate.

`nl:soak-cpu-saturated` was authored (ADR-012, NL → detect block) from the prose

    "a workload is pinned at its CPU limit"

and the model compiled it to

    {kind: Pod, status_regex: "^Running$"}

plus a trend predicate over `container_cpu_usage_seconds_total`. `WatchPredicate.matches` tests
the status and nothing else — there is no namespace or label scope — and the trend predicate runs
on a separate loop and is OR'd with the watch predicates, never AND'd. So the compiled detector
means "fire on every pod that is Running". It validated, stored, listed as `shadow`, and on the
F3 soak cluster its ring held 46 findings taken before any fault was injected:

    {"namespace": "kube-system", "object": "coredns-585777974f-9mtbs",
     "evidence": "pod status=Running", "severity": "warning"}

The positive control then graded it ALIVE, because it fired when a pod was injected into the
control's namespace and that pod was Running.

Where each gate stands, and why they differ:

    authoring   REFUSE  — the only place it can be stopped; a WatchPredicate cannot be narrowed
    review      REFUSE  — never promote one out of shadow into the watchtower
    engine      LOAD    — and record it. Refusing at load would delete the evidence that the
                          detector is wrong and move the measured false-positive rate toward
                          the pre-registered direction, which is what the round-two liveness
                          gate did before `dropped_predicates` split it per predicate.
"""
from __future__ import annotations

import re

import pytest

from app.detectors.authoring import validate_detect_block
from app.detectors.models import DetectBlock, WatchPredicate
from app.detectors.predicate_shape import (
    HEALTHY_STATUS,
    predicate_health_errors,
    predicate_liveness_errors,
)


def _pod(pattern: str) -> WatchPredicate:
    return WatchPredicate(kind="Pod", status_regex=re.compile(pattern))


def _node(pattern: str) -> WatchPredicate:
    return WatchPredicate(kind="Node", status_regex=re.compile(pattern))


class TestTheHealthyStatusesAreTheOnesTheObserverEmits:
    """Read off `pod_display_status`, not guessed. The whole dead-predicate family exists because
    `^OOMKilled$` was compared against a function whose range did not contain it."""

    def test_the_pod_statuses_are_the_steady_healthy_ones(self):
        assert set(HEALTHY_STATUS["Pod"]) == {"Running", "Completed", "Succeeded"}

    def test_the_node_status_is_ready(self):
        assert set(HEALTHY_STATUS["Node"]) == {"Ready"}

    def test_pod_display_status_really_returns_running_for_a_healthy_pod(self):
        from app.sensorium.observations import pod_display_status
        healthy = {
            "metadata": {"name": "p"},
            "status": {"phase": "Running",
                       "conditions": [{"type": "Ready", "status": "True"}],
                       "containerStatuses": [{"name": "c", "ready": True,
                                              "state": {"running": {}}}]},
        }
        assert pod_display_status(healthy) in HEALTHY_STATUS["Pod"]

    def test_pod_display_status_returns_completed_for_a_finished_container(self):
        from app.sensorium.observations import pod_display_status
        done = {
            "metadata": {"name": "p"},
            "status": {"phase": "Succeeded",
                       "containerStatuses": [{"name": "c", "ready": False,
                                              "state": {"terminated": {"reason": "Completed",
                                                                       "exitCode": 0}}}]},
        }
        assert pod_display_status(done) in HEALTHY_STATUS["Pod"]


class TestTheCheckItself:
    def test_the_soak_predicate_is_refused(self):
        errors = predicate_health_errors(_pod("^Running$"))
        assert errors and "Running" in errors[0]

    def test_succeeded_is_refused(self):
        assert predicate_health_errors(_pod("^Succeeded$"))

    def test_a_ready_node_is_refused(self):
        assert predicate_health_errors(_node("^Ready$"))

    @pytest.mark.parametrize("pattern", [
        "^CrashLoopBackOff$", "^ImagePullBackOff$", "^ErrImagePull$", "^OOMKilled$",
        "^Error$", "^Pending$", "^Evicted$", "^Init:CrashLoopBackOff$",
    ])
    def test_a_real_fault_predicate_is_untouched(self, pattern):
        assert predicate_health_errors(_pod(pattern)) == []

    def test_notready_is_not_swallowed_by_ready(self):
        """`Ready` is a substring of `NotReady`. An unanchored membership test would refuse the
        one predicate on the soak cluster that is watching the right kind of thing."""
        assert predicate_health_errors(_node("^NotReady$")) == []
        assert predicate_health_errors(_pod("^.*NotReady.*$")) == []

    def test_an_alternation_that_includes_a_healthy_status_is_refused(self):
        """It fires on every healthy pod AND on the fault; the healthy half is not cancelled by
        the presence of the other."""
        assert predicate_health_errors(_pod("^(Running|CrashLoopBackOff)$"))

    def test_an_event_predicate_is_out_of_scope(self):
        assert predicate_health_errors(
            WatchPredicate(kind="Event", reason_regex=re.compile("^BackOff$"))) == []

    def test_a_predicate_with_no_status_regex_is_out_of_scope(self):
        # That one is already refused, as dead, by `predicate_liveness_errors`.
        assert predicate_health_errors(WatchPredicate(kind="Pod")) == []
        assert predicate_liveness_errors(WatchPredicate(kind="Pod"))

    def test_the_message_says_why_it_cannot_be_narrowed(self):
        msg = predicate_health_errors(_pod("^Running$"))[0]
        assert "no namespace or label scope" in msg


class TestTheAuthoringGateRefusesIt:
    """The only place it can be stopped. `WatchPredicate` has no scope to add afterwards."""

    def test_the_soak_block_is_refused(self):
        block, errors = validate_detect_block(
            {"watch_predicates": [{"kind": "Pod", "status_regex": "^Running$"}]},
            "nl:soak-cpu-saturated")
        assert block is None
        assert any("HEALTHY" in e for e in errors)

    def test_a_trend_predicate_does_not_rescue_it(self):
        """The soak detector carried both. They are OR'd, so the trend half does not narrow the
        watch half — the compiled detector still fires on every Running pod."""
        block, errors = validate_detect_block(
            {"watch_predicates": [{"kind": "Pod", "status_regex": "^Running$"}],
             "trend_predicates": [{"metric": "container_cpu_usage_seconds_total",
                                   "threshold": 0.9}]},
            "nl:soak-cpu-saturated")
        assert block is None
        assert any("HEALTHY" in e for e in errors)

    def test_a_real_fault_detector_still_validates(self):
        block, errors = validate_detect_block(
            {"watch_predicates": [{"kind": "Pod", "status_regex": "^CrashLoopBackOff$"}]},
            "nl:soak-crashloop")
        assert errors == []
        assert block is not None


class TestTheEngineLoadsItAnyway:
    """Refusing at load would delete the evidence against H4. The round-two liveness gate did
    exactly that — it dropped whole rows and improved the measured false-positive rate by
    removing the predicates that falsified it."""

    def test_the_block_carries_the_reason(self):
        block = DetectBlock(playbook="nl:soak-cpu-saturated",
                            watch_predicates=(_pod("^Running$"),),
                            fires_on_healthy=("fires on every healthy Pod",))
        assert block.fires_on_healthy

    def test_the_field_defaults_to_empty_for_a_playbook_block(self):
        assert DetectBlock(playbook="p").fires_on_healthy == ()

    def test_the_loader_records_it_rather_than_dropping_the_predicate(self):
        src = (__import__("pathlib").Path(
            __import__("app.detectors.engine", fromlist=["x"]).__file__).read_text())
        i_record = src.index("db_detector_fires_on_healthy_objects")
        assert "fires_on_healthy=tuple(unhealthy)" in src
        # It must NOT appear in the expression that computes the live predicate set.
        live = src[src.index("live_watch = tuple("):src.index("dropped = [msg")]
        assert "predicate_health_errors" not in live, (
            "a predicate that fires on healthy objects must still be EVALUATED — dropping it "
            "removes the false positives it produces from the measurement of false positives"
        )
        assert i_record > src.index("live_watch = tuple(")


class TestTheApiSaysSo:
    def test_watching_reason_warns_when_the_detector_fires_on_health(self):
        from app.api.v1.endpoints.detectors import _watching
        loaded = DetectBlock(playbook="nl:soak-cpu-saturated",
                             watch_predicates=(_pod("^Running$"),),
                             fires_on_healthy=("status_regex '^Running$' matches Running",))
        watching, reason = _watching(loaded, "nl:soak-cpu-saturated")
        assert watching is True
        assert "HEALTHY" in reason

    def test_a_normal_detector_gets_no_warning(self):
        from app.api.v1.endpoints.detectors import _watching
        loaded = DetectBlock(playbook="nl:soak-crashloop",
                             watch_predicates=(_pod("^CrashLoopBackOff$"),))
        watching, reason = _watching(loaded, "nl:soak-crashloop")
        assert watching is True
        assert "HEALTHY" not in reason


class TestTheAuthoringPromptStatesTheRule:
    """A gate that only refuses teaches the model nothing — it retries and is refused again. Each
    hard rule in this prompt was written because a model broke it and the result was stored."""

    SRC = __import__("app.detectors.authoring", fromlist=["x"])._AUTHORING_SYSTEM

    def test_the_healthy_status_rule_is_stated(self):
        assert "never match a HEALTHY status" in self.SRC.replace("NEVER", "never")

    def test_it_names_the_statuses(self):
        for status in ("Running", "Completed", "Succeeded", "Ready"):
            assert status in self.SRC

    def test_it_says_a_trend_predicate_does_not_narrow_a_watch_predicate(self):
        assert "OR'd, never AND'd" in self.SRC

    def test_it_says_what_to_do_instead(self):
        assert "trend_predicate ALONE" in self.SRC
