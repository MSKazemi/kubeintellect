"""ACI mutating-verb chokepoint (v5 P3 Action/Trust plane).

A proposed cluster mutation is (1) stamped with a **rollback class** — how reversible it is —
and (2) routed through a single write-authority decision that composes the blast-radius/spend gate,
the action class's statistically **earned rung**, and reversibility, BEFORE anything executes.

⚠️ **Designed destination, not the live brake.** `decide_write`/`plan_mutation` have no production
caller yet: the A3 path today goes through `autonomy.watchtower` (ladder + allowlist +
`auto_write_permitted`). `earned_rung` therefore always arrives as its `L2` default. The store that
would earn it (`promotion_outcomes`, ADR-102) is no longer empty — it gained a writer and, on the
watchtower path, a reader that can **revoke** A3 (both behind `KI_V5_STATISTICAL_PROMOTION`) — but
nothing computes a rung for *this* signature, and nothing calls it. Wiring this up must also update
the write-gate paragraph in `docs/how-it-works.md` —
`tests/test_the_write_gate_doc_matches_the_wiring.py` fails until it does.

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
# stateful / cluster-scoped kinds whose deletion is irreversible (data / cascade loss), as
# canonical (folded) kinds — matched EXACTLY, never as a substring of the command.
_IRREVERSIBLE_TARGETS = frozenset({
    "persistentvolumeclaim", "persistentvolume", "namespace", "customresourcedefinition",
    "statefulset",
})

# kind aliases (short names; plural forms are folded by `fold_kind`).
_KIND_ALIASES = {
    "po": "pod", "svc": "service", "deploy": "deployment", "ds": "daemonset",
    "sts": "statefulset", "rs": "replicaset", "rc": "replicationcontroller",
    "cm": "configmap", "ns": "namespace", "pvc": "persistentvolumeclaim",
    "pv": "persistentvolume", "crd": "customresourcedefinition", "crds": "customresourcedefinition",
    "no": "node", "sa": "serviceaccount", "ing": "ingress", "cj": "cronjob",
    "hpa": "horizontalpodautoscaler", "pdb": "poddisruptionbudget", "netpol": "networkpolicy",
    "sc": "storageclass", "ep": "endpoints", "ev": "event", "limits": "limitrange",
    "quota": "resourcequota",
}


def fold_kind(raw: str) -> str:
    """Fold a resource kind: `PVC`, `pvc`, `persistentvolumeclaims` → `persistentvolumeclaim`.

    The single folding used by both `classify_rollback` here and the ADR-008 effect guard's
    canonical intent (`app/tools/effect_guard.py`), so the two cannot disagree on what a kind is.
    """
    head, dot, group = raw.strip().lower().partition(".")
    head = _KIND_ALIASES.get(head, head)
    if head not in _KIND_ALIASES.values():
        if head.endswith("ies"):
            head = head[:-3] + "y"
        elif head.endswith("sses"):
            head = head[:-2]
        elif head.endswith("s") and not head.endswith("ss"):
            head = head[:-1]
        head = _KIND_ALIASES.get(head, head)
    return head + (dot + group if dot else "")


# flags that take a value as the next token (so the value is not mistaken for a kind/name).
_VALUE_FLAGS = frozenset({
    "-n", "--namespace", "-l", "--selector", "--field-selector", "-o", "--output", "-c",
    "--container", "--context", "--cluster", "--user", "--kubeconfig", "--grace-period",
    "--timeout", "--cascade", "--request-timeout", "--as", "--as-group", "--as-uid", "--server",
    "-s", "--token", "--cache-dir", "--certificate-authority", "--client-certificate",
    "--client-key", "--tls-server-name", "--username", "--password", "--chunk-size",
})


def _delete_kinds(toks: list[str]) -> set[str] | None:
    """Folded kinds a `delete` names, or None when the target cannot be parsed (⇒ fail-closed).

    `-f`/`--filename`/`-k` name a manifest whose kinds are not visible here, so they are
    unparseable. Otherwise: `kind/name` operands carry their kind; else the first operand is a
    (comma-separated) kind list and the rest are names.
    """
    operands: list[str] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok.startswith("-") and tok != "-":
            name, eq, _ = tok.partition("=")
            if name in ("-f", "--filename", "-k", "--kustomize"):
                return None
            if not eq and name in _VALUE_FLAGS:
                i += 1
        else:
            operands.append(tok)
        i += 1
    if not operands:
        return None
    slashed = [o for o in operands if "/" in o]
    if slashed:
        raw = [o.partition("/")[0] for o in slashed]
    else:
        raw = operands[0].split(",")
    # `statefulset.apps` / `customresourcedefinitions.apiextensions.k8s.io`: the API group does
    # not change what the kind is (conservative for a custom resource that reuses a core name).
    return {fold_kind(k).partition(".")[0] for k in raw}


def classify_rollback(command: str) -> str:
    """Classify a mutating kubectl command by how safely it can be reverted (ADR-008)."""
    toks = command.strip().split()
    if toks and toks[0] == "kubectl":
        toks = toks[1:]
    if not toks:
        return IRREVERSIBLE          # unparseable ⇒ treat as unsafe (fail-closed)
    verb = toks[0].lower()
    if verb in _IRREVERSIBLE_VERBS:
        # a delete is irreversible if it hits stateful/cluster-scoped data; a bare pod delete is
        # versioned (the controller recreates it). Kinds are matched exactly after folding; a
        # target that cannot be parsed stays irreversible (fail-closed).
        kinds = _delete_kinds(toks[1:])
        if kinds is None or kinds & _IRREVERSIBLE_TARGETS:
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
