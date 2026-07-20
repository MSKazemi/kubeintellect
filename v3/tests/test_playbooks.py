"""Unit tests for the playbook library (C4a)."""
from __future__ import annotations

from app.agent.playbooks import get_playbook, list_playbooks, match_playbooks


CRASHLOOP_PODS = """\
NAMESPACE   NAME    READY   STATUS             RESTARTS   AGE
default     app-1   0/1     CrashLoopBackOff   5          10m
"""

OOMKILLED_PODS = """\
NAMESPACE   NAME    READY   STATUS      RESTARTS   AGE
default     app-1   0/1     OOMKilled   2          5m
"""

IMAGEPULL_PODS = """\
NAMESPACE   NAME    READY   STATUS             RESTARTS   AGE
default     app-1   0/1     ImagePullBackOff   0          1m
"""

PENDING_PODS = """\
NAMESPACE   NAME    READY   STATUS    RESTARTS   AGE
default     app-1   0/1     Pending   0          1m
"""

CONFIGERROR_PODS = """\
NAMESPACE   NAME    READY   STATUS                         RESTARTS   AGE
default     app-1   0/1     CreateContainerConfigError     0          1m
"""

INSUFFICIENT_RESOURCES_EVENTS = """\
NAMESPACE   LAST SEEN   TYPE      REASON            OBJECT     MESSAGE
default     30s         Warning   FailedScheduling  pod/app-1  0/3 nodes are available: 3 Insufficient cpu.
"""

UNHEALTHY_EVENTS = """\
NAMESPACE   LAST SEEN   TYPE      REASON      OBJECT     MESSAGE
default     30s         Warning   Unhealthy   pod/app-1  Readiness probe failed: HTTP probe failed with statuscode: 500
"""

HEALTHY_PODS = """\
NAMESPACE   NAME    READY   STATUS    RESTARTS   AGE
default     app-1   1/1     Running   0          2h
"""

NO_EVENTS = "No resources found in default namespace."


def test_all_ten_playbooks_load() -> None:
    names = {pb.name for pb in list_playbooks()}
    expected = {
        "CrashLoopBackOff",
        "OOMKilled",
        "ImagePullBackOff",
        "PendingInsufficientResources",
        "PendingSchedulingConstraints",
        "CreateContainerConfigError",
        "ContainerCreatingStuck",
        "TerminatingStuck",
        "ReadinessProbeFailing",
        "ServiceUnreachable",
    }
    assert expected.issubset(names), f"missing playbooks: {expected - names}"


def test_each_playbook_has_complete_schema() -> None:
    for pb in list_playbooks():
        assert pb.name
        assert pb.triggers, f"{pb.name} has no triggers"
        assert pb.investigation_steps, f"{pb.name} has no investigation_steps"
        assert pb.expected_evidence, f"{pb.name} has no expected_evidence"
        assert pb.recommended_fix_template, f"{pb.name} has no recommended_fix_template"


def test_match_crashloop_by_pod_status() -> None:
    matched = match_playbooks(CRASHLOOP_PODS, NO_EVENTS)
    assert "CrashLoopBackOff" in matched


def test_match_oomkilled_by_pod_status() -> None:
    matched = match_playbooks(OOMKILLED_PODS, NO_EVENTS)
    assert "OOMKilled" in matched


def test_match_imagepullbackoff() -> None:
    matched = match_playbooks(IMAGEPULL_PODS, NO_EVENTS)
    assert "ImagePullBackOff" in matched


def test_match_configerror() -> None:
    matched = match_playbooks(CONFIGERROR_PODS, NO_EVENTS)
    assert "CreateContainerConfigError" in matched


def test_match_pending_resources_by_event_message() -> None:
    matched = match_playbooks(PENDING_PODS, INSUFFICIENT_RESOURCES_EVENTS)
    assert "PendingInsufficientResources" in matched


def test_match_readiness_probe_by_event_reason() -> None:
    matched = match_playbooks(HEALTHY_PODS, UNHEALTHY_EVENTS)
    assert "ReadinessProbeFailing" in matched


def test_no_match_on_healthy_cluster() -> None:
    matched = match_playbooks(HEALTHY_PODS, NO_EVENTS)
    assert matched == []


def test_get_playbook_returns_known() -> None:
    pb = get_playbook("CrashLoopBackOff")
    assert pb is not None
    assert "describe pod" in pb.investigation_steps[0]


def test_get_playbook_returns_none_for_unknown() -> None:
    assert get_playbook("DoesNotExist") is None
