"""PDB refusal text routes to investigation, without inventing a watch signal."""
from __future__ import annotations

import pytest

from app.agent.playbooks import get_playbook, match_playbooks


@pytest.mark.parametrize("message", [
    "Cannot evict pod as it would violate the pod's disruption budget.",
    'Cannot disrupt Node: PDB "default/api" prevents pod evictions',
    'pdb default/inflate-pdb prevents pod evictions',
])
def test_pdb_refusal_routes_to_playbook(message):
    # Explicitly supplied diagnostic text; the normal snapshot only collects
    # Warning events, not kubectl drain stderr or Karpenter's Normal events.
    assert "PodDisruptionBudgetBlocking" in match_playbooks("", message)


@pytest.mark.parametrize("message", [
    "Cannot evict pod: Too Many Requests",
    "The node was low on resource: memory",
    "Cannot disrupt Node: state node is marked for deletion",
    "DisruptionBlocked: do-not-disrupt annotation",
    "NAME MIN AVAILABLE MAX UNAVAILABLE ALLOWED DISRUPTIONS AGE\napi 1 N/A 0 3d",
    "",
])
def test_pdb_playbook_ignores_unrelated_or_insufficient_evidence(message):
    assert "PodDisruptionBudgetBlocking" not in match_playbooks("", message)


def test_pdb_playbook_is_available_without_a_fabricated_watch_detector():
    playbook = get_playbook("PodDisruptionBudgetBlocking")
    assert playbook is not None
    assert playbook.detect is None
    assert playbook.investigation_steps
    assert playbook.expected_evidence
