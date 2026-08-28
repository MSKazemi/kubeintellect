"""The scheduled post-fix re-check must read the cluster before it says it re-checked.

`prospective.py` exists to answer one question an operator asks after an autonomous fix:
*did it hold?* Until 2026-08-28 the production answer was manufactured. The dispatcher was
pluggable "so the watchtower can wire a real investigation", nothing ever called
`set_dispatch` outside the tests, and the fallback logged the row and returned
`"rechecked"` — which `_TERMINAL` mapped to `status='done'`. Every re-check therefore
closed as a completed verification of a cluster nobody had looked at, and the row was
indistinguishable from one that had genuinely passed.

These tests pin the properties that make the answer real:

  * the default dispatcher issues actual reads, and they are reads;
  * a failed read is `"unverified"` and is NOT terminal — the row retries rather than
    closing on an answer nobody obtained;
  * it grades the same way `coordinator._verify_resolution` does, so the two post-fix
    graders in this codebase cannot disagree about the word "resolved";
  * no outcome reachable from the default is missing from `_TERMINAL` by accident.
"""
from __future__ import annotations

import inspect

import pytest

from app.memory import prospective, service

HEALTHY_PODS = (
    "NAMESPACE   NAME        READY   STATUS      RESTARTS   AGE\n"
    "dev         api-0       1/1     Running     0          4h\n"
    "dev         migrate-0   0/1     Completed   0          4h\n"
)
BROKEN_PODS = (
    "NAMESPACE   NAME    READY   STATUS             RESTARTS   AGE\n"
    "dev         api-0   0/1     CrashLoopBackOff   7          4h\n"
)
WARNING_EVENTS = (
    "LAST SEEN   TYPE      REASON      OBJECT      MESSAGE\n"
    "3m          Warning   BackOff     pod/api-0   Back-off restarting failed container\n"
)
NO_EVENTS = "No resources found in dev namespace.\n"
READ_FAILED = "error: You must be logged in to the server (Unauthorized)\n"

ROW = {"id": "p1", "cluster_id": "c1", "namespace": "dev",
       "condition": "did the CrashLoopBackOff fix hold?",
       "check_query": "CrashLoopBackOff", "source_episode_id": "ep-1"}


class Cluster:
    """Stands in for `_kubectl_snapshot`, recording every command it was asked to run."""

    def __init__(self, pods: tuple[bool, str], events: tuple[bool, str] = (True, NO_EVENTS)):
        self.pods = pods
        self.events = events
        self.commands: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[bool, str]:
        self.commands.append(list(args))
        return self.pods if args[1] == "pods" else self.events


@pytest.fixture
def cluster(mocker):
    """Patch the shared snapshot runner; the module imports it lazily, inside the call."""
    def _install(pods, events=(True, NO_EVENTS)):
        fake = Cluster(pods, events)
        mocker.patch(
            "app.agent.nodes.context_fetcher._kubectl_snapshot", side_effect=fake)
        return fake
    return _install


class TestTheDefaultReadsTheCluster:
    async def test_healthy_pods_grade_resolved(self, cluster):
        fake = cluster((True, HEALTHY_PODS))
        assert await prospective._default_dispatch(ROW, "A1") == "resolved"
        assert len(fake.commands) == 2                      # pods AND warning events

    async def test_a_broken_pod_grades_still_broken(self, cluster):
        cluster((True, BROKEN_PODS), events=(True, WARNING_EVENTS))
        assert await prospective._default_dispatch(ROW, "A1") == "still_broken"

    async def test_it_reads_the_rows_namespace(self, cluster):
        fake = cluster((True, HEALTHY_PODS))
        await prospective._default_dispatch(ROW, "A1")
        assert all("-n" in cmd and "dev" in cmd for cmd in fake.commands)

    async def test_a_row_without_a_namespace_reads_the_whole_cluster(self, cluster):
        fake = cluster((True, HEALTHY_PODS))
        await prospective._default_dispatch({**ROW, "namespace": ""}, "A1")
        assert all("--all-namespaces" in cmd for cmd in fake.commands)

    async def test_every_command_it_issues_is_a_read(self, cluster):
        """A re-check verifies; it never repairs. `get` is the only verb this may use."""
        fake = cluster((True, BROKEN_PODS))
        await prospective._default_dispatch(ROW, "A1")
        assert [cmd[0] for cmd in fake.commands] == ["get", "get"]

    async def test_the_default_is_what_runs_when_nothing_was_injected(self):
        """`set_dispatch` was never called in production. That must now be the safe case."""
        prospective.set_dispatch(None)
        assert prospective._dispatch is None
        source = inspect.getsource(prospective.run_prospective_once)
        assert "_dispatch or _default_dispatch" in source


class TestAFailedReadIsNotAGrade:
    async def test_a_failed_pod_read_is_unverified(self, cluster):
        cluster((False, READ_FAILED))
        assert await prospective._default_dispatch(ROW, "A1") == "unverified"

    async def test_a_failed_pod_read_stops_before_the_events_read(self, cluster):
        fake = cluster((False, READ_FAILED))
        await prospective._default_dispatch(ROW, "A1")
        assert len(fake.commands) == 1     # no point asking a cluster that just refused

    def test_unverified_is_not_terminal(self):
        """The whole defect in one assertion: 'we could not look' must not close the row."""
        assert "unverified" not in prospective._TERMINAL
        assert prospective._TERMINAL.get("unverified", "pending") == "pending"

    def test_every_outcome_the_default_can_return_is_accounted_for(self):
        """Terminal-by-accident is the failure mode; an outcome is graded or it retries."""
        source = inspect.getsource(prospective._default_dispatch)
        returned = {line.split("return ")[1].strip().strip('"')
                    for line in source.splitlines()
                    if line.strip().startswith('return "')}
        assert returned == {"unverified"}                  # the early-out
        assert {"resolved", "still_broken"} <= set(prospective._TERMINAL)
        assert all(prospective._TERMINAL[o] == "done" for o in ("resolved", "still_broken"))

    async def test_a_failed_events_read_does_not_fail_the_grade(self, cluster):
        """Warnings do not decide this; healthy pods do. A dead events read must not
        invent a failure — nor, per `_scan_snapshot`, be read as 'no warnings'."""
        cluster((True, HEALTHY_PODS), events=(False, READ_FAILED))
        assert await prospective._default_dispatch(ROW, "A1") == "resolved"


class TestTheTwoGradersAgree:
    """`coordinator._verify_resolution` grades the coordinator's own post-fix snapshot; this
    grades the scheduled one. Both feed rows an operator compares, so a snapshot one calls
    resolved and the other calls broken would be a contradiction in the record."""

    @pytest.mark.parametrize("pods,events,resolved", [
        (HEALTHY_PODS, NO_EVENTS, True),
        (HEALTHY_PODS, WARNING_EVENTS, True),      # lingering warnings are not a regression
        (BROKEN_PODS, WARNING_EVENTS, False),
        (BROKEN_PODS, NO_EVENTS, False),
    ])
    async def test_same_snapshot_same_verdict(self, mocker, pods, events, resolved):
        from app.agent.nodes import coordinator

        def snapshot(args):
            return (True, pods) if args[1] == "pods" else (True, events)

        mocker.patch("app.agent.nodes.context_fetcher._kubectl_snapshot", side_effect=snapshot)
        mocker.patch.object(coordinator.settings, "REFLEXION_VERIFY_RESOLUTION", True)
        mocker.patch.object(coordinator, "_wait_for_rollout", lambda ns: None)

        verified, _label = coordinator._verify_resolution("dev")
        outcome = await prospective._default_dispatch(ROW, "A1")
        assert verified is resolved
        assert (outcome == "resolved") is resolved

    async def test_both_refuse_to_grade_a_failed_read(self, mocker):
        from app.agent.nodes import coordinator

        mocker.patch("app.agent.nodes.context_fetcher._kubectl_snapshot",
                     side_effect=lambda args: (False, READ_FAILED))
        mocker.patch.object(coordinator.settings, "REFLEXION_VERIFY_RESOLUTION", True)
        mocker.patch.object(coordinator, "_wait_for_rollout", lambda ns: None)

        assert coordinator._verify_resolution("dev") == (None, None)
        assert await prospective._default_dispatch(ROW, "A1") == "unverified"


class FakePool:
    def __init__(self, due_rows):
        self._due_rows = due_rows
        self.records: list[tuple] = []

    async def fetch(self, sql, *args):
        return self._due_rows

    async def execute(self, sql, *args):
        self.records.append(args)
        return "UPDATE 1"


class TestTheSchedulerPassRecordsTheRealOutcome:
    @pytest.fixture(autouse=True)
    def _wired(self, mocker):
        prospective.set_dispatch(None)
        mocker.patch.object(prospective.settings, "MEMORY_PROSPECTIVE", True)
        mocker.patch.object(prospective, "level_for_namespace", lambda ns: "A1")
        yield
        prospective.set_dispatch(None)

    async def test_a_verified_recheck_closes_done(self, mocker, cluster):
        cluster((True, HEALTHY_PODS))
        pool = FakePool([ROW])
        mocker.patch.object(service, "_pool", pool)
        assert await prospective.run_prospective_once() == 1
        assert pool.records[0][1:] == ("resolved", "done")

    async def test_a_still_broken_recheck_closes_done_with_the_bad_news(self, mocker, cluster):
        cluster((True, BROKEN_PODS))
        pool = FakePool([ROW])
        mocker.patch.object(service, "_pool", pool)
        await prospective.run_prospective_once()
        assert pool.records[0][1:] == ("still_broken", "done")

    async def test_an_unreadable_cluster_leaves_the_row_pending(self, mocker, cluster):
        """The row an operator would otherwise read as 'verified, done'."""
        cluster((False, READ_FAILED))
        pool = FakePool([ROW])
        mocker.patch.object(service, "_pool", pool)
        await prospective.run_prospective_once()
        assert pool.records[0][1:] == ("unverified", "pending")

    async def test_an_a0_namespace_still_never_reads_the_cluster(self, mocker, cluster):
        fake = cluster((True, HEALTHY_PODS))
        mocker.patch.object(prospective, "level_for_namespace", lambda ns: "A0")
        pool = FakePool([{**ROW, "namespace": "kube-system"}])
        mocker.patch.object(service, "_pool", pool)
        await prospective.run_prospective_once()
        assert fake.commands == []
        assert pool.records[0][1:] == ("skipped_a0", "cancelled")

    async def test_an_injected_dispatcher_still_wins(self, mocker, cluster):
        """The seam stays open for a richer re-check — it just is no longer required."""
        fake = cluster((True, HEALTHY_PODS))

        async def richer(row, level):
            return "still_broken"

        prospective.set_dispatch(richer)
        pool = FakePool([ROW])
        mocker.patch.object(service, "_pool", pool)
        await prospective.run_prospective_once()
        assert fake.commands == []
        assert pool.records[0][1:] == ("still_broken", "done")


class TestTheDocstringNoLongerPromisesAWiringThatDoesNotExist:
    def test_the_module_does_not_claim_the_watchtower_wires_the_real_one(self):
        source = inspect.getsource(prospective)
        assert "wired by the\n    watchtower via set_dispatch" not in source
        assert "Real investigation is wired" not in source
