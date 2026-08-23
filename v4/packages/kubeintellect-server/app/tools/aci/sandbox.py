"""Two-axis capability sandbox (v5 P3 Trust plane).

The second HITL axis (⊥ the per-verb-class approval policy in ``mutating.decide_write``): the agent
acts through an **impersonated ServiceAccount** scoped to a capability role, so the cluster's own
RBAC — not just the app's policy — bounds what any action can touch. Defense in depth: even a bug
that authorizes a write can't exceed the impersonated SA's rights (never cluster-admin).

Roles map to SAs (configurable); ``impersonation_args`` builds the ``--as`` flags; ``run_as`` runs
a command through the run_kubectl seam under that identity. Pure flag-building is unit-tested; the
RBAC enforcement itself is validated live against a kind cluster.

**`run_as` fails closed, because it is the seam that executes.** It runs with ``hitl_bypass`` — the
app-level approval gate is deliberately switched off on the grounds that the impersonated SA's RBAC
is the real guard. That trade is only sound while the impersonation is definitely there. It was
not: an unrecognised role produced no flags, `as_impersonated` documented itself as a "no-op", and
`run_as` executed the command anyway — **unimpersonated, with HITL already bypassed**, returning an
ordinary output string that said nothing about it. Measured 2026-08-20:
`run_as("delete deployment web -n prod", "typo")` sent exactly `delete deployment web -n prod` to
the seam. The role vocabulary makes that easy to hit by accident: this module's roles are
``read-only`` / ``namespace-write`` / ``never-cluster-admin``, while the API-key roles used
everywhere else in the codebase are ``readonly`` / ``operator`` / ``admin`` / ``superadmin`` — so
passing ``"readonly"``, the spelling the rest of the project uses, silently turned the sandbox off.

A command may also not bring its **own** identity. Real kubectl (v1.36.3) documents ``--as-group=[]``
as *"Group to impersonate for the operation, this flag can be repeated"*, so a command carrying
``--as-group=system:masters`` would defeat the one property this sandbox exists to guarantee, no
matter which SA the ``--as`` flag names.
"""

from __future__ import annotations

from app.core.config import settings

READ_ONLY = "read-only"
NAMESPACE_WRITE = "namespace-write"
NEVER_ADMIN = "never-cluster-admin"

VALID_ROLES = (READ_ONLY, NAMESPACE_WRITE, NEVER_ADMIN)

# kubectl's impersonation flags (`kubectl options`, v1.36.3). A sandboxed command must not carry
# any of them itself — the sandbox, not the command, decides who the command is.
_IDENTITY_FLAGS = frozenset({"--as", "--as-group", "--as-uid"})


class SandboxContractError(RuntimeError):
    """The sandbox was asked to run something it cannot bound. Never a cluster call."""


def sa_for_role(role: str) -> str:
    """The ServiceAccount name for a capability role. never-cluster-admin reuses the read-only SA
    (its defining property is the ABSENCE of cluster-admin, enforced by RBAC)."""
    if role == NAMESPACE_WRITE:
        return settings.KI_V5_SANDBOX_WRITER_SA
    return settings.KI_V5_SANDBOX_READONLY_SA


def impersonation_args(role: str) -> str:
    """kubectl ``--as`` flags to act as the role's ServiceAccount. Empty for an unknown role
    (fail-closed: no impersonation ⇒ caller must not proceed under the sandbox)."""
    if role not in VALID_ROLES:
        return ""
    ns = settings.KI_V5_SANDBOX_SA_NAMESPACE
    return f"--as=system:serviceaccount:{ns}:{sa_for_role(role)}"


def as_impersonated(command: str, role: str) -> str:
    """Return ``command`` with the role's impersonation flags appended.

    An unknown role yields the command unchanged — this is flag-building, and there are no flags to
    add. That is *not* a licence to run the result: `run_as` is the enforcing seam and raises.
    """
    flags = impersonation_args(role)
    return f"{command.rstrip()} {flags}" if flags else command


def _own_identity_flag(command: str) -> str | None:
    """The first impersonation flag the command sets for itself, if any."""
    for token in command.split():
        if token.split("=", 1)[0] in _IDENTITY_FLAGS:
            return token
    return None


def run_as(command: str, role: str, *, _runner=None) -> str:
    """Run ``command`` impersonating the capability role's ServiceAccount.

    Runs with hitl_bypass: in the sandbox, the impersonated SA's RBAC is the guard (a read-only SA
    is forbidden to mutate by the cluster, not by an app prompt), so app-level HITL is redundant
    here — the whole point is that RBAC bounds the identity regardless of app policy.

    Which is why this raises rather than degrades. Both halves of the trade have to hold: an
    unknown role, or a command that sets its own identity, means the cluster-side guard is not
    there — and the app-side one has already been given up. Nothing runs.
    """
    if role not in VALID_ROLES:
        raise SandboxContractError(
            f"unknown capability role {role!r} — nothing was run. Valid roles are "
            f"{', '.join(VALID_ROLES)}. (The API-key roles readonly/operator/admin/superadmin are "
            "a different vocabulary and are not accepted here.)"
        )
    own = _own_identity_flag(command)
    if own is not None:
        raise SandboxContractError(
            f"command sets its own identity ({own!r}) — nothing was run. The sandbox decides who a "
            "command is; --as-group=system:masters would defeat the never-cluster-admin property "
            "whatever the --as flag says."
        )
    flags = impersonation_args(role)
    if _runner is None:
        from app.tools.kubectl_tool import run_kubectl
        def _runner(cmd: str, _flags: str = flags) -> str:
            # `sandbox_identity` names the exact `--as` token this module built, so `run_kubectl`
            # can tell app-narrowed impersonation from a caller choosing its own identity. Both
            # keys ride the run config, which the graph injects and a model cannot write.
            return run_kubectl.invoke(
                {"command": cmd},
                config={"configurable": {"hitl_bypass": True, "sandbox_identity": _flags}},
            )
    sandboxed = as_impersonated(command, role)
    if "--as=" not in sandboxed:  # belt and braces: never execute outside the sandbox
        raise SandboxContractError(f"impersonation flags were not applied to {command!r}")
    return _runner(sandboxed)
