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


def test_all_playbooks_load() -> None:
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
        "ServiceNoEndpoints",
        "CommandHardcodedFailure",
        "InitContainerFailing",
        "JobBackoffLimitExceeded",
        "QuotaExceeded",
        "Evicted",
        "WebhookAdmissionRejected",
        "NodeNotReady",
    }
    assert expected.issubset(names), f"missing playbooks: {expected - names}"


# ── F1: ServiceNoEndpoints triggers ───────────────────────────────────────────


def test_match_service_no_endpoints_by_event_reason() -> None:
    events = (
        "NAMESPACE   LAST SEEN  TYPE     REASON                  OBJECT                MESSAGE\n"
        "default     30s        Warning  FailedToUpdateEndpoint  endpointslice/svc-x   could not update\n"
    )
    matched = match_playbooks(HEALTHY_PODS, events)
    assert "ServiceNoEndpoints" in matched


def test_match_service_no_endpoints_by_event_message() -> None:
    events = (
        "NAMESPACE   LAST SEEN  TYPE     REASON   OBJECT     MESSAGE\n"
        "default     30s        Warning  Failed   svc/svc-x  Service has no endpoints\n"
    )
    matched = match_playbooks(HEALTHY_PODS, events)
    assert "ServiceNoEndpoints" in matched


# ── F3: CommandHardcodedFailure triggers ──────────────────────────────────────


def test_match_command_hardcoded_failure_by_crashloop() -> None:
    matched = match_playbooks(CRASHLOOP_PODS, NO_EVENTS)
    assert "CommandHardcodedFailure" in matched


def test_match_command_hardcoded_failure_by_error_status() -> None:
    pods = (
        "NAMESPACE   NAME    READY   STATUS  RESTARTS   AGE\n"
        "default     app-1   0/1     Error   3          1m\n"
    )
    matched = match_playbooks(pods, NO_EVENTS)
    assert "CommandHardcodedFailure" in matched


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


# ── New playbooks (B-track) ────────────────────────────────────────────────────

INIT_CONTAINER_PODS = """\
NAMESPACE   NAME    READY   STATUS      RESTARTS   AGE
default     app-1   0/1     Init:0/1    0          2m
"""

JOB_BACKOFF_EVENTS = """\
NAMESPACE   LAST SEEN   TYPE      REASON                  OBJECT    MESSAGE
default     5s          Warning   BackoffLimitExceeded    job/etl   Job has reached the specified backoff limit
"""

QUOTA_EXCEEDED_EVENTS = """\
NAMESPACE   LAST SEEN   TYPE      REASON        OBJECT                MESSAGE
default     10s         Warning   FailedCreate  replicaset/app-rs-1  exceeded quota: default-quota, requested: cpu=500m, used: cpu=1900m, limited: cpu=2
"""

EVICTED_PODS = """\
NAMESPACE   NAME    READY   STATUS    RESTARTS   AGE
default     app-1   0/1     Evicted   0          10m
"""

WEBHOOK_EVENTS = """\
NAMESPACE   LAST SEEN   TYPE      REASON        OBJECT        MESSAGE
default     3s          Warning   FailedCreate  pod/app-pod   admission webhook "policy.example.com" denied the request: container must set runAsNonRoot
"""

NODE_NOT_READY_EVENTS = """\
NAMESPACE   LAST SEEN   TYPE      REASON        OBJECT         MESSAGE
default     1m          Warning   NodeNotReady  node/worker-1  Node worker-1 status is now: NodeNotReady
"""


def test_match_init_container_failing_by_pod_status() -> None:
    matched = match_playbooks(INIT_CONTAINER_PODS, NO_EVENTS)
    assert "InitContainerFailing" in matched


def test_match_init_container_failing_by_event_message() -> None:
    events = (
        "NAMESPACE   LAST SEEN  TYPE     REASON   OBJECT     MESSAGE\n"
        "default     30s        Warning  BackOff  pod/app-1  Back-off restarting failed init container\n"
    )
    matched = match_playbooks(HEALTHY_PODS, events)
    assert "InitContainerFailing" in matched


def test_match_job_backoff_limit_exceeded() -> None:
    matched = match_playbooks(HEALTHY_PODS, JOB_BACKOFF_EVENTS)
    assert "JobBackoffLimitExceeded" in matched


def test_match_quota_exceeded_by_event_message() -> None:
    matched = match_playbooks(HEALTHY_PODS, QUOTA_EXCEEDED_EVENTS)
    assert "QuotaExceeded" in matched


def test_match_evicted_by_pod_status() -> None:
    matched = match_playbooks(EVICTED_PODS, NO_EVENTS)
    assert "Evicted" in matched


def test_match_evicted_by_event_message() -> None:
    events = (
        "NAMESPACE   LAST SEEN  TYPE     REASON   OBJECT     MESSAGE\n"
        "default     5s         Warning  Evicted  pod/app-1  The node was low on resource: memory.\n"
    )
    matched = match_playbooks(HEALTHY_PODS, events)
    assert "Evicted" in matched


def test_match_webhook_admission_rejected() -> None:
    matched = match_playbooks(HEALTHY_PODS, WEBHOOK_EVENTS)
    assert "WebhookAdmissionRejected" in matched


def test_match_node_not_ready_by_event_reason() -> None:
    matched = match_playbooks(HEALTHY_PODS, NODE_NOT_READY_EVENTS)
    assert "NodeNotReady" in matched


def test_match_node_not_ready_by_event_message() -> None:
    events = (
        "NAMESPACE   LAST SEEN  TYPE     REASON   OBJECT        MESSAGE\n"
        "default     2m         Warning  Unknown  node/node-1   Kubelet not posting node status. Node condition unknown\n"
    )
    matched = match_playbooks(HEALTHY_PODS, events)
    assert "NodeNotReady" in matched
