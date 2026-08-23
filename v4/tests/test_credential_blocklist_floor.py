"""Credential types must stay blocked whatever the deployment configures.

`KUBECTL_BLOCKED_RESOURCES` *replaces* the blocklist rather than extending it, and the Helm
values file used to say "Override to add tenant-specific or environment-specific namespaces" —
so an operator following the documentation and adding `configmap` silently removed `secret`.
Measured 2026-08-19 (pass 50) through the real `run_kubectl`: reading every Secret in the
cluster, listing ServiceAccounts, and reading this release's *own* API keys all went from
BLOCKED to ALLOWED, with no warning anywhere and the guard's own message still promising that
Secrets are "shielded from inspection to protect cluster credentials".

Nothing failed when this broke — that is the point of the tests below. A blocklist that is
merely *usually* right reads exactly like one that is always right, right up until someone
tunes it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.core.config import ALWAYS_BLOCKED_RESOURCES, Settings

_CREDENTIAL_COMMANDS = [
    "kubectl get secrets -n prod -o yaml",
    "kubectl get secret my-secret -n default",
    "kubectl describe secret ki-secrets -n kubeintellect",
    "kubectl get serviceaccounts -n prod",
    "kubectl get serviceaccount default -n default",
]


def _settings(**env: str) -> Settings:
    return Settings(**env)  # type: ignore[arg-type]


class TestTheFloorCannotBeConfiguredAway:
    def test_an_operator_narrowing_the_list_still_blocks_credentials(self):
        """The exact mistake the old values.yaml invited: 'add' configmap, lose secret."""
        blocked = _settings(KUBECTL_BLOCKED_RESOURCES="configmap,configmaps").kubectl_blocked_resources
        assert {"secret", "secrets", "serviceaccount", "serviceaccounts"} <= blocked
        assert "configmap" in blocked, "the operator's own additions must still apply"

    def test_an_empty_blocklist_still_blocks_credentials(self):
        assert ALWAYS_BLOCKED_RESOURCES <= _settings(
            KUBECTL_BLOCKED_RESOURCES="").kubectl_blocked_resources

    def test_a_blocklist_of_only_whitespace_still_blocks_credentials(self):
        assert ALWAYS_BLOCKED_RESOURCES <= _settings(
            KUBECTL_BLOCKED_RESOURCES=" , , ").kubectl_blocked_resources

    def test_the_floor_names_every_credential_carrying_type(self):
        """Both spellings of each: the guard matches the literal token the user typed."""
        assert ALWAYS_BLOCKED_RESOURCES == {
            "secret", "secrets", "serviceaccount", "serviceaccounts"
        }


class TestTheGuardActuallyRefusesTheCommand:
    """The set being right is not the property that matters — refusing the command is."""

    @pytest.mark.parametrize("command", _CREDENTIAL_COMMANDS)
    def test_credential_reads_are_refused_under_a_narrowed_blocklist(self, command):
        import app.core.config as cfg
        from app.tools import kubectl_tool

        narrowed = _settings(KUBECTL_BLOCKED_RESOURCES="configmap")
        with patch.object(cfg, "settings", narrowed), \
             patch.object(kubectl_tool, "settings", narrowed), \
             patch("subprocess.run") as run:
            proc = MagicMock()
            proc.stdout, proc.stderr, proc.returncode = "<REAL CLUSTER DATA>", "", 0
            run.return_value = proc
            result = str(kubectl_tool.run_kubectl.invoke({"command": command, "stdin": None}))

        assert "[Protected]" in result, f"{command!r} was not refused: {result[:120]}"
        assert "REAL CLUSTER DATA" not in result
        run.assert_not_called()


class TestNamespacesDeliberatelyHaveNoFloor:
    """Recorded as a decision, not an oversight — so nobody 'fixes' it later by accident."""

    def test_an_operator_may_still_unblock_an_infrastructure_namespace(self):
        blocked = _settings(KUBECTL_BLOCKED_NAMESPACES="tenant-a").kubectl_blocked_namespaces
        assert blocked == {"tenant-a"}, (
            "Namespaces stay fully operator-controlled: letting the agent investigate "
            "`monitoring` is a legitimate choice, whereas letting it read Secrets is not. "
            "Credential protection lives in ALWAYS_BLOCKED_RESOURCES instead."
        )
