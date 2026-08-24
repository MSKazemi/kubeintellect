"""`kubectl_output.classify_output` must agree with the string `run_kubectl` actually returns.

Every other test of this classifier builds its input by hand, so the classifier and the tool that
feeds it were free to drift — and on 2026-08-24 they did. `run_kubectl` was changed that morning
to state a non-zero exit (`[kubectl exited 1] Error from server (Forbidden): …`), which is strictly
more information; but the classifier matches on **line prefixes**, and the marker sits in front of
kubectl's own line. Measured: an RBAC `Forbidden`, a refused API server and a bad local path all
came back `ok` with `reached_cluster() == True`, and `deployment_ready` answered *"deployment 'web'
not found in 'prod'"* about a namespace it never read — the exact verdict `postcondition.py` was
written on 2026-08-20 to stop it giving. Downstream, `execute_transactional` reads that `met=False`
as a failed mitigation and **rolls back**, so an instrument outage becomes a live mutation.

So these tests drive the real tool. A test that asserts a classifier's behaviour on a literal it
also wrote proves the classifier self-consistent, not correct.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from app.tools.aci import kubectl_output as out
from app.tools.aci.postcondition import deployment_ready
from app.tools.kubectl_tool import run_kubectl

READ = "get deployment web -n prod"

# Captured from real kubectl; see `_FAILURE_PREFIXES` in the module under test.
FORBIDDEN = 'Error from server (Forbidden): deployments.apps is forbidden: User "sa" cannot get'
REFUSED_CONN = ("The connection to the server localhost:8080 was refused - "
                "did you specify the right host or port?")
BAD_PATH = 'error: the path "/nope.yaml" does not exist'
HEALTHY = "NAME   READY   UP-TO-DATE   AVAILABLE\nweb    3/3     3            3\n"


def _proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.stdout, proc.stderr, proc.returncode = stdout, stderr, returncode
    return proc


def _kubectl(command: str = READ, **kw: object) -> str:
    """The real tool's answer, with only the subprocess boundary faked."""
    with patch("subprocess.run", return_value=_proc(**kw)):  # type: ignore[arg-type]
        return run_kubectl.invoke({"command": command})


# ── The tool still emits the marker these tests are about ──────────────────────

def test_the_tool_still_states_a_nonzero_exit() -> None:
    """Non-vacuity: if `run_kubectl` stops emitting the marker, say so here, loudly.

    Without this, every assertion below would keep passing while testing a shape the tool no
    longer produces — which is how the drift these tests exist to catch went unnoticed.
    """
    assert _kubectl(stderr=FORBIDDEN, returncode=1).startswith("[kubectl exited 1]")


# ── A failure the tool reports must not classify as a reading ──────────────────

@pytest.mark.parametrize("stderr", [FORBIDDEN, REFUSED_CONN, BAD_PATH])
def test_a_failed_read_never_classifies_as_ok(stderr: str) -> None:
    assert out.classify_output(_kubectl(stderr=stderr, returncode=1)) != out.OK


def test_a_successful_read_does_classify_as_ok() -> None:
    """Vacuity guard in the other direction — the classifier is not simply always non-OK."""
    assert out.classify_output(_kubectl(stdout=HEALTHY)) == out.OK


def test_a_refusal_from_kubeintellect_itself_is_still_a_refusal() -> None:
    refused = _kubectl("get pods -n kube-system", stdout=HEALTHY)
    assert refused.startswith("[Protected]"), refused
    assert out.classify_output(refused) == out.REFUSED


# ── reached_cluster: rejected by the server is not the same as never sent ──────

def test_a_server_rejection_did_reach_the_server() -> None:
    assert out.reached_cluster(_kubectl(stderr=FORBIDDEN, returncode=1)) is True


def test_a_connection_refusal_did_not_reach_the_server() -> None:
    assert out.reached_cluster(_kubectl(stderr=REFUSED_CONN, returncode=1)) is False


def test_a_successful_read_reached_the_server() -> None:
    assert out.reached_cluster(_kubectl(stdout=HEALTHY)) is True


def test_a_protected_refusal_never_reached_the_server() -> None:
    assert out.reached_cluster(_kubectl("get pods -n kube-system", stdout=HEALTHY)) is False


# ── The oracle that consumes it ────────────────────────────────────────────────

@pytest.mark.parametrize("stderr", [FORBIDDEN, REFUSED_CONN, BAD_PATH])
def test_the_health_oracle_reports_no_observation_on_a_failed_read(stderr: str) -> None:
    answer = _kubectl(stderr=stderr, returncode=1)
    result = deployment_ready("web", "prod", _runner=lambda _cmd: answer)
    assert result.evaluated is False, result
    assert "not found" not in result.detail, result.detail


def test_the_health_oracle_still_reads_a_healthy_deployment() -> None:
    """Vacuity guard — `evaluated=False` is not the answer to everything."""
    result = deployment_ready("web", "prod", _runner=lambda _cmd: _kubectl(stdout=HEALTHY))
    assert result.evaluated is True
    assert result.met is True
    assert (result.ready, result.desired) == (3, 3)


def test_a_genuinely_absent_deployment_is_still_an_observation() -> None:
    """The one case that must stay `evaluated=True`: the read worked and the row is not there."""
    empty = _kubectl(stdout="NAME   READY   UP-TO-DATE   AVAILABLE\nother  1/1     1            1\n")
    result = deployment_ready("web", "prod", _runner=lambda _cmd: empty)
    assert result.evaluated is True
    assert result.met is False
    assert "not found" in result.detail


# ── The marker itself ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("marked", [
    "[kubectl exited 1] Error from server (Forbidden): nope",
    "[kubectl exited 127] something else entirely",
    "[helm exited 1] Error: release: not found",
])
def test_any_exit_marker_is_a_failure(marked: str) -> None:
    assert out.classify_output(marked) == out.FAILED


def test_a_command_that_printed_nothing_is_not_an_observation() -> None:
    """`(no output)` is the tool's placeholder for silence, not a reading of an empty cluster."""
    silent = _kubectl()
    assert silent == "(no output)", silent
    assert out.classify_output(silent) == out.FAILED
    assert out.reached_cluster(silent) is False


def test_the_marker_is_read_as_a_prefix_and_not_as_a_substring() -> None:
    """A successful read whose *content* quotes the marker is still a successful read.

    Same property the module header states for `error from server`: cluster data routinely
    contains error text — a ConfigMap of message templates, a log line, an annotation — and a
    classifier that greps anywhere turns that data into a verdict about the command that read it.
    """
    listing = (
        "NAME              DATA\n"
        "message-templates 1\n"
        "  tpl: '[kubectl exited 1] Error from server (Forbidden)'\n"
    )
    assert out.classify_output(_kubectl(stdout=listing)) == out.OK
    assert out.reached_cluster(_kubectl(stdout=listing)) is True


def test_a_resource_named_like_an_error_is_still_ok() -> None:
    """The prefix-not-substring property the module was built on, still true after the change."""
    listing = "NAME                    READY\nerror-budget-exporter   1/1\n"
    assert out.classify_output(_kubectl(stdout=listing)) == out.OK
    assert out.reached_cluster(_kubectl(stdout=listing)) is True
