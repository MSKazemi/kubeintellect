"""Expiry evidence selects triage; generic TLS failures and future validity do not."""
from __future__ import annotations

import pytest

from app.agent.playbooks import get_playbook, match_playbooks


@pytest.mark.parametrize("message", [
    "x509: certificate has expired or is not yet valid: current time 2026-09-11T00:00:00Z is after 2026-09-10T00:00:00Z",
    "SSL certificate problem: certificate has expired",
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired (_ssl.c:1000)",
    "verify error:num=10:certificate has expired",
])
def test_expiry_error_selects_triage(message):
    assert "TlsCertificateExpired" in match_playbooks("", message)


@pytest.mark.parametrize("message", [
    "",
    "Verify return code: 0 (ok)",
    "SSL certificate verify ok.",
    "certificate expires in 30 days",
    "certificate has not expired",
    "x509: certificate signed by unknown authority",
    "x509: certificate is valid for other.example, not shop.example",
    "SSL certificate problem: certificate is not yet valid",
    "x509: certificate has expired or is not yet valid: current time 2026-09-10T00:00:00Z is before 2026-09-11T00:00:00Z",
    "x509: certificate has expired or is not yet valid",
    "x509: certificate has expired or is not yet valid: current time is before validity\nunrelated task is after deadline",
    "HTTP/1.1 503 Service Unavailable",
    "tls: handshake failure",
    "remote error: tls: bad certificate",
])
def test_other_tls_conditions_do_not_assert_expiry(message):
    assert "TlsCertificateExpired" not in match_playbooks("", message)


def test_tls_guide_loads_without_an_automatic_detector():
    guide = get_playbook("TlsCertificateExpired")
    assert guide is not None
    assert guide.detect is None
    assert guide.investigation_steps
    assert guide.expected_evidence
