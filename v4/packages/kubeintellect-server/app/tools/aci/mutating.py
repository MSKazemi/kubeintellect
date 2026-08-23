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
from app.tools.aci import kubectl_output as _out

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
    # False ⇒ the API server never saw the command (KubeIntellect refused it, or kubectl could not
    # reach the cluster). `ok` and `admission_denied` are then statements about nothing.
    validated: bool = True


def _with_server_dry_run(command: str) -> str:
    """Force `--dry-run=server` on ``command``, whatever dry-run flag it already carries.

    This used to be a substring test — `any(f in command for f in ("--dry-run=server",
    "--dry-run=client", "--dry-run"))` — which left the command untouched in three cases where the
    server-side validation this whole function exists for would never happen:

    - `--dry-run=none`, which real kubectl documents as the **default** (`--dry-run='none': Must
      be "none", "server", or "client"`, kubectl v1.36.3) — i.e. not a dry run at all;
    - a bare `--dry-run`, which real kubectl answers with *"--dry-run is deprecated and can be
      replaced with --dry-run=client"* — client-side, so admission is never consulted;
    - `--dry-run=client`, same reason;

    and it also fired on the string appearing inside an unrelated **value**
    (`kubectl label deploy/web team=--dry-run`).

    So: match whole tokens, and rewrite anything that is not already `--dry-run=server`. A caller
    hands this function the mutation it wants validated; the flag that makes the answer mean
    "the API server and its admission chain accepted this" is not negotiable.
    """
    toks = command.split()
    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "--dry-run":                       # `--dry-run client` / bare, pflag space form
            i += 2 if i + 1 < len(toks) and toks[i + 1] in ("client", "server", "none") else 1
            continue
        if t.startswith("--dry-run="):
            i += 1
            continue
        out.append(t)
        i += 1
    return " ".join([*out, "--dry-run=server"])


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
        return DryRunResult(False, False, f"dry-run error: {exc}", validated=False)
    if not _out.reached_cluster(out):
        # KubeIntellect refused it, or kubectl never got to the API server. Nothing was validated,
        # so this must not read as "would apply cleanly" — which is exactly what it used to do:
        # measured 2026-08-20, all five real refusal strings produced ok=True.
        first = next((ln.strip() for ln in out.splitlines() if ln.strip()), "(empty)")
        return DryRunResult(False, False, f"not validated: {first[:400]}", validated=False)
    low = out.lower()
    admission = any(m in low for m in _ADMISSION_MARKERS)
    ok = not admission and _out.classify_output(out) == _out.OK
    return DryRunResult(ok, admission, out.strip()[:2000])


def plan_mutation(
    command: str, *, earned_rung: str = "L2", budget: BudgetDecision | None = None,
    _runner=None,
) -> tuple[MutationProposal, DryRunResult | None]:
    """The full chokepoint: authorize (budget+rung+reversibility) → server-side dry-run.

    Only runs the dry-run when the write is authorized (not denied) — a denied write is never even
    validated against the cluster. Returns (proposal, dry_run|None).

    An `auto` decision is downgraded to `approve` when the dry-run could not run at all: auto is
    earned against evidence that the API server would accept the command, and there is none.
    """
    proposal = decide_write(command, earned_rung=earned_rung, budget=budget)
    if proposal.decision == "deny":
        return proposal, None
    dry_run = validate_mutation(command, _runner=_runner)
    if proposal.decision == "auto" and not dry_run.validated:
        # An unrun check is not a passed check. Auto-execution is earned against evidence that the
        # API server would accept the command; with no such evidence, a human decides.
        proposal = MutationProposal(
            proposal.command, proposal.rollback_class, "approve",
            f"{proposal.reason}, but the server-side dry-run never ran — approval required",
        )
    return proposal, dry_run
