"""Pass 89's guard turned the v5 capability sandbox off, and no test could see it.

`run_kubectl` refuses the connection/identity flag family, because which cluster a command talks
to and who it runs as are the deployment's decisions, not a caller's. That is right for a flag a
*model* wrote. It is wrong for `app/tools/aci/sandbox.py`, whose entire mechanism is to impersonate
a ServiceAccount holding **fewer** rights — and which runs with `hitl_bypass=True` precisely on the
grounds that the impersonated RBAC is the guard instead.

Measured 2026-08-20, after the connection-flag gate landed::

    run_as("get pods -n prod", "read-only")
      → "[Protected] '--as' is not permitted. …"
    kubectl invocations that actually reached subprocess: NONE

So the sandbox was dead: the app-side gate given up, the cluster-side gate never applied, and a
refusal string returned where the caller expected command output. `tests/test_sandbox.py` could not
notice — every one of its cases injects ``_runner=lambda cmd: …`` and so never crosses the seam
into the real tool. **A seam introduced for testability is a place a test can stop looking.**

The exemption added here is an exact match against a token the *application* placed in the run
config, which arrives the way `hitl_bypass` and `user_role` do — injected by the graph, never
writable by a model. Everything pass 89 refused is still refused; the tests below assert both
halves, because an exemption that quietly widens the guard is worse than the bug it fixes.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app.tools.aci.sandbox import (
    NAMESPACE_WRITE,
    READ_ONLY,
    impersonation_args,
    run_as,
)
from app.tools.kubectl_tool import (
    _authorised_identity,
    _identity_is_authorised,
    run_kubectl,
)

SA = impersonation_args(READ_ONLY)          # --as=system:serviceaccount:<ns>:<sa>


def _fake_kubectl():
    seen: list[list[str]] = []

    def _run(cmd, **kwargs):
        seen.append(cmd)
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "NAME   READY\nweb    1/1\n"
        proc.stderr = ""
        return proc
    return seen, _run


def _invoke(command: str, configurable: dict | None = None) -> str:
    return run_kubectl.invoke(
        {"command": command},
        config={"configurable": configurable or {}},
    )


# ── L1 · what counts as an application-placed identity ────────────────────────
class TestTheAuthorisedIdentityComesFromTheRunConfig:
    def test_no_config_authorises_nothing(self):
        assert _authorised_identity(None) is None

    def test_an_empty_config_authorises_nothing(self):
        assert _authorised_identity({"configurable": {}}) is None

    def test_a_sandbox_identity_is_read_back(self):
        assert _authorised_identity({"configurable": {"sandbox_identity": SA}}) == SA

    @pytest.mark.parametrize("value", [
        "--server=http://evil", "--kubeconfig=/tmp/x", "--as-group=system:masters",
        "system:masters", "", None, 7, ["--as=x"],
    ])
    def test_only_an_as_token_can_be_authorised(self, value):
        assert _authorised_identity({"configurable": {"sandbox_identity": value}}) is None


# ── L2 · exactly that token, and nothing beside it ────────────────────────────
class TestOnlyTheExactTokenPasses:
    def test_the_authorised_token_passes(self):
        assert _identity_is_authorised(["kubectl", "get", "pods", SA], SA) is True

    def test_a_different_service_account_does_not(self):
        assert _identity_is_authorised(
            ["kubectl", "get", "pods", "--as=system:serviceaccount:kube-system:admin"], SA) is False

    def test_a_second_identity_flag_alongside_it_does_not(self):
        assert _identity_is_authorised(
            ["kubectl", "get", "pods", SA, "--as-group=system:masters"], SA) is False

    def test_a_connection_flag_alongside_it_does_not(self):
        assert _identity_is_authorised(
            ["kubectl", "get", "pods", SA, "--server=http://evil"], SA) is False

    def test_the_same_token_twice_does_not(self):
        # kubectl takes the last value; two tokens are not "the one the app placed".
        assert _identity_is_authorised(["kubectl", "get", "pods", SA, SA], SA) is False

    def test_an_ordinary_flag_beside_it_is_fine(self):
        assert _identity_is_authorised(
            ["kubectl", "get", "pods", "-n", "prod", "-o", "wide", SA], SA) is True


# ── L3 · the gate honours the exemption, and only the exemption ───────────────
class TestTheToolAcceptsTheSandboxAndNothingElse:
    def test_the_sandbox_command_now_reaches_kubectl(self):
        seen, run = _fake_kubectl()
        with patch.object(subprocess, "run", run):
            out = _invoke(f"kubectl get pods -n prod {SA}", {"sandbox_identity": SA})
        assert "[Protected]" not in out
        assert seen and seen[0][-1] == SA

    def test_the_same_command_without_the_config_is_still_refused(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("must not run")):
            out = _invoke(f"kubectl get pods -n prod {SA}")
        assert out.startswith("[Protected]")

    def test_a_model_written_impersonation_is_still_refused(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("must not run")):
            out = _invoke("kubectl get pods --as=system:masters", {"sandbox_identity": SA})
        assert out.startswith("[Protected]")

    @pytest.mark.parametrize("extra", [
        "--as-group=system:masters", "--as-uid=0", "--server=http://attacker.example.com:8080",
        "--kubeconfig=/tmp/evil.yaml", "--token=abc", "--insecure-skip-tls-verify",
        "--context=prod-admin",
    ])
    def test_nothing_rides_along_with_the_authorised_token(self, extra):
        with patch.object(subprocess, "run", side_effect=AssertionError("must not run")):
            out = _invoke(f"kubectl get pods {SA} {extra}", {"sandbox_identity": SA})
        assert out.startswith("[Protected]")

    @pytest.mark.parametrize("flag", [
        "--server=http://attacker.example.com:8080", "--kubeconfig=/tmp/evil.yaml",
        "--token=abc", "--as=system:masters", "--insecure-skip-tls-verify",
    ])
    def test_pass_89_still_holds_for_every_flag_it_closed(self, flag):
        with patch.object(subprocess, "run", side_effect=AssertionError("must not run")):
            assert _invoke(f"kubectl get pods {flag}").startswith("[Protected]")

    def test_an_ordinary_command_is_unaffected(self):
        seen, run = _fake_kubectl()
        with patch.object(subprocess, "run", run):
            out = _invoke("kubectl get pods -n prod")
        assert "[Protected]" not in out and seen


# ── L4 · the seam a test must cross at least once ─────────────────────────────
class TestRunAsActuallyReachesKubectl:
    """These deliberately do NOT inject `_runner`. That injection is why the break was invisible."""

    def test_a_read_only_sandbox_read_runs(self):
        seen, run = _fake_kubectl()
        with patch.object(subprocess, "run", run):
            out = run_as("get pods -n prod", READ_ONLY)
        assert "[Protected]" not in out
        assert seen, "run_as reached kubectl not at all"

    def test_the_impersonation_is_on_the_argv_that_ran(self):
        seen, run = _fake_kubectl()
        with patch.object(subprocess, "run", run):
            run_as("get pods -n prod", READ_ONLY)
        assert seen[0][-1] == impersonation_args(READ_ONLY)

    def test_the_writer_role_impersonates_the_writer_sa(self):
        seen, run = _fake_kubectl()
        with patch.object(subprocess, "run", run):
            run_as("scale deploy web --replicas=3 -n prod", NAMESPACE_WRITE)
        argv = [c for c in seen if "scale" in c][0]
        assert argv[-1] == impersonation_args(NAMESPACE_WRITE)

    def test_the_sandbox_still_refuses_a_command_bringing_its_own_identity(self):
        from app.tools.aci.sandbox import SandboxContractError
        with patch.object(subprocess, "run", side_effect=AssertionError("must not run")):
            with pytest.raises(SandboxContractError):
                run_as("get pods --as-group=system:masters", READ_ONLY)

    def test_the_sandbox_still_refuses_an_unknown_role(self):
        from app.tools.aci.sandbox import SandboxContractError
        with patch.object(subprocess, "run", side_effect=AssertionError("must not run")):
            with pytest.raises(SandboxContractError):
                run_as("get pods -n prod", "readonly")   # the API-key spelling
