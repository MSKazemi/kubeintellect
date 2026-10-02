"""
Exactly-once guard for irreversible tool calls, and single-use approvals (ADR-008).

**Inert unless ``SELF_GOVERN_ENABLED``.** With the flag off, :func:`admit` returns :data:`PASS`
before looking at anything, :func:`settle` returns the output it was given, and the workflow
helpers leave the run config and the resume value exactly as they were.

The problem
-----------
An agent's *state* can be rolled back and retried; the cluster cannot. When a turn re-runs a
step — a LangGraph node re-executing on resume, a retry after an error, the model re-issuing a
call after a rollback — an irreversible call that already ran (``kubectl delete pvc …``) would
run again, and an approval that was already used would be honoured again ("Authority
Resurrection": LangGraph hands a resumed node the stored resume value by *position*, so a bare
``True`` approves whatever interrupt now sits at that index, not the one the human saw).

What this module does, at the guarded-tool boundary (`run_kubectl`, step 4c)
---------------------------------------------------------------------------
For a call `mutating.classify_rollback` puts in the **IRREVERSIBLE** class, it consults the
effect log (`app/db/effect_log.py`) keyed to the rollback point —
``{tool, canonical intent, branch id}`` within ``(session, rollback point)`` — and:

* **same canonical intent already ran** → return the recorded result, labelled as a replay,
  without executing;
* **a different intent of the same action class already ran here** (same verb, same resource
  kind, different target) → block, surface the prior record, and require an explicit **fork**
  (a human approval that records a new branch id) to proceed;
* **a recorded attempt whose outcome is unknown** (an intent with no effect, or an effect that
  failed) → neither replay nor re-execute silently: ask a human again;
* otherwise → obtain a **single-use approval token** bound to this exact call, consume it
  server-side, record the intent, execute, and record the effect (:func:`settle`).

A token is consumed exactly once (a unique index in the database enforces it, not just this
code). A human approval only counts if the resume value carries the token minted for *this*
call; a bare ``True`` or another call's token is rejected. An auto-approve / A3 session gets an
``a3`` token that is minted and consumed in the same transaction as the intent — the bypass
authorises one execution of one canonical intent, not every replay of it.

**Fail-closed.** If the effect log is not configured or cannot be read/written, the call is not
replayed and not executed on the strength of the bypass: it falls back to a HITL re-approval,
even on an auto-approve session. A call with no session id has no rollback point to key on and
is refused.

Canonicalization (rule-based)
-----------------------------
Intent-bearing: verb and subcommand, resource kind (aliases and plurals folded), names,
namespace, selectors, ``--all``/``-A``, replica count, cascade mode, image, container, revision,
``key=value`` assignments, and for a stdin manifest every object's kind/name/namespace plus a
digest of the manifest. Not intent-bearing (varies legitimately across runs): output format,
timeouts, grace periods, ``--wait``/``--now``/``--force``-style execution modifiers, server-set
metadata (``uid``, ``resourceVersion``, ``creationTimestamp``, ``generation``, ``managedFields``,
``status``, ``last-applied-configuration``) and any label/annotation/assignment whose key names a
request id, trace id, nonce or timestamp.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

import yaml
from langgraph.types import interrupt

from app.core.config import settings
from app.db import effect_log
from app.tools.aci import kubectl_output
from app.tools.aci.mutating import IRREVERSIBLE, classify_rollback, fold_kind as _kind
from app.utils.logger import get_logger
from app.utils.redact import redact_secrets

logger = get_logger(__name__)

TOOL = "run_kubectl"

#: Recorded results are redacted (they land in Postgres) and capped at run_kubectl's own cap.
_MAX_RESULT_CHARS = 8000

# ── Canonicalization ──────────────────────────────────────────────────────────

# Kind folding (`pvc` == `persistentvolumeclaims`) is `mutating.fold_kind` — ONE function shared
# with `classify_rollback`, so the guard's canonical key and the rollback class cannot disagree.

#: Verbs whose first operand is a subcommand, not a resource kind.
_SUBCOMMAND_VERBS = frozenset({"rollout", "set", "certificate", "auth", "config"})

#: Flags that carry the *intent* of the call, mapped to their canonical name.
_INTENT_FLAGS = {
    "-l": "selector", "--selector": "selector",
    "--field-selector": "field-selector",
    "--all": "all", "-A": "all-namespaces", "--all-namespaces": "all-namespaces",
    "--replicas": "replicas", "--cascade": "cascade", "--image": "image",
    "-c": "container", "--container": "container", "--to-revision": "to-revision",
}

#: Value-taking flags this module knows besides run_kubectl's own `_VALUE_FLAGS`.
_EXTRA_VALUE_FLAGS = frozenset({
    "-c", "--container", "--image", "--replicas", "--grace-period", "--timeout",
    "--to-revision", "--field-manager",
})

#: Server-set metadata — it differs between two submissions of the same intent.
_VOLATILE_METADATA = (
    "uid", "resourceVersion", "creationTimestamp", "generation", "managedFields", "selfLink",
    "deletionTimestamp", "deletionGracePeriodSeconds",
)

#: Label/annotation/assignment keys that vary per run by design (the last path segment,
#: lower-cased, with `-`, `_` and `.` removed).
_VOLATILE_KEYS = frozenset({
    "requestid", "reqid", "traceid", "spanid", "correlationid", "idempotencykey", "nonce",
    "runid", "timestamp", "ts", "time", "date", "restartedat", "generatedat", "createdat",
    "updatedat", "lastappliedconfiguration",
})


def _volatile_key(key: str) -> bool:
    tail = str(key).rsplit("/", 1)[-1].lower()
    squashed = tail.replace("-", "").replace("_", "").replace(".", "")
    return squashed in _VOLATILE_KEYS or squashed.endswith("timestamp")


def _strip_volatile(node: Any, under_meta_map: bool = False) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if under_meta_map and _volatile_key(k):
                continue
            out[k] = _strip_volatile(v, under_meta_map=k in ("labels", "annotations"))
        return out
    if isinstance(node, list):
        return [_strip_volatile(v) for v in node]
    return node


def _canonical_manifest(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    doc.pop("status", None)
    meta = doc.get("metadata")
    if isinstance(meta, dict):
        for k in _VOLATILE_METADATA:
            meta.pop(k, None)
    return _strip_volatile(doc)


def _value_flags() -> frozenset[str]:
    # Imported lazily: run_kubectl imports this module, so a top-level import would be a cycle.
    from app.tools.kubectl_tool import _VALUE_FLAGS
    return frozenset(_VALUE_FLAGS) | _EXTRA_VALUE_FLAGS


def canonical_intent(args: list[str], stdin: str | None = None) -> dict[str, Any]:
    """The intent-bearing content of a kubectl call, in a form two equivalent calls share."""
    tokens = list(args[1:] if args and args[0] == "kubectl" else args)
    value_flags = _value_flags()
    namespace = ""
    flags: dict[str, Any] = {}
    positionals: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-") and tok != "-":
            name, eq, val = tok.partition("=")
            if not eq and name in value_flags and i + 1 < len(tokens):
                val = tokens[i + 1]
                i += 1
            if name in ("-n", "--namespace"):
                namespace = val
            elif name in _INTENT_FLAGS:
                flags[_INTENT_FLAGS[name]] = val if eq or name in value_flags else True
            # anything else is an execution modifier — not part of the intent
        else:
            positionals.append(tok)
        i += 1

    verb = positionals[0].lower() if positionals else ""
    rest = positionals[1:]
    sub = rest.pop(0).lower() if verb in _SUBCOMMAND_VERBS and rest else ""

    assignments: list[str] = []
    operands: list[str] = []
    for tok in rest:
        key, eq, _ = tok.partition("=")
        if (eq and "/" not in key) or (tok.endswith("-") and len(tok) > 1 and "/" not in tok):
            if not _volatile_key(key.rstrip("-")):
                assignments.append(tok)
        else:
            operands.append(tok)

    targets: set[tuple[str, str, str]] = set()
    slashed = [o for o in operands if "/" in o]
    if slashed:
        for o in slashed:
            k, _, n = o.partition("/")
            targets.add((_kind(k), n, namespace))
    elif operands:
        kinds = [_kind(k) for k in operands[0].split(",")]
        names = operands[1:] or [""]
        targets.update((k, n, namespace) for k in kinds for n in names)

    manifests: list[str] = []
    if stdin:
        for doc in yaml.safe_load_all(stdin):
            if not isinstance(doc, dict):
                continue
            meta = doc.get("metadata") or {}
            targets.add((_kind(str(doc.get("kind", ""))), str(meta.get("name", "")),
                         str(meta.get("namespace", "") or namespace)))
            body = json.dumps(_canonical_manifest(doc), sort_keys=True, default=str)
            manifests.append(hashlib.sha256(body.encode()).hexdigest())

    return {
        "tool": TOOL,
        "verb": verb,
        "subcommand": sub,
        "namespace": namespace,
        "targets": [list(t) for t in sorted(targets)],
        "assignments": sorted(assignments),
        "flags": dict(sorted(flags.items())),
        "manifests": sorted(manifests),
    }


def canonical_key(intent: dict[str, Any]) -> str:
    body = json.dumps(intent, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def action_class(intent: dict[str, Any]) -> str:
    """What makes two calls attempts at *the same kind of thing*: tool, verb, subcommand, kinds.

    Two calls in one class with different canonical keys are the case ADR-008 blocks — a retry
    that drifted onto a different target. Calls in different classes are different actions.
    """
    kinds = sorted({t[0] for t in intent["targets"]})
    return ":".join([intent["tool"], intent["verb"], intent["subcommand"], ",".join(kinds)])


def is_irreversible(intent: dict[str, Any]) -> bool:
    """ADR-008 rollback class of the call, from its canonical (not raw) form.

    Built from the parsed verb and kinds rather than the raw string: `classify_rollback` reads
    the first token as the verb, so `kubectl -n prod delete pvc x` would otherwise be classified
    on `-n`, and a `delete -f -` would be classified without the kinds its manifest names.
    """
    kinds = ",".join(sorted({t[0] for t in intent["targets"]}))
    parts = [p for p in ("kubectl", intent["verb"], intent["subcommand"], kinds) if p]
    return classify_rollback(" ".join(parts)) == IRREVERSIBLE


# ── Admission ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Binding:
    session_id: str
    rollback_point: str
    branch_id: str
    action: str
    canonical_key: str
    intent: dict[str, Any] = field(compare=False)
    command: str = field(default="", compare=False)

    def fields(self) -> dict[str, Any]:
        # Redacted: these land in Postgres. The key is computed from the unredacted intent, so
        # two calls differing only in a secret value are still told apart.
        return {
            "tool": TOOL, "rollback_point": self.rollback_point, "branch_id": self.branch_id,
            "action": self.action, "canonical_key": self.canonical_key,
            "intent": _scrubbed(self.intent), "command": redact_secrets(self.command, max_chars=300),
        }


def _scrubbed(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value, max_chars=500) if value else value
    if isinstance(value, dict):
        return {k: _scrubbed(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrubbed(v) for v in value]
    return value


@dataclass(frozen=True)
class Ticket:
    store: Any
    binding: Binding
    token: str


@dataclass(frozen=True)
class Admission:
    """What `run_kubectl` does next.

    ``response`` set → return it, execute nothing. ``approved`` → a single-use approval was
    obtained and consumed here, so the legacy prompt must not ask again. Neither → today's path.
    """

    response: str | None = None
    approved: bool = False
    ticket: Ticket | None = None


PASS = Admission()


def _scope(config: Any) -> tuple[str | None, str, str | None]:
    conf = ((config or {}).get("configurable") or {}) if isinstance(config, dict) else {}
    session = conf.get("thread_id")
    rollback_point = str(conf.get("effect_rollback_point") or "-")
    branch = conf.get("branch_id")
    return (str(session) if session else None), rollback_point, (str(branch) if branch else None)


def _active_branch(rows: list[dict], rollback_point: str, explicit: str | None) -> str:
    """An explicit `branch_id` in the run config wins; else the newest fork at this point."""
    if explicit:
        return explicit
    branch = "main"
    for row in rows:
        p = row["payload"]
        if row["kind"] == effect_log.FORK and p.get("rollback_point") == rollback_point:
            branch = str(p.get("new_branch_id") or branch)
    return branch


@dataclass
class _View:
    effect: dict | None = None        # a successful recorded effect of this exact intent
    unknown: dict | None = None       # an attempt of this intent with no known-good outcome
    conflict: dict | None = None      # a different intent of the same action class
    issued: dict[str, dict] = field(default_factory=dict)   # token -> issued payload
    consumed: set[str] = field(default_factory=set)


def _view(rows: list[dict], b: Binding) -> _View:
    v = _View()
    effected: set[str] = set()
    intents: list[dict] = []
    failed: list[dict] = []
    for row in rows:
        kind, p = row["kind"], row["payload"]
        if kind == effect_log.APPROVAL_CONSUMED:
            v.consumed.add(str(p.get("token")))
            continue
        if p.get("rollback_point") != b.rollback_point or p.get("branch_id") != b.branch_id:
            continue
        if kind == effect_log.APPROVAL_ISSUED:
            v.issued[str(p.get("token"))] = p
            continue
        if kind not in (effect_log.INTENT, effect_log.EFFECT):
            continue
        same = p.get("canonical_key") == b.canonical_key
        if not same:
            if p.get("action") == b.action:
                v.conflict = row
            continue
        if kind == effect_log.INTENT:
            intents.append(row)
            continue
        effected.add(str(p.get("token")))
        if p.get("outcome") == kubectl_output.OK:
            v.effect = row
        else:
            failed.append(row)
    if v.effect is None:
        # Claimed but never recorded (a crash, a lost write), or recorded as failed: nobody knows
        # what the cluster did. Never a replay, never a silent re-run.
        unknown = failed + [r for r in intents if str(r["payload"].get("token")) not in effected]
        v.unknown = max(unknown, key=lambda r: int(r["seq"])) if unknown else None
    return v


def _mint(b: Binding, purpose: str, n: int) -> str:
    body = json.dumps([b.session_id, b.rollback_point, b.branch_id, b.canonical_key, purpose, n])
    prefix = "a3" if purpose == "a3" else "apv"
    return f"{prefix}-{hashlib.sha256(body.encode()).hexdigest()[:32]}"


def _pending_token(v: _View, b: Binding, purpose: str) -> str | None:
    for token, p in v.issued.items():
        if (p.get("canonical_key") == b.canonical_key and p.get("purpose") == purpose
                and token not in v.consumed):
            return token
    return None


def _issue(txn: Any, v: _View, b: Binding, purpose: str) -> str:
    token = _pending_token(v, b, purpose)
    if token:
        return token
    n = sum(1 for p in v.issued.values()
            if p.get("canonical_key") == b.canonical_key and p.get("purpose") == purpose)
    token = _mint(b, purpose, n)
    txn.append(effect_log.APPROVAL_ISSUED, {**b.fields(), "token": token, "purpose": purpose})
    v.issued[token] = {"canonical_key": b.canonical_key, "purpose": purpose}
    return token


def _summary(row: dict) -> str:
    p = row["payload"]
    return f"`{p.get('command') or '(redacted)'}` (effect-log seq {row['seq']}, branch {p.get('branch_id')})"


def _replay_text(row: dict) -> str:
    p = row["payload"]
    return (
        f"[Replayed — not re-executed] `{p.get('command') or '(redacted)'}` is irreversible and already ran "
        f"at this point in the conversation (effect-log seq {row['seq']}, recorded "
        f"{row.get('created_at') or 'earlier'}, branch {p.get('branch_id')}). Running it a second "
        "time is exactly what ADR-008 prevents, so the result recorded when it ran is returned "
        "instead. If it genuinely has to run again, ask for it in a new message.\n"
        f"--- recorded result ---\n{p.get('result', '(no result recorded)')}"
    )


def _rejected(cmd: str, reason: str) -> str:
    return (
        f"[Rejected] The approval presented for `{cmd}` is not valid for it: {reason}. "
        "Approvals are single-use and bound to one action (ADR-008); nothing was executed. "
        "Ask again to get a fresh approval."
    )


@dataclass
class _Plan:
    admission: Admission | None = None
    binding: Binding | None = None
    token: str = ""
    purpose: str = ""
    prompt: dict[str, Any] = field(default_factory=dict)
    prior: str = ""


def _plan(store: Any, session: str, rp: str, explicit: str | None, intent: dict, cmd: str,
          stdin: str | None, *, hitl_bypass: bool, always_confirm: bool) -> _Plan:
    with store.transaction(session) as txn:
        branch = _active_branch(txn.rows, rp, explicit)
        b = Binding(session, rp, branch, action_class(intent), canonical_key(intent), intent, cmd)
        v = _view(txn.rows, b)
        if v.effect is not None:
            logger.info(f"effect_guard: replaying recorded effect seq={v.effect['seq']} for {cmd!r}")
            return _Plan(admission=Admission(response=_replay_text(v.effect)))
        prior = ""
        if v.conflict is not None:
            purpose, prior = "fork", _summary(v.conflict)
        elif v.unknown is not None:
            purpose, prior = "reapprove", _summary(v.unknown)
        elif hitl_bypass and not always_confirm:
            # Auto-approve / A3: the bypass authorises ONE execution of THIS intent. The token is
            # issued, consumed and the intent claimed in one transaction under the session lock.
            n = sum(1 for p in v.issued.values()
                    if p.get("canonical_key") == b.canonical_key and p.get("purpose") == "a3")
            token = _mint(b, "a3", n)
            txn.append(effect_log.APPROVAL_ISSUED, {**b.fields(), "token": token, "purpose": "a3"})
            txn.append(effect_log.APPROVAL_CONSUMED, {**b.fields(), "token": token,
                                                      "outcome": "approved", "approver": "auto"})
            txn.append(effect_log.INTENT, {**b.fields(), "token": token})
            return _Plan(admission=Admission(approved=True, ticket=Ticket(store, b, token)))
        else:
            purpose = "approve"
        token = _issue(txn, v, b, purpose)

    if purpose == "fork":
        summary = (f"A different irreversible action already ran at this point: {prior}. "
                   f"Approving FORKS a new branch and runs: `{cmd}`")
    elif purpose == "reapprove":
        summary = (f"An earlier attempt of this irreversible action has no known-good outcome: "
                   f"{prior}. Check the cluster before approving it again: `{cmd}`")
    else:
        summary = f"About to run an irreversible action: `{cmd}`"
    prompt = {
        "type": "hitl", "command": cmd, "stdin": stdin, "risk_level": "high",
        "always_confirm": True, "human_summary": summary, "approval_token": token,
        "effect_guard": {"purpose": purpose, "rollback_point": rp, "branch_id": b.branch_id,
                         "prior": prior or None},
    }
    return _Plan(binding=b, token=token, purpose=purpose, prompt=prompt, prior=prior)


def _read_resume(value: Any) -> tuple[bool, str | None]:
    if isinstance(value, dict):
        return value.get("approved") is True, (str(value["approval_token"])
                                               if value.get("approval_token") else None)
    return value is True, None


def _redeem(store: Any, plan: _Plan, approved: bool, presented: str | None, cmd: str) -> Admission:
    b = plan.binding
    assert b is not None
    with store.transaction(b.session_id) as txn:
        v = _view(txn.rows, b)
        if not approved:
            if plan.token not in v.consumed:
                txn.append(effect_log.APPROVAL_CONSUMED, {**b.fields(), "token": plan.token,
                                                          "outcome": "denied"})
            if plan.purpose == "fork":
                return Admission(response=(
                    f"[Blocked] A different irreversible action already ran at this point: "
                    f"{plan.prior}. `{cmd}` targets something else, so it is not a retry of that "
                    "action; running it needs an explicit fork, which was not approved. Nothing "
                    "was executed."))
            return Admission(response="Action cancelled by user.")
        reason = ""
        if presented is None:
            reason = "it carries no approval token"
        elif presented != plan.token:
            reason = ("it was already consumed" if presented in v.consumed
                      else "it was issued for a different action")
        elif presented in v.consumed:
            reason = "it was already consumed"
        elif presented not in v.issued:
            reason = "it was never issued for this action"
        if reason:
            txn.append(effect_log.REFUSED, {**b.fields(), "token": presented or "",
                                            "expected_token": plan.token, "reason": reason})
            logger.warning(f"effect_guard: rejected approval for {cmd!r}: {reason}")
            return Admission(response=_rejected(cmd, reason))
        token = plan.token                         # == presented, checked just above
        if plan.purpose != "fork" and v.effect is not None:
            # Raced with a parallel attempt that already executed: replay, do not run twice.
            txn.append(effect_log.APPROVAL_CONSUMED, {**b.fields(), "token": token,
                                                      "outcome": "replayed"})
            return Admission(response=_replay_text(v.effect))
        txn.append(effect_log.APPROVAL_CONSUMED, {**b.fields(), "token": token,
                                                  "outcome": "approved", "approver": "human"})
        if plan.purpose == "fork":
            new_branch = f"br-{token[-12:]}"
            txn.append(effect_log.FORK, {**b.fields(), "token": token,
                                         "new_branch_id": new_branch, "prior": plan.prior})
            b = replace(b, branch_id=new_branch)
        txn.append(effect_log.INTENT, {**b.fields(), "token": token})
        return Admission(approved=True, ticket=Ticket(store, b, token))


def _reapprove_without_ledger(cmd: str, stdin: str | None, session: str, intent: dict,
                              reason: str) -> Admission:
    """Fail-closed: no ledger ⇒ no replay, no bypass. A human decides, every time."""
    logger.warning(f"effect_guard: effect log unavailable ({reason}) — HITL re-approval for {cmd!r}")
    token = "nolog-" + hashlib.sha256(
        json.dumps([session, canonical_key(intent)]).encode()).hexdigest()[:32]
    resume = interrupt({
        "type": "hitl", "command": cmd, "stdin": stdin, "risk_level": "high",
        "always_confirm": True, "approval_token": token,
        "human_summary": (
            f"The effect log is unavailable ({reason[:200]}), so KubeIntellect cannot tell "
            f"whether this irreversible action already ran. Approve only after checking the "
            f"cluster: `{cmd}`"),
        "effect_guard": {"purpose": "reapprove", "effect_log": "unavailable"},
    })
    approved, presented = _read_resume(resume)
    if not approved:
        return Admission(response="Action cancelled by user.")
    if presented != token:
        return Admission(response=_rejected(
            cmd, "it carries no approval token" if presented is None
            else "it was issued for a different action"))
    return Admission(approved=True)


def admit(cmd: str, args: list[str], stdin: str | None, config: Any, *, has_dry_run: bool,
          hitl_bypass: bool, always_confirm: bool) -> Admission:
    """Decide what `run_kubectl` does with a mutating call. :data:`PASS` when the flag is off."""
    if not settings.SELF_GOVERN_ENABLED or has_dry_run:
        return PASS
    try:
        intent = canonical_intent(args, stdin)
    except Exception as exc:
        # Cannot say what the call targets ⇒ cannot dedupe it ⇒ a human decides.
        return _reapprove_without_ledger(cmd, stdin, "-", {"raw": cmd},
                                         f"could not canonicalize the call: {exc}")
    if not is_irreversible(intent):
        return PASS
    session, rp, explicit = _scope(config)
    if not session:
        return Admission(response=(
            f"[Blocked] `{cmd}` is irreversible and this call has no session, so there is no "
            "rollback point to guarantee it runs at most once (ADR-008). Nothing was executed."))
    store = effect_log.get_store()
    if store is None:
        return _reapprove_without_ledger(cmd, stdin, session, intent,
                                         "it needs the flight recorder on Postgres")
    try:
        plan = _plan(store, session, rp, explicit, intent, cmd, stdin,
                     hitl_bypass=hitl_bypass, always_confirm=always_confirm)
    except effect_log.EffectLogUnavailable as exc:
        return _reapprove_without_ledger(cmd, stdin, session, intent, str(exc))
    if plan.admission is not None:
        return plan.admission
    # Outside any transaction: interrupt() raises GraphInterrupt on the first pass.
    approved, presented = _read_resume(interrupt(plan.prompt))
    try:
        return _redeem(store, plan, approved, presented, cmd)
    except effect_log.EffectLogUnavailable as exc:
        logger.warning(f"effect_guard: could not redeem approval for {cmd!r}: {exc}")
        return Admission(response=(
            f"[Blocked] The effect log became unavailable while redeeming the approval for "
            f"`{cmd}` ({exc}), so it could not be consumed as single-use. Nothing was executed; "
            "ask again to re-approve."))


def settle(admission: Admission, output: str) -> str:
    """Record the effect of an admitted call. Returns ``output`` unchanged; never raises.

    If the write fails the intent stays without an effect, which the next attempt reads as an
    unknown outcome and sends to a human — never as permission to run again.
    """
    ticket = admission.ticket
    if ticket is None:
        return output
    try:
        b = ticket.binding
        with ticket.store.transaction(b.session_id) as txn:
            txn.append(effect_log.EFFECT, {
                **b.fields(), "token": ticket.token,
                "outcome": kubectl_output.classify_output(output),
                "result": redact_secrets(output, max_chars=_MAX_RESULT_CHARS),
            })
    except Exception as exc:
        logger.error(f"effect_guard: effect NOT recorded for {ticket.binding.command!r}: {exc} — "
                     "the next attempt will require re-approval")
    return output


# ── Workflow helpers (the rollback point and the token-carrying resume) ──────


def stamp_rollback_point(config: Any, input_data: Any, graph_state: Any = None) -> None:
    """Put the id of the human message that opened this turn into the run config.

    A rollback point is a turn: retries, resumes and re-issued calls inside one turn share it;
    a new user message is a new instruction and a new rollback point. No-op with the flag off.
    """
    if not settings.SELF_GOVERN_ENABLED:
        return
    rp = None
    try:
        if isinstance(input_data, dict):
            msgs = input_data.get("messages") or []
            if msgs:
                if not getattr(msgs[0], "id", None):
                    msgs[0].id = str(uuid.uuid4())
                rp = msgs[0].id
        else:
            values = getattr(graph_state, "values", None) or {}
            for m in reversed(values.get("messages", []) or []):
                if getattr(m, "type", "") == "human":
                    rp = getattr(m, "id", None)
                    break
    except Exception as exc:
        logger.warning(f"effect_guard: could not identify the turn: {exc}")
    config.setdefault("configurable", {})["effect_rollback_point"] = f"turn:{rp}" if rp else "-"


def resume_value(approved: bool, graph_state: Any) -> Any:
    """The resume value for a pending HITL interrupt: bound to its token when it carries one."""
    if not settings.SELF_GOVERN_ENABLED or not approved:
        return approved
    try:
        for task in getattr(graph_state, "tasks", None) or []:
            for intr in getattr(task, "interrupts", None) or []:
                val = getattr(intr, "value", intr)
                if isinstance(val, dict) and val.get("type") == "hitl":
                    token = val.get("approval_token")
                    return {"approved": True, "approval_token": token} if token else approved
    except Exception as exc:
        logger.warning(f"effect_guard: could not read the pending interrupt: {exc}")
    return approved
