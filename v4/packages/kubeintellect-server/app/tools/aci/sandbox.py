"""Two-axis capability sandbox (v5 P3 Trust plane).

The second HITL axis (⊥ the per-verb-class approval policy in ``mutating.decide_write``): the agent
acts through an **impersonated ServiceAccount** scoped to a capability role, so the cluster's own
RBAC — not just the app's policy — bounds what any action can touch. Defense in depth: even a bug
that authorizes a write can't exceed the impersonated SA's rights (never cluster-admin).

Roles map to SAs (configurable); ``impersonation_args`` builds the ``--as`` flags; ``run_as`` runs
a command through the run_kubectl seam under that identity. Pure flag-building is unit-tested; the
RBAC enforcement itself is validated live against a kind cluster.
"""

from __future__ import annotations

from app.core.config import settings

READ_ONLY = "read-only"
NAMESPACE_WRITE = "namespace-write"
NEVER_ADMIN = "never-cluster-admin"

VALID_ROLES = (READ_ONLY, NAMESPACE_WRITE, NEVER_ADMIN)


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
    """Return ``command`` with the role's impersonation flags appended (no-op for unknown role)."""
    flags = impersonation_args(role)
    return f"{command.rstrip()} {flags}" if flags else command


def run_as(command: str, role: str, *, _runner=None) -> str:
    """Run ``command`` impersonating the capability role's ServiceAccount.

    Runs with hitl_bypass: in the sandbox, the impersonated SA's RBAC is the guard (a read-only SA
    is forbidden to mutate by the cluster, not by an app prompt), so app-level HITL is redundant
    here — the whole point is that RBAC bounds the identity regardless of app policy.
    """
    if _runner is None:
        from app.tools.kubectl_tool import run_kubectl
        def _runner(cmd: str) -> str:
            return run_kubectl.invoke({"command": cmd},
                                      config={"configurable": {"hitl_bypass": True}})
    return _runner(as_impersonated(command, role))
