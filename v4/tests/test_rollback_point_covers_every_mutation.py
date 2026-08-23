"""`flight-recorder.md`: "Before **every mutating `kubectl` command** … the tool layer captures
the current YAML of the targeted objects."

The arming condition was `verb in _HIGH_RISK or verb in _MEDIUM_RISK` — the enumerated deny-list
that the risk gate itself stopped relying on when it moved to `_is_write_verb`. Two consumers of
"is this destructive", two different answers. Measured 2026-08-20:

    kubectl delete pod api-1              rollback armed
    kubectl rollout restart deploy/api    NOT armed   ← the ordinary way to restart a workload
    kubectl rollout undo deploy/api       NOT armed   ← and the ordinary way to roll one back
    kubectl rollout pause deploy/api      NOT armed
    kubectl frobnicate thing              NOT armed   ← any verb this build does not know

Two further silent no-ops in the capture itself, which is `except Exception: pass` throughout, so
a malformed target command arms nothing while looking like it armed something:

- `rollout` puts a subcommand before its target, and only the verb was dropped, so the pre-state
  read became `kubectl get restart deployment/api -o yaml` — rejected by kubectl;
- `kubectl label pod api-1 tier=web` kept `tier=web` as if it were a resource name, giving
  `kubectl get pod api-1 tier=web -o yaml` — also rejected. A Kubernetes name cannot contain `=`.

Best-effort is a deliberate and correct design (`the rollback point is a safety net, not a gate`)
but best-effort silence plus a wrong command is indistinguishable from a cluster that simply had
nothing to capture.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import app.tools.kubectl_tool as kt

_CONFIG = {"configurable": {"user_role": "admin", "thread_id": "t", "hitl_bypass": True}}


def _pre_state_reads(command: str, stdin: str | None = None) -> list[str]:
    """Every `kubectl get … -o yaml` the tool issues before running the mutation."""
    seen: list[str] = []

    def fake_run(cmd, *_a, **_k):
        seen.append(" ".join(cmd) if isinstance(cmd, list) else str(cmd))
        proc = MagicMock()
        proc.stdout, proc.stderr, proc.returncode = "apiVersion: v1\n", "", 0
        return proc

    with patch("subprocess.run", fake_run):
        kt.run_kubectl.invoke({"command": command, "stdin": stdin}, config=_CONFIG)
    return [c for c in seen if c.startswith("kubectl get") and c.endswith("-o yaml")]


class TestEveryMutationArmsAPoint:
    @pytest.mark.parametrize("command", [
        "kubectl delete pod api-1 -n prod",
        "kubectl patch deployment api -n prod -p {}",
        "kubectl scale deployment api --replicas=3 -n prod",
        "kubectl label pod api-1 tier=web -n prod",
        "kubectl annotate deployment api note=x -n prod",
        "kubectl rollout restart deployment/api -n prod",
        "kubectl rollout undo deployment/api -n prod",
        "kubectl rollout pause deployment/api -n prod",
        "kubectl -n prod rollout restart deploy/api",
        "kubectl frobnicate thing -n prod",
    ])
    def test_a_pre_state_read_is_issued(self, command):
        assert _pre_state_reads(command), f"no rollback point armed for {command!r}"


class TestTheCapturedCommandIsOneKubectlWouldAccept:
    @pytest.mark.parametrize("command,expected", [
        ("kubectl delete pod api-1 -n prod", "kubectl get pod api-1 -n prod -o yaml"),
        ("kubectl rollout restart deployment/api -n prod",
         "kubectl get deployment/api -n prod -o yaml"),
        ("kubectl rollout undo deployment/api -n prod",
         "kubectl get deployment/api -n prod -o yaml"),
        ("kubectl label pod api-1 tier=web -n prod", "kubectl get pod api-1 -n prod -o yaml"),
        ("kubectl annotate deployment api note=x -n prod",
         "kubectl get deployment api -n prod -o yaml"),
    ])
    def test_the_subcommand_and_key_value_pairs_are_not_mistaken_for_resources(
        self, command, expected
    ):
        assert _pre_state_reads(command)[0] == expected

    def test_a_slash_bearing_target_is_not_dropped_as_a_key_value_pair(self):
        """`deployment/api` contains no `=`; the key=value filter must not touch it."""
        assert "deployment/api" in _pre_state_reads("kubectl rollout restart deployment/api")[0]


class TestReadsAndDryRunsArmNothing:
    @pytest.mark.parametrize("command", [
        "kubectl get pods -n prod",
        "kubectl describe deployment api -n prod",
        "kubectl logs api-1 -n prod",
        "kubectl rollout status deployment/api -n prod",
        "kubectl rollout history deployment/api -n prod",
        "kubectl delete pod api-1 -n prod --dry-run=server",
    ])
    def test_no_pre_state_read_is_issued(self, command):
        assert _pre_state_reads(command) == [], f"armed a rollback point for {command!r}"


class TestTheArmingConditionMatchesTheHitlGate:
    """One definition of "is this a write", not two that drift."""

    @pytest.mark.parametrize("command", [
        "kubectl rollout restart deployment/api -n prod",
        "kubectl label pod api-1 tier=web -n prod",
        "kubectl frobnicate thing -n prod",
        "kubectl get pods -n prod",
        "kubectl rollout status deployment/api -n prod",
    ])
    def test_armed_exactly_when_the_command_is_a_write(self, command):
        tokens = command.split()
        is_write = kt._is_write_verb(kt._extract_verb(tokens), tokens)
        assert bool(_pre_state_reads(command)) is is_write
