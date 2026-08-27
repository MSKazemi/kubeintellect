"""A detector can only wait for a string its observer is able to produce.

`nl:soak-oom` was authored from the prose *"a container is killed for using too much memory
(OOMKilled)"*. ADR-012 turned that into `{"kind": "Pod", "status_regex": "^OOMKilled$"}`, stored
it, listed it as `shadow`, and offered it for promotion. It could not fire. Not because the
regex was wrong — it is exactly the string `kubectl get pods` prints for an OOM-killed container
— but because `pod_display_status` had no *terminated* branch, so `OOMKilled` was not in the
range of the only function whose output that predicate is ever matched against.

That is the #114 dead-predicate class arriving through a door #114's fix does not cover.
`predicate_shape.predicate_liveness_errors` asks whether a pattern's language contains only
strings that *look like* Kubernetes reasons; `OOMKilled` passes that test easily. The question it
could not ask is whether the string is one *this observer* emits, and the answer depended on a
missing `elif`.

These tests pin the observer's range to kubectl's, which is the range every predicate is written
against — by a person reading `kubectl get pods`, or by a model that learned from people who did.
Thirteen of the twenty fail against the pre-2026-08-25 implementation.
"""
from __future__ import annotations

import re

import pytest
from app.sensorium.observations import pod_display_status


def _pod(*, phase="Running", reason=None, containers=(), inits=(), conditions=(),
         deletion=None, spec_inits=None):
    pod: dict = {"metadata": {}, "status": {"phase": phase}}
    if deletion:
        pod["metadata"]["deletionTimestamp"] = deletion
    if reason:
        pod["status"]["reason"] = reason
    if containers:
        pod["status"]["containerStatuses"] = list(containers)
    if inits:
        pod["status"]["initContainerStatuses"] = list(inits)
    if conditions:
        pod["status"]["conditions"] = list(conditions)
    if spec_inits is not None:
        pod["spec"] = {"initContainers": spec_inits}
    return pod


def _terminated(reason=None, exit_code=1, signal=None):
    t: dict = {"exitCode": exit_code}
    if reason:
        t["reason"] = reason
    if signal:
        t["signal"] = signal
    return {"state": {"terminated": t}}


class TestTheTerminatedBranchThatWasMissing:
    """Every one of these returned the bare phase before the fix."""

    def test_an_oom_killed_container_reads_oomkilled(self):
        pod = _pod(phase="Running", containers=[_terminated("OOMKilled", exit_code=137)])
        assert pod_display_status(pod) == "OOMKilled"

    def test_the_authored_predicate_matches_that_pod(self):
        # The whole point. This is `nl:soak-oom`'s stored regex, verbatim, run against the
        # observation the sensorium would publish for a real OOMKill.
        status = pod_display_status(
            _pod(phase="Running", containers=[_terminated("OOMKilled", exit_code=137)])
        )
        assert re.compile("^OOMKilled$").match(status), (
            f"the detector waits for 'OOMKilled' and the observer says {status!r}"
        )

    def test_a_crashed_container_reads_error(self):
        pod = _pod(phase="Failed", containers=[_terminated("Error", exit_code=2)])
        assert pod_display_status(pod) == "Error"

    def test_a_reasonless_exit_reads_its_exit_code(self):
        pod = _pod(phase="Failed", containers=[_terminated(None, exit_code=3)])
        assert pod_display_status(pod) == "ExitCode:3"

    def test_a_signalled_exit_reads_its_signal(self):
        pod = _pod(phase="Failed", containers=[_terminated(None, exit_code=0, signal=9)])
        assert pod_display_status(pod) == "Signal:9"

    def test_a_finished_job_pod_reads_completed(self):
        pod = _pod(phase="Succeeded", containers=[_terminated("Completed", exit_code=0)])
        assert pod_display_status(pod) == "Completed"


class TestNotReadyMeansSomethingElseThanTheAuthorThought:
    """`nl:soak-not-ready` waits for `.*NotReady.*` meaning "the readiness probe fails".

    It does not mean that. kubectl prints `Running` for a pod whose readiness probe is failing;
    `NotReady` appears only for the completed-plus-running-and-ready shape below. The detector is
    live — the string is reachable — and it is still watching for the wrong event. Liveness and
    correctness are different properties and only one of them is machine-checkable.
    """

    def test_a_failing_readiness_probe_reads_running_not_notready(self):
        pod = _pod(
            phase="Running",
            containers=[{"ready": False, "state": {"running": {"startedAt": "2026-08-25T00:00:00Z"}}}],
            conditions=[{"type": "Ready", "status": "False"}],
        )
        assert pod_display_status(pod) == "Running"

    def test_completed_beside_a_ready_runner_on_an_unready_pod_reads_notready(self):
        pod = _pod(
            phase="Running",
            containers=[
                _terminated("Completed", exit_code=0),
                {"ready": True, "state": {"running": {"startedAt": "2026-08-25T00:00:00Z"}}},
            ],
            conditions=[{"type": "Ready", "status": "False"}],
        )
        assert pod_display_status(pod) == "NotReady"

    def test_the_same_pod_reads_running_once_it_is_ready(self):
        pod = _pod(
            phase="Running",
            containers=[
                _terminated("Completed", exit_code=0),
                {"ready": True, "state": {"running": {"startedAt": "2026-08-25T00:00:00Z"}}},
            ],
            conditions=[{"type": "Ready", "status": "True"}],
        )
        assert pod_display_status(pod) == "Running"


class TestPrecedence:
    def test_the_first_container_has_the_last_word(self):
        # kubectl walks containerStatuses in reverse without breaking, so index 0 wins. The old
        # implementation walked forward and returned on the first waiting reason it saw, which is
        # the opposite answer for a pod whose containers disagree.
        pod = _pod(
            phase="Running",
            containers=[
                {"state": {"waiting": {"reason": "CrashLoopBackOff"}}},
                {"state": {"waiting": {"reason": "ImagePullBackOff"}}},
            ],
        )
        assert pod_display_status(pod) == "CrashLoopBackOff"

    def test_a_container_status_overrides_the_pod_reason(self):
        # `status.reason` is the base kubectl starts from, not a fallback it consults last.
        pod = _pod(phase="Failed", reason="Evicted",
                   containers=[_terminated("OOMKilled", exit_code=137)])
        assert pod_display_status(pod) == "OOMKilled"

    def test_a_pod_reason_still_survives_with_no_container_statuses(self):
        assert pod_display_status(_pod(phase="Failed", reason="Evicted")) == "Evicted"


class TestInitContainers:
    def test_a_clean_init_container_is_skipped_for_the_next_one(self):
        pod = _pod(
            phase="Pending",
            inits=[_terminated("Completed", exit_code=0),
                   {"state": {"waiting": {"reason": "ImagePullBackOff"}}}],
        )
        assert pod_display_status(pod) == "Init:ImagePullBackOff"

    def test_an_init_container_that_died_reads_its_reason(self):
        pod = _pod(phase="Pending", inits=[_terminated("OOMKilled", exit_code=137)])
        assert pod_display_status(pod) == "Init:OOMKilled"

    def test_podinitializing_is_not_a_reason_it_is_a_count(self):
        pod = _pod(
            phase="Pending",
            inits=[{"state": {"waiting": {"reason": "PodInitializing"}}}],
            spec_inits=[{"name": "a"}, {"name": "b"}],
        )
        assert pod_display_status(pod) == "Init:0/2"

    def test_container_statuses_win_once_initialization_is_done(self):
        pod = _pod(
            phase="Running",
            inits=[{"state": {"waiting": {"reason": "SomethingOdd"}}}],
            containers=[_terminated("OOMKilled", exit_code=137)],
            conditions=[{"type": "Initialized", "status": "True"}],
        )
        assert pod_display_status(pod) == "OOMKilled"


class TestDeletion:
    def test_a_running_pod_under_deletion_reads_terminating(self):
        pod = _pod(phase="Running", deletion="2026-08-25T00:00:00Z")
        assert pod_display_status(pod) == "Terminating"

    @pytest.mark.parametrize("phase", ["Succeeded", "Failed"])
    def test_a_terminal_pod_under_deletion_keeps_its_own_status(self, phase):
        # The old implementation returned "Terminating" for any deletionTimestamp, which erased
        # the outcome of every completed pod that happened to be on its way out — including the
        # OOMKilled ones a detector is waiting for.
        pod = _pod(phase=phase, deletion="2026-08-25T00:00:00Z",
                   containers=[_terminated("OOMKilled", exit_code=137)])
        assert pod_display_status(pod) == "OOMKilled"

    def test_a_pod_on_a_lost_node_reads_unknown(self):
        pod = _pod(phase="Running", reason="NodeLost", deletion="2026-08-25T00:00:00Z")
        assert pod_display_status(pod) == "Unknown"
