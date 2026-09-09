"""Controller creation failures offer triage; normal rollout waits do not."""
from __future__ import annotations

import pytest

from app.agent.playbooks import get_playbook, match_playbooks


@pytest.mark.parametrize("message", [
    'Create Pod web-0 in StatefulSet web failed error: pods "web-0" is forbidden',
    'Create Claim data-web-0 for Pod web-0 in StatefulSet web failed error: exceeded quota',
])
def test_statefulset_creation_failure_offers_triage(message):
    # Controller FailedCreate event forms, not fabricated rollout-status events.
    events = f"1m Warning FailedCreate statefulset/web {message}\n"
    assert "StatefulSetRolloutStuck" in match_playbooks("", events)


@pytest.mark.parametrize("pods,events", [
    ("", ""),
    ("web-0 0/1 Pending 0 10s", ""),
    ("web-0 0/1 Running 0 10s", ""),
    ("", "Waiting for 1 pods to be ready..."),
    ("", "Waiting for partitioned roll out to finish: 1 out of 2 new pods have been updated..."),
    ("", "statefulset rolling update complete 3 pods at revision web-abc..."),
    ("", "Normal SuccessfulCreate statefulset/web create Pod web-0 in StatefulSet web successful"),
    ("", "Warning Unhealthy pod/web-0 Readiness probe failed: HTTP probe failed with statuscode: 503"),
    ("", "Warning FailedScheduling pod/web-0 pod has unbound immediate PersistentVolumeClaims"),
    ("", 'Warning FailedCreate replicaset/api Error creating: pods "api-123" is forbidden'),
    ("", 'Warning FailedCreate statefulset/web create Pod web-0 in StatefulSet web successful\nWarning Failed pod/api failed error: image missing'),
])
def test_statefulset_triage_does_not_infer_stall_from_insufficient_evidence(pods, events):
    assert "StatefulSetRolloutStuck" not in match_playbooks(pods, events)


def test_statefulset_guide_loads_without_claiming_automatic_stall_detection():
    playbook = get_playbook("StatefulSetRolloutStuck")
    assert playbook is not None
    assert playbook.detect is None
    assert playbook.investigation_steps
    assert playbook.expected_evidence
