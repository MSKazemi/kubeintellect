"""Unit tests for the context_fetcher snapshot scan (C2)."""
from __future__ import annotations

import subprocess

from app.agent.nodes.context_fetcher import (
    _SNAPSHOT_MAX_CHARS,
    _kubectl_snapshot,
    _run_kubectl_snapshot,
    _scan_snapshot,
)
from app.core.config import settings
from app.tools.output_policy import MARKER_PATTERNS, UNAVAILABLE_MARKER

HEALTHY_PODS = """\
NAMESPACE     NAME                              READY   STATUS    RESTARTS   AGE
default       app-1                             1/1     Running   0          2h
default       app-2                             1/1     Running   0          2h
kube-system   coredns-abc                       1/1     Running   0          3d
"""

WITH_CRASHLOOP = """\
NAMESPACE     NAME                              READY   STATUS             RESTARTS   AGE
default       app-1                             0/1     CrashLoopBackOff   5          10m
default       app-2                             1/1     Running            0          10m
"""

WITH_PENDING = """\
NAMESPACE     NAME                              READY   STATUS    RESTARTS   AGE
default       app-1                             0/1     Pending   0          1m
"""

EMPTY_PODS = """\
NAMESPACE     NAME   READY   STATUS   RESTARTS   AGE
"""

WARNING_EVENTS = """\
NAMESPACE   LAST SEEN   TYPE      REASON         OBJECT     MESSAGE
default     1m          Warning   BackOff        pod/app-1  Back-off restarting failed container
"""

NO_EVENTS = "No resources found in default namespace."


def test_scan_healthy_cluster() -> None:
    has_issues, has_warnings, pod_count = _scan_snapshot(HEALTHY_PODS, NO_EVENTS)
    assert has_issues is False
    assert has_warnings is False
    assert pod_count == 3


def test_scan_detects_crashloop() -> None:
    has_issues, _, pod_count = _scan_snapshot(WITH_CRASHLOOP, NO_EVENTS)
    assert has_issues is True
    assert pod_count == 2


def test_scan_detects_pending() -> None:
    has_issues, _, pod_count = _scan_snapshot(WITH_PENDING, NO_EVENTS)
    assert has_issues is True
    assert pod_count == 1


def test_scan_detects_warnings() -> None:
    _, has_warnings, _ = _scan_snapshot(HEALTHY_PODS, WARNING_EVENTS)
    assert has_warnings is True


def test_scan_no_warnings_when_empty() -> None:
    _, has_warnings, _ = _scan_snapshot(HEALTHY_PODS, NO_EVENTS)
    assert has_warnings is False


def test_scan_handles_empty_pod_list() -> None:
    has_issues, has_warnings, pod_count = _scan_snapshot(EMPTY_PODS, NO_EVENTS)
    assert has_issues is False
    assert has_warnings is False
    assert pod_count == 0


def test_scan_full_healthy_snapshot_stays_clean() -> None:
    # The conservative flags must not fire when nothing was truncated or lost.
    has_issues, has_warnings, pod_count = _scan_snapshot(HEALTHY_PODS, NO_EVENTS)
    assert has_issues is False
    assert has_warnings is False
    assert pod_count == 3


# ── #139 / #140: a snapshot that was cut is not a snapshot of the cluster ──────
#
# Reported by @uuzzrm against an earlier shape of this module, where the fix carried
# truncation in `has_issues`. The seam changed underneath the report: `has_issues` is
# rendered to the user as "unhealthy workloads were observed", so a cluster nobody could
# measure must not be described as an unhealthy one. Completeness is its own flag, and it
# gates the answer-from-the-snapshot shortcut instead. The scenarios below are his.


class _FakeCompletedProcess:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _long_listing(rows: int = 1000, status: str = "Running") -> str:
    return "NAMESPACE NAME READY STATUS RESTARTS AGE\n" + (
        f"default app-1 1/1 {status} 0 2h\n" * rows
    )


def test_snapshot_cap_reports_incomplete(monkeypatch) -> None:
    """The cap must be visible in the return value, not only in the text."""
    long_out = _long_listing()
    assert len(long_out) > _SNAPSHOT_MAX_CHARS
    monkeypatch.setattr(
        "app.agent.nodes.context_fetcher.subprocess.run",
        lambda *a, **k: _FakeCompletedProcess(stdout=long_out),
    )
    ok, text, complete = _kubectl_snapshot(["get", "pods", "--all-namespaces"])
    assert ok is True          # the read itself succeeded
    assert complete is False   # and it is not all of it
    assert len(text) <= _SNAPSHOT_MAX_CHARS + 400  # body capped; notices appended after


def test_snapshot_cap_emits_a_marker_the_prompt_names(monkeypatch) -> None:
    """A cut listing must carry a marker, or nothing downstream can know it was cut.

    This is the #140 failure in one assertion: before the fix the text came back
    silently sliced, with `ok=True` and no marker at all, and the coordinator was told
    to prefer answering "is the cluster healthy" from it.
    """
    monkeypatch.setattr(
        "app.agent.nodes.context_fetcher.subprocess.run",
        lambda *a, **k: _FakeCompletedProcess(stdout=_long_listing()),
    )
    _ok, text, _complete = _kubectl_snapshot(["get", "pods", "--all-namespaces"])
    assert any(marker in text for marker in MARKER_PATTERNS), text[-300:]


def test_an_unhealthy_pod_past_the_cap_does_not_read_as_healthy(monkeypatch) -> None:
    """@uuzzrm's scenario, on the current seam.

    A 401-pod listing whose one CrashLoopBackOff sorts past the cap: the surviving text
    genuinely contains no unhealthy status, so `has_issues` is False and always will be.
    What must not be False is completeness — that is the flag standing between this and
    an all-clear.
    """
    long_out = _long_listing(rows=400) + "payments payments-api 0/1 CrashLoopBackOff 47 3h\n"
    monkeypatch.setattr(
        "app.agent.nodes.context_fetcher.subprocess.run",
        lambda *a, **k: _FakeCompletedProcess(stdout=long_out),
    )
    ok, text, complete = _kubectl_snapshot(["get", "pods", "--all-namespaces"])
    assert "CrashLoopBackOff" not in text     # it really is past the cut
    has_issues, _w, _c = _scan_snapshot(text, NO_EVENTS, pods_ok=ok)
    assert has_issues is False                # nothing in the text says otherwise
    assert complete is False                  # and this is what stops the all-clear


def test_the_cap_does_not_invent_a_pod_out_of_a_severed_row(monkeypatch) -> None:
    """Cut on a line boundary, not a character.

    A character slice ends mid-row, and the fragment still has enough whitespace-separated
    columns to parse: `default app-1 1/1 Runni` was read as a pod whose STATUS is `Runni`,
    which is not a healthy phase. A listing containing nothing but Running pods therefore
    reported an issue, and counted the fragment as a pod. Found by the test above failing
    for the wrong reason.
    """
    monkeypatch.setattr(
        "app.agent.nodes.context_fetcher.subprocess.run",
        lambda *a, **k: _FakeCompletedProcess(stdout=_long_listing(rows=400)),
    )
    _ok, text, _complete = _kubectl_snapshot(["get", "pods", "--all-namespaces"])
    body = [ln for ln in text.splitlines() if ln and not ln.startswith("[")]
    assert body[-1] == "default app-1 1/1 Running 0 2h"  # whole row, not a fragment
    has_issues, _w, _c = _scan_snapshot(text, NO_EVENTS, pods_ok=True)
    assert has_issues is False


def test_the_withheld_notice_survives_the_cap(monkeypatch) -> None:
    """Blocked-namespace filtering must not go silent on a large cluster.

    `_filter_snapshot_output` appends its withheld sentence at the end of the table, and
    the cap used to run afterwards — so on any listing over the limit the sentence was
    sliced off and the short listing read as a complete one, which is the exact outcome
    its own docstring says it exists to prevent. Not part of the original report; found
    while fixing it.
    """
    blocked = sorted(settings.kubectl_blocked_namespaces)[0]
    out = (
        "NAMESPACE NAME READY STATUS RESTARTS AGE\n"
        + f"{blocked} secret-1 1/1 Running 0 9d\n" * 30
        + "default app-1 1/1 Running 0 2h\n" * 400
    )
    assert len(out) > _SNAPSHOT_MAX_CHARS
    monkeypatch.setattr(
        "app.agent.nodes.context_fetcher.subprocess.run",
        lambda *a, **k: _FakeCompletedProcess(stdout=out),
    )
    _ok, text, _complete = _kubectl_snapshot(["get", "pods", "--all-namespaces"])
    assert "withheld" in text.lower(), text[-300:]
    assert blocked not in text


def test_snapshot_failure_is_not_complete(monkeypatch) -> None:
    def fake_run(*args: object, **kwargs: object):
        raise subprocess.TimeoutExpired(cmd=["kubectl"], timeout=5)

    monkeypatch.setattr("app.agent.nodes.context_fetcher.subprocess.run", fake_run)
    ok, text, complete = _kubectl_snapshot(["get", "pods"])
    assert ok is False
    assert complete is False
    assert "(unavailable:" in text


def test_nonzero_exit_is_not_complete(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.agent.nodes.context_fetcher.subprocess.run",
        lambda *a, **k: _FakeCompletedProcess(
            stdout="", stderr="error: You must be logged in to the server (Unauthorized)",
            returncode=1,
        ),
    )
    ok, _text, complete = _kubectl_snapshot(["get", "pods"])
    assert ok is False
    assert complete is False


def test_a_failed_read_is_not_rendered_as_cluster_data(monkeypatch) -> None:
    """The text-only wrapper used to drop `ok`, so `targeted_investigator` fenced
    `error: You must be logged in…` under a `### Pod Description` heading with none of
    the markers the prompt tells the model to distrust."""
    monkeypatch.setattr(
        "app.agent.nodes.context_fetcher.subprocess.run",
        lambda *a, **k: _FakeCompletedProcess(
            stdout="", stderr="error: You must be logged in to the server (Unauthorized)",
            returncode=1,
        ),
    )
    rendered = _run_kubectl_snapshot(["describe", "pod", "api-1", "-n", "default"])
    assert UNAVAILABLE_MARKER in rendered


def test_a_short_listing_is_returned_unchanged(monkeypatch) -> None:
    """The ordinary case must not grow a marker it does not deserve."""
    unblocked = "\n".join(
        ln for ln in HEALTHY_PODS.splitlines() if not ln.startswith("kube-system")
    ) + "\n"
    monkeypatch.setattr(
        "app.agent.nodes.context_fetcher.subprocess.run",
        lambda *a, **k: _FakeCompletedProcess(stdout=unblocked),
    )
    ok, text, complete = _kubectl_snapshot(["get", "pods", "--all-namespaces"])
    assert ok is True and complete is True
    assert text == unblocked
    assert not any(marker in text for marker in MARKER_PATTERNS)
