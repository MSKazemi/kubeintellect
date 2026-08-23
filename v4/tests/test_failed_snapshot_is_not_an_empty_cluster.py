"""A cluster read that failed is not a cluster that is empty and healthy.

`context_fetcher` pre-fetches pods and warning events before every turn. Its runner returned
`proc.stdout or proc.stderr` and **never looked at the exit code**, so kubectl's error text was
handed to the table parser as if it were cluster data. Verified against the real binary
(`bitnami/kubectl:latest`), the two failure shapes produce two different lies:

    $ kubectl get pods --all-namespaces          # no kubeconfig — three lines, exit 1
    E0820 … "Unhandled Error" err="couldn't get current server API group list: …"
    E0820 … "Unhandled Error" err="couldn't get current server API group list: …"
    The connection to the server localhost:8080 was refused - did you specify the right host or port?

The two `E0820` lines have enough whitespace-separated columns to be counted as pod rows, so the
scan reported **`pod_count=2`** — a quantity invented out of an error message. A single-line error
(`error: You must be logged in to the server (Unauthorized)`) was instead consumed as the header
row, giving **`pod_count=0, has_issues=False`**: an unreachable cluster reported as an empty,
healthy one.

Three consumers acted on that:

1. **The prompt.** `_snapshot_sufficiency_block` asserts *"The cluster snapshot above was fetched
   Ns ago and contains {pod_count} pods. Health flags: issues=false, warnings=false"* and then
   instructs the model to *prefer answering directly from the snapshot* for exactly the questions
   "how many pods", "is the cluster healthy", "what's running". Measured with the pods read failing
   and the events read succeeding-and-empty — an ordinary asymmetric failure — the model was told
   the cluster had zero pods and no issues, and told not to check.
2. **R4 post-fix verification.** `_verify_resolution` documents "None if verification … failed to
   run", but the runner never raised, so a failed read scanned clean and it returned
   `(True, "resolved")` — recording an unverified fix as verified. `promotion.py` selects on
   `WHERE verified = TRUE` to mint learned rules and detector candidates. A cluster read is most
   likely to fail immediately after a disruptive change, which is precisely when this runs.
3. **Playbook matching**, which ran its trigger regexes over the stderr text.

The fix is not to hide the error — an operator needs it — but to stop *counting* it.
"""
from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import patch

import pytest
from app.agent.nodes import context_fetcher as cf
from app.agent.nodes.context_fetcher import _kubectl_snapshot, _scan_snapshot
from app.agent.nodes.coordinator import _snapshot_sufficiency_block, _verify_resolution
from app.core.config import settings

# Captured from the real binary, 2026-08-20. Kept verbatim: the line *shape* is the bug.
REAL_CONNECTION_REFUSED = (
    'E0820 00:54:23.948463       8 memcache.go:265] "Unhandled Error" err="couldn\'t get current '
    'server API group list: Get \\"http://localhost:8080/api?timeout=32s\\": dial tcp [::1]:8080: '
    'connect: connection refused"\n'
    'E0820 00:54:23.949311       8 memcache.go:265] "Unhandled Error" err="couldn\'t get current '
    'server API group list: Get \\"http://localhost:8080/api?timeout=32s\\": dial tcp [::1]:8080: '
    'connect: connection refused"\n'
    "The connection to the server localhost:8080 was refused - did you specify the right host or port?"
)
REAL_UNAUTHORIZED = "error: You must be logged in to the server (Unauthorized)"
REAL_FORBIDDEN = (
    'Error from server (Forbidden): pods is forbidden: User '
    '"system:serviceaccount:kubeintellect:kubeintellect" cannot list resource "pods" '
    'in API group "" at the cluster scope'
)
REAL_POD_TABLE = (
    "NAMESPACE     NAME                     READY   STATUS             RESTARTS   AGE\n"
    "default       web-7d9f-abcde           1/1     Running            0          4h\n"
    "default       api-55c8-fghij           0/1     CrashLoopBackOff   7          22m\n"
)


class TestTheRunnerReportsFailure:
    @pytest.mark.parametrize("stderr", [REAL_CONNECTION_REFUSED, REAL_UNAUTHORIZED, REAL_FORBIDDEN])
    def test_a_non_zero_exit_is_not_ok(self, stderr):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", stderr)):
            ok, text = _kubectl_snapshot(["get", "pods", "--all-namespaces"])
        assert ok is False
        assert text, "the operator still needs to see why"

    def test_a_zero_exit_is_ok(self):
        with patch("subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, REAL_POD_TABLE, "")):
            assert _kubectl_snapshot(["get", "pods"]) == (True, REAL_POD_TABLE)

    @pytest.mark.parametrize("exc", [
        FileNotFoundError(2, "No such file or directory", "kubectl"),
        subprocess.TimeoutExpired("kubectl", 30),
    ])
    def test_a_command_that_never_ran_is_not_ok(self, exc):
        with patch("subprocess.run", side_effect=exc):
            ok, _ = _kubectl_snapshot(["get", "pods"])
        assert ok is False


class TestTheScanRefusesToParseAnError:
    def test_the_three_line_error_no_longer_becomes_two_pods(self):
        """The exact regression: kubectl's own log lines counted as pod rows."""
        assert _scan_snapshot(REAL_CONNECTION_REFUSED, "", pods_ok=False, events_ok=False) \
            == (False, False, 0)

    @pytest.mark.parametrize("err", [REAL_CONNECTION_REFUSED, REAL_UNAUTHORIZED, REAL_FORBIDDEN])
    def test_no_failure_shape_produces_a_pod_count(self, err):
        _, _, count = _scan_snapshot(err, err, pods_ok=False, events_ok=False)
        assert count == 0

    def test_a_failed_events_read_is_not_no_warnings(self):
        """`has_warnings=False` here means unknown; the caller's read_failed flag carries that."""
        _, has_warnings, _ = _scan_snapshot(REAL_POD_TABLE, REAL_UNAUTHORIZED,
                                            pods_ok=True, events_ok=False)
        assert has_warnings is False, "stderr is not a warning list"

    def test_a_real_table_still_scans_correctly(self):
        """Guard on the guard — the fix must not blind the healthy path."""
        has_issues, has_warnings, count = _scan_snapshot(
            REAL_POD_TABLE, "No resources found\n", pods_ok=True, events_ok=True)
        assert (has_issues, has_warnings, count) == (True, False, 2)


def _run_node(pods=(False, REAL_UNAUTHORIZED), events=(True, "No resources found\n")):
    def fake(args):
        return pods if args[1] == "pods" else events
    async def go():
        with patch.object(cf, "_kubectl_snapshot", side_effect=fake):
            return await cf.context_fetcher({"session_id": "t"})
    return asyncio.run(go())


class TestTheNodeSaysTheReadFailed:
    def test_the_asymmetric_failure_is_flagged(self):
        """Pods fail, events succeed and are genuinely empty — the full-lie case."""
        out = _run_node()
        assert out["snapshot_read_failed"] is True
        assert out["snapshot_pod_count"] == 0
        assert out["snapshot_has_issues"] is False

    def test_stderr_is_not_presented_as_pod_state(self):
        snap = _run_node()["cluster_snapshot"]
        pod_section = snap.split("### Warning")[0]
        assert "UNAVAILABLE" in pod_section
        assert "not zero pods" in pod_section
        assert REAL_UNAUTHORIZED in pod_section, "the operator still needs the reason"

    def test_a_failed_events_read_is_not_rendered_as_cluster_appears_healthy(self):
        snap = _run_node(pods=(True, REAL_POD_TABLE),
                         events=(False, REAL_FORBIDDEN))["cluster_snapshot"]
        assert "cluster appears healthy" not in snap
        assert "UNAVAILABLE" in snap.split("### Warning")[1]

    def test_playbooks_do_not_match_against_stderr(self):
        seen = {}
        def spy(pods_out, events_out):
            seen["args"] = (pods_out, events_out)
            return []
        with patch("app.agent.playbooks.match_playbooks", spy):
            _run_node()
        assert seen["args"][0] == "", "a failed pod read must not feed trigger regexes"

    def test_a_healthy_cluster_is_untouched(self):
        out = _run_node(pods=(True, REAL_POD_TABLE), events=(True, "No resources found\n"))
        assert out["snapshot_read_failed"] is False
        assert out["snapshot_pod_count"] == 2
        assert "cluster appears healthy" in out["cluster_snapshot"]


class TestThePromptStopsAssertingACount:
    def test_it_claims_no_pod_count_when_the_read_failed(self):
        block = _snapshot_sufficiency_block(_run_node())
        assert "contains" not in block, block
        assert "issues=false" not in block, block

    def test_it_forbids_answering_from_the_snapshot(self):
        block = _snapshot_sufficiency_block(_run_node())
        assert "Prefer answering directly from the snapshot" not in block
        assert "never answer from the snapshot" in block
        assert "not an empty or healthy cluster" in block

    def test_the_normal_block_is_unchanged(self):
        block = _snapshot_sufficiency_block(
            _run_node(pods=(True, REAL_POD_TABLE), events=(True, "No resources found\n")))
        assert "contains 2 pods" in block
        assert "Prefer answering directly from the snapshot" in block

    def test_off_still_means_off(self, mocker):
        mocker.patch.object(settings, "SNAPSHOT_SUFFICIENCY_MODE", "off")
        assert _snapshot_sufficiency_block(_run_node()) == ""


class TestPostFixVerificationCannotFabricateResolved:
    """The one that writes to memory — `promotion.py` selects `WHERE verified = TRUE`."""

    @pytest.fixture(autouse=True)
    def _on(self, mocker):
        mocker.patch.object(settings, "REFLEXION_VERIFY_RESOLUTION", True)
        mocker.patch("app.agent.nodes.coordinator._wait_for_rollout", lambda ns: None)

    def _verify(self, pods, events=(True, "No resources found\n")):
        def fake(args):
            return pods if args[1] == "pods" else events
        with patch("app.agent.nodes.context_fetcher._kubectl_snapshot", side_effect=fake):
            return _verify_resolution("prod", pre_state={"had_issues": True})

    @pytest.mark.parametrize("err", [REAL_CONNECTION_REFUSED, REAL_UNAUTHORIZED, REAL_FORBIDDEN])
    def test_a_failed_read_is_unverified_not_resolved(self, err):
        assert self._verify((False, err)) == (None, None)

    def test_a_clean_read_still_verifies(self):
        healthy = ("NAMESPACE   NAME     READY   STATUS    RESTARTS   AGE\n"
                   "prod        web-1    1/1     Running   0          4h\n")
        assert self._verify((True, healthy)) == (True, "resolved")

    def test_a_still_broken_read_still_reports_it(self):
        assert self._verify((True, REAL_POD_TABLE))[0] is False
