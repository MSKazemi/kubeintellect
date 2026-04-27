"""Unit tests for the context_fetcher snapshot scan (C2)."""
from __future__ import annotations

from app.agent.nodes.context_fetcher import _scan_snapshot


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
