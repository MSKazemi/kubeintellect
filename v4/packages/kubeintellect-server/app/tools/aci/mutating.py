"""ACI mutating-verb chokepoint (v5 P3 Action/Trust plane).

Every proposed cluster mutation is (1) stamped with a **rollback class** — how reversible it is —
and (2) routed through a single write-authority decision that composes the blast-radius/spend gate,
the action class's statistically **earned rung**, and reversibility, BEFORE anything executes.

This module is the pure decision core (no cluster, no execution): `classify_rollback` maps a
kubectl command to its ADR-008 rollback class, and `decide_write` returns auto / approve / deny.
Actual execution, server-side `--dry-run`, and Kyverno/VAP admission are separately cluster-gated
(they need a live cluster) — this seam is what they plug into.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.autonomy.budget import BudgetDecision, gate_write

# ADR-008 rollback classes (also the ADR-102 L3→L4 sub-transitions).
VERSIONED_WORKLOAD = "versioned-workload"   # revert via controller rollout undo
DECLARATIVE_REVERT = "declarative-revert"   # revert by re-applying the prior manifest
IRREVERSIBLE = "irreversible"               # no safe automatic revert — never auto

# verb → rollback class. `set`/`scale`/`rollout` on a controller are versioned-workload; `apply`/
# `patch`/`edit`/`label`/`annotate` are declarative; deletes of stateful/cluster-scoped objects are
# irreversible.
_VERSIONED_VERBS = {"scale", "rollout", "set", "autoscale"}
_DECLARATIVE_VERBS = {"apply", "patch", "edit", "replace", "label", "annotate"}
_IRREVERSIBLE_VERBS = {"delete"}
# stateful / cluster-scoped kinds whose deletion is irreversible (data / cascade loss).
_IRREVERSIBLE_TARGETS = ("pvc", "persistentvolumeclaim", "pv", "persistentvolume", "namespace",
                         "ns", "crd", "customresourcedefinition", "statefulset", "sts")


def classify_rollback(command: str) -> str:
    """Classify a mutating kubectl command by how safely it can be reverted (ADR-008)."""
    toks = command.strip().split()
    if toks and toks[0] == "kubectl":
        toks = toks[1:]
    if not toks:
        return IRREVERSIBLE          # unparseable ⇒ treat as unsafe (fail-closed)
    verb = toks[0].lower()
    target = " ".join(toks[1:]).lower()
    if verb in _IRREVERSIBLE_VERBS:
        # a delete is irreversible if it hits stateful/cluster-scoped data; a bare pod delete is
        # versioned (the controller recreates it).
        if any(t in target for t in _IRREVERSIBLE_TARGETS):
            return IRREVERSIBLE
        return VERSIONED_WORKLOAD
    if verb in _VERSIONED_VERBS:
        return VERSIONED_WORKLOAD
    if verb in _DECLARATIVE_VERBS:
        return DECLARATIVE_REVERT
    return IRREVERSIBLE              # unknown mutation ⇒ fail-closed


@dataclass(frozen=True)
class MutationProposal:
    command: str
    rollback_class: str
    decision: str                    # "auto" | "approve" | "deny"
    reason: str = ""


def decide_write(
    command: str,
    *,
    earned_rung: str = "L2",
    budget: BudgetDecision | None = None,
) -> MutationProposal:
    """Compose the write-authority decision for a proposed mutation.

    - Any budget/kill/freeze denial ⇒ **deny** (fail-closed).
    - Irreversible ⇒ **approve** (HITL) always — never auto, regardless of earned rung.
    - Otherwise auto only if the class has earned L4 for this action; else **approve**.
    """
    rc = classify_rollback(command)
    gate = budget if budget is not None else gate_write()
    if not gate.allow:
        return MutationProposal(command, rc, "deny", gate.reason)
    if rc == IRREVERSIBLE:
        return MutationProposal(command, rc, "approve", "irreversible — human approval required")
    if earned_rung == "L4":
        return MutationProposal(command, rc, "auto", f"earned L4 for {rc}")
    return MutationProposal(command, rc, "approve", f"rung {earned_rung} < L4 — approval required")


# ── server-side dry-run validation (needs a cluster; runs INSIDE the tool per the P3 spec) ──

_ADMISSION_MARKERS = ("denied the request", "admission webhook", "forbidden", "is invalid",
                      "violat", "not allowed")


@dataclass(frozen=True)
class DryRunResult:
    ok: bool                       # the command validated server-side (would apply cleanly)
    admission_denied: bool         # rejected by admission (VAP/Kyverno/RBAC), not just a typo
    output: str


def _with_server_dry_run(command: str) -> str:
    """Append --dry-run=server unless a dry-run flag is already present."""
    if any(f in command for f in ("--dry-run=server", "--dry-run=client", "--dry-run")):
        return command
    return f"{command.rstrip()} --dry-run=server"


def validate_mutation(command: str, *, _runner=None) -> DryRunResult:
    """Validate a mutation against the live API server + admission via `--dry-run=server`.

    No cluster change occurs. `_runner` is injectable for tests; defaults to the run_kubectl seam
    (inheriting its injection guard + protected-namespace block + redaction).
    """
    if _runner is None:
        from app.tools.kubectl_tool import run_kubectl
        def _runner(cmd: str) -> str:
            return run_kubectl.invoke({"command": cmd})
    try:
        out = _runner(_with_server_dry_run(command))
    except Exception as exc:
        return DryRunResult(False, False, f"dry-run error: {exc}")
    low = out.lower()
    admission = any(m in low for m in _ADMISSION_MARKERS)
    ok = not admission and ("error" not in low) and ("exit=1" not in low)
    return DryRunResult(ok, admission, out.strip()[:2000])


def plan_mutation(
    command: str, *, earned_rung: str = "L2", budget: BudgetDecision | None = None,
    _runner=None,
) -> tuple[MutationProposal, DryRunResult | None]:
    """The full chokepoint: authorize (budget+rung+reversibility) → server-side dry-run.

    Only runs the dry-run when the write is authorized (not denied) — a denied write is never even
    validated against the cluster. Returns (proposal, dry_run|None).
    """
    proposal = decide_write(command, earned_rung=earned_rung, budget=budget)
    if proposal.decision == "deny":
        return proposal, None
    return proposal, validate_mutation(command, _runner=_runner)
