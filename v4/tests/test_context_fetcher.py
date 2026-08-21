"""Unit tests for the context_fetcher snapshot scan (C2)."""
from __future__ import annotations

import subprocess

from app.agent.nodes.context_fetcher import (
    _SNAPSHOT_MAX_CHARS,
    _run_kubectl_snapshot,
    _scan_snapshot,
)

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


def test_scan_truncated_pod_list_is_not_clean() -> None:
    # A truncated listing may hide an unhealthy pod past the cap — the
    # all-clear signal must not survive truncation.
    has_issues, has_warnings, pod_count = _scan_snapshot(
        HEALTHY_PODS, NO_EVENTS, pods_truncated=True
    )
    assert has_issues is True
    assert has_warnings is False
    assert pod_count == 3  # visible rows are still counted


def test_scan_unavailable_pod_list_is_not_clean() -> None:
    has_issues, _, pod_count = _scan_snapshot(
        "(unavailable: boom)", NO_EVENTS, pods_unavailable=True
    )
    assert has_issues is True
    assert pod_count == 0


def test_scan_truncated_events_are_not_clean() -> None:
    _, has_warnings, _ = _scan_snapshot(
        HEALTHY_PODS, NO_EVENTS, events_truncated=True
    )
    assert has_warnings is True


def test_scan_unavailable_events_are_not_clean() -> None:
    _, has_warnings, _ = _scan_snapshot(
        HEALTHY_PODS, NO_EVENTS, events_unavailable=True
    )
    assert has_warnings is True


def test_scan_full_healthy_snapshot_stays_clean() -> None:
    # The conservative flags must not fire when nothing was truncated or lost.
    has_issues, has_warnings, pod_count = _scan_snapshot(HEALTHY_PODS, NO_EVENTS)
    assert has_issues is False
    assert has_warnings is False
    assert pod_count == 3


class _FakeCompletedProcess:
    def __init__(self, stdout: str, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr


def test_snapshot_cap_marks_truncation(monkeypatch) -> None:
    long_out = "NAMESPACE NAME READY STATUS RESTARTS AGE\n" + (
        "default app-1 1/1 Running 0 2h\n" * 1000
    )
    assert len(long_out) > _SNAPSHOT_MAX_CHARS

    def fake_run(*args: object, **kwargs: object) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(stdout=long_out)

    monkeypatch.setattr("app.agent.nodes.context_fetcher.subprocess.run", fake_run)
    result = _run_kubectl_snapshot(["get", "pods", "--all-namespaces"])
    assert result.truncated is True
    assert result.unavailable is False
    assert result.text == long_out[:_SNAPSHOT_MAX_CHARS]


def test_snapshot_failure_marks_unavailable(monkeypatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> _FakeCompletedProcess:
        raise subprocess.TimeoutExpired(cmd=["kubectl"], timeout=5)

    monkeypatch.setattr("app.agent.nodes.context_fetcher.subprocess.run", fake_run)
    result = _run_kubectl_snapshot(["get", "pods"])
    assert result.unavailable is True
    assert result.truncated is False
    assert "(unavailable:" in result.text
