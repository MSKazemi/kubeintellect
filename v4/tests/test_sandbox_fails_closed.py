"""An unbounded command must not run — the sandbox is the only guard left at this point.

`run_as` executes with `hitl_bypass=True`. The app-level approval gate is deliberately given up
there, on the stated grounds that the impersonated ServiceAccount's RBAC is the real guard. That
trade only holds while the impersonation is definitely applied, and it was not:

    impersonation_args("typo")                          -> ""            (no flags)
    as_impersonated("delete deployment web -n prod", "typo")
                                                        -> "delete deployment web -n prod"
    run_as(..., "typo")   sent exactly that to run_kubectl, with HITL already bypassed.

Measured 2026-08-20 against the real `run_kubectl`: with `hitl_bypass=True` (what `run_as` passes)
that command runs straight through to execution, while the same command with `hitl_bypass=False`
stops at the approval interrupt — so the gate being skipped is real, not theoretical.

The role vocabulary makes it easy to hit by accident. This module's roles are `read-only` /
`namespace-write` / `never-cluster-admin`; the API-key roles used everywhere else in the codebase
are `readonly` / `operator` / `admin` / `superadmin`. Passing `"readonly"` — the spelling the rest
of the project uses — silently turned the sandbox off.

And a command must not bring its own identity: real kubectl (v1.36.3, `kubectl options`) documents
`--as-group=[]` as *"Group to impersonate for the operation, this flag can be repeated to specify
multiple groups"*, so `--as-group=system:masters` defeats the never-cluster-admin property whatever
the `--as` flag names.
"""
from __future__ import annotations

import pytest

from app.tools.aci import sandbox
from app.tools.aci.sandbox import (
    NAMESPACE_WRITE,
    NEVER_ADMIN,
    READ_ONLY,
    SandboxContractError,
    as_impersonated,
    run_as,
)

MUTATION = "delete deployment web -n prod"


def _never_called(cmd: str) -> str:
    raise AssertionError(f"the sandbox executed an unbounded command: {cmd!r}")


class TestAnUnknownRoleRunsNothing:
    @pytest.mark.parametrize("role", [
        "typo", "root", "", "cluster-admin",
        "readonly",   # the API-key vocabulary — the collision that makes this reachable
        "read_only", "Read-Only", "namespace_write",
    ])
    def test_it_raises_instead_of_running_unimpersonated(self, role):
        with pytest.raises(SandboxContractError) as exc:
            run_as(MUTATION, role, _runner=_never_called)
        assert "nothing was run" in str(exc.value)

    def test_the_error_names_the_roles_that_would_have_worked(self):
        with pytest.raises(SandboxContractError) as exc:
            run_as(MUTATION, "readonly", _runner=_never_called)
        message = str(exc.value)
        assert READ_ONLY in message and NAMESPACE_WRITE in message and NEVER_ADMIN in message


class TestACommandMayNotSetItsOwnIdentity:
    @pytest.mark.parametrize("flag", [
        "--as-group=system:masters",
        "--as-group system:masters",
        "--as=system:serviceaccount:kube-system:default",
        "--as system:admin",
        "--as-uid=0",
    ])
    def test_an_impersonation_flag_in_the_command_is_refused(self, flag):
        with pytest.raises(SandboxContractError) as exc:
            run_as(f"get pods -n prod {flag}", READ_ONLY, _runner=_never_called)
        assert "sets its own identity" in str(exc.value)

    def test_a_lookalike_value_is_not_an_identity_flag(self):
        # `--selector` values and labels may contain the text; only a real flag token counts.
        seen: list[str] = []
        run_as("get pods -l team=--as-group", READ_ONLY,
               _runner=lambda c: seen.append(c) or "ok")
        assert seen and "--as=system:serviceaccount:" in seen[0]


class TestTheSandboxStillWorks:
    @pytest.mark.parametrize("role", [READ_ONLY, NAMESPACE_WRITE, NEVER_ADMIN])
    def test_a_valid_role_runs_impersonated(self, role):
        seen: list[str] = []
        out = run_as("get pods -n demo", role, _runner=lambda c: seen.append(c) or "ok")
        assert out == "ok"
        assert seen[0].startswith("get pods -n demo --as=system:serviceaccount:")

    def test_never_cluster_admin_uses_the_read_only_service_account(self):
        seen: list[str] = []
        run_as("get pods", NEVER_ADMIN, _runner=lambda c: seen.append(c) or "ok")
        assert seen[0] == as_impersonated("get pods", READ_ONLY)

    def test_nothing_reaches_the_seam_without_an_as_flag(self):
        # Belt and braces: whatever a future refactor does to the builders, this seam checks.
        for role in (READ_ONLY, NAMESPACE_WRITE, NEVER_ADMIN):
            seen: list[str] = []
            run_as("get pods", role, _runner=lambda c: seen.append(c) or "ok")
            assert "--as=" in seen[0]

    def test_the_seam_checks_the_flags_itself_and_does_not_trust_the_builder(self, monkeypatch):
        """The property this asserts belongs to `run_as` alone.

        Without it, `run_as` is correct only because `impersonation_args` happens to be — and the
        first revert of the check failed **zero** tests, because the role guard above already keeps
        every valid role flagged. So drive the one case that isolates it: a valid role whose
        builder returns nothing, which is what a future refactor of `impersonation_args`,
        `sa_for_role` or `VALID_ROLES` would look like from here.
        """
        monkeypatch.setattr(sandbox, "as_impersonated", lambda command, role: command)
        with pytest.raises(SandboxContractError) as exc:
            run_as("get pods", READ_ONLY, _runner=_never_called)
        assert "impersonation flags were not applied" in str(exc.value)
