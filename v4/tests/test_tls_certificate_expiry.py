"""Tests for the TLS certificate expiry playbook."""
from __future__ import annotations

from app.agent.playbooks import get_playbook, match_playbooks

HEALTHY_PODS = """\
NAMESPACE   NAME    READY   STATUS    RESTARTS   AGE
default     app-1   1/1     Running   0          2h
"""

TLS_FAILURE_EVENTS = """\
NAMESPACE   LAST SEEN   TYPE      REASON      OBJECT      MESSAGE
default     30s         Warning   Unhealthy   pod/app-1   Get "https://api.example.com/health": x509: certificate has expired or is not yet valid
"""


def test_tls_certificate_expiry_matches_certificate_failure() -> None:
    matched = match_playbooks(HEALTHY_PODS, TLS_FAILURE_EVENTS)
    assert "TLSCertificateExpiry" in matched


def test_tls_certificate_expiry_playbook_has_required_schema() -> None:
    pb = get_playbook("TLSCertificateExpiry")
    assert pb is not None
    assert pb.triggers
    assert pb.investigation_steps
    assert pb.expected_evidence
    assert pb.recommended_fix_template


def test_tls_certificate_expiry_does_not_match_healthy_events() -> None:
    events = """\
NAMESPACE   LAST SEEN   TYPE      REASON             OBJECT      MESSAGE
default     30s         Normal    Pulled             pod/app-1   Successfully pulled image
"""
    assert "TLSCertificateExpiry" not in match_playbooks(HEALTHY_PODS, events)
