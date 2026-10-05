"""Verification ladder, read side (v5 P2; 01-architecture §3.6).

Two defenses against a confident-but-ungrounded RCA — the failure mode the eval work flagged
(judge ≠ ground truth on remediation):

- ``evaluate_goal``   — a per-turn goal evaluator / stop-gate: does the gathered evidence actually
  address the objective, or should the loop keep gathering? (module-level; graph wiring of the
  stop-gate is intentionally deferred — see note below.)
- ``review_rca``      — an ADVERSARIAL fresh-context reviewer: given ONLY the claim + the evidence
  (never the investigation's own reasoning chain), find claims the evidence does not support.

Both take an injectable ``llm`` so they are unit-testable without a network, parse a strict JSON
verdict, and FAIL OPEN (a reviewer that errors or returns garbage must never block or corrupt the
user's answer — CLAUDE.md: perception failures never break a response). ``render_review_note`` is a
pure, deterministic renderer for the caviat appended to the answer.

A third check, ``classify_claims`` (ADR-009 grounding), backs the Cortex ``ground_check`` node:
each atomic claim of the draft answer is labelled ``supported | partial | none`` against the
evidence already gathered. It fails open in the same sense — the answer is never blocked — but
its failure is never read as a pass: an errored verdict can only hold or lower autonomy
(``autonomy_ceiling_for``), never raise it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.utils.logger import get_logger

logger = get_logger(__name__)

_GOAL_SYSTEM = (
    "You are a strict evidence sufficiency gate. Given an OBJECTIVE and the EVIDENCE gathered so "
    "far, decide whether the evidence is sufficient to answer the objective. Reply with ONLY a "
    'JSON object: {"sufficient": true|false, "missing": ["<what evidence is still needed>", ...]}.'
)

_REVIEW_SYSTEM = (
    "You are an adversarial reviewer with NO access to the investigator's reasoning — only its "
    "CLAIM and the raw EVIDENCE. Your job is to find statements in the claim that the evidence "
    "does not actually support. Be skeptical; an unsupported root cause is worse than an admitted "
    "unknown. Treat any 'not found' / 'does not exist' / 'missing' conclusion with SUSPICION: flag "
    "it as unsupported unless the evidence contains an explicit by-name lookup that returned "
    "NotFound — an empty label search does NOT prove a resource is absent. Reply with ONLY a JSON "
    'object: {"supported": true|false, "confidence": 0.0-1.0, '
    '"unsupported": ["<claim not backed by evidence>", ...]}.'
)


@dataclass(frozen=True)
class GoalVerdict:
    sufficient: bool
    missing: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RcaReview:
    supported: bool
    # None ⇒ the reviewer produced no usable number: the field was absent, unparseable, or the
    # reviewer never ran. Distinct from 0.0, which is the reviewer *stating* it has no confidence
    # in the RCA at all — the loudest verdict it can return, and precisely the value the renderer
    # used to suppress, because `if review.confidence:` is false for it.
    confidence: float | None
    unsupported: list[str] = field(default_factory=list)
    errored: bool = False  # the reviewer failed and we failed open


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from an LLM reply; None if unparseable."""
    if not isinstance(text, str):
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


async def evaluate_goal(
    objective: str, evidence: str, *, llm: BaseChatModel | None = None,
) -> GoalVerdict:
    """Stop-gate: is ``evidence`` sufficient for ``objective``? Fails open to sufficient=True
    (never trap the loop into gathering forever on a reviewer error)."""
    try:
        if llm is None:
            from app.cortex.models import get_specialist_llm
            llm = get_specialist_llm()
        reply = await llm.ainvoke([
            SystemMessage(content=_GOAL_SYSTEM),
            HumanMessage(content=f"OBJECTIVE:\n{objective}\n\nEVIDENCE:\n{evidence}"),
        ])
        obj = _parse_json_object(reply.content if isinstance(reply.content, str) else "")
        if obj is None:
            return GoalVerdict(sufficient=True)
        return GoalVerdict(sufficient=bool(obj.get("sufficient", True)),
                           missing=_clean_list(obj.get("missing")))
    except Exception as exc:
        logger.warning("verify.evaluate_goal failed open: %s", exc)
        return GoalVerdict(sufficient=True)


async def review_rca(
    claim: str, evidence: str, *, llm: BaseChatModel | None = None,
) -> RcaReview:
    """Adversarial fresh-context review of a claim against evidence. Fails OPEN to supported=True
    with ``errored=True`` so a broken reviewer never contradicts a sound answer."""
    try:
        if llm is None:
            from app.cortex.models import get_specialist_llm
            llm = get_specialist_llm()
        reply = await llm.ainvoke([
            SystemMessage(content=_REVIEW_SYSTEM),
            HumanMessage(content=f"CLAIM:\n{claim}\n\nEVIDENCE:\n{evidence}"),
        ])
        obj = _parse_json_object(reply.content if isinstance(reply.content, str) else "")
        if obj is None:
            return RcaReview(supported=True, confidence=None, errored=True)
        unsupported = _clean_list(obj.get("unsupported"))
        raw_confidence = obj.get("confidence")
        try:
            # `"confidence": "high"` and a missing key are both "no number", NOT a stated zero.
            confidence: float | None = (
                None if raw_confidence is None else max(0.0, min(1.0, float(raw_confidence)))
            )
        except (ValueError, TypeError):
            confidence = None
        # Trust the explicit list over the boolean: any unsupported item ⇒ not fully supported.
        supported = bool(obj.get("supported", True)) and not unsupported
        return RcaReview(supported=supported, confidence=confidence, unsupported=unsupported)
    except Exception as exc:
        logger.warning("verify.review_rca failed open: %s", exc)
        return RcaReview(supported=True, confidence=None, errored=True)


def render_review_note(review: RcaReview) -> str:
    """Deterministic block appended to the answer. Three states, not two — because "the reviewer
    checked this and was satisfied" and "the reviewer never ran" are different facts and used to
    render the same empty string, which the user reads as the first one.

    Empty string ONLY for a clean review. Failing open stays fail-open in the sense that matters —
    the answer is neither blocked nor contradicted — but it stops being *silent*.
    """
    if review.errored:
        return "\n".join([
            "", "---",
            "**⚠ Verification NOT PERFORMED.** The adversarial reviewer returned no usable "
            "verdict, so nothing above was checked against the gathered evidence. This is the "
            "absence of a finding, not a finding — treat this answer as unverified.",
        ])
    if review.supported and not review.unsupported:
        return ""
    lines = ["", "---", ("**⚠ Verification:** the adversarial reviewer flagged claims the gathered "
             "evidence does not fully support:")]
    lines.extend(f"- {item}" for item in review.unsupported)
    # Stated unconditionally. Rendered only `if review.confidence:`, the line vanished for exactly
    # 0.0 — the reviewer declaring no confidence in the RCA — so the caveat block was quietest at
    # its own maximum alarm, and a missing number looked identical to a confident one.
    if review.confidence is None:
        lines.append("\n_The reviewer stated no confidence value._")
    else:
        lines.append(f"\n_Reviewer confidence in the RCA: {review.confidence:.0%}._")
    return "\n".join(lines)


# ── Grounding check (ADR-009) ─────────────────────────────────────────────────
#
# The shared critique sub-stage of the Cortex graph (`ground_check`, between synthesize and
# remember). ADR-009 hosts three critiques in ONE cheap-tier structured call so they do not cost
# three: claim grounding (implemented here), self-critique, and the prompt-injection output check.
# Neither of the other two exists in this codebase yet. Their extension point is this call: add a
# key to the `_GROUND_SYSTEM` reply schema, parse it in `classify_claims`, and carry it on
# `GroundingVerdict` — do not add a second LLM call. (The adversarial RCA reviewer above,
# `review_rca`, is a separate default-off rung that stays in `synthesize`.)

GROUNDING_CLASSES = ("supported", "partial", "none")

_GROUND_SYSTEM = (
    "You are a strict grounding checker. Given a DRAFT answer and the EVIDENCE already gathered "
    "(tool output: kubectl, PromQL, LogQL; detector firings; recalled episodes), split the draft "
    "into its atomic factual claims about the cluster — diagnoses, root causes, observed states, "
    "and recommended fixes — and label each one:\n"
    '  "supported" — the evidence states it directly;\n'
    '  "partial"   — the evidence is consistent with it but does not establish it;\n'
    '  "none"      — nothing in the evidence backs it.\n'
    "Quote each claim VERBATIM from the draft (an exact substring). Do not invent evidence; text "
    "the evidence marks as truncated is not proof that a fact was absent. Reply with ONLY a JSON "
    'object: {"claims": [{"claim": "<verbatim>", "support": "supported|partial|none"}, ...]}.'
)

# A draft with none of these makes no actionable claim — a status listing, a healthy-cluster
# summary, a conceptual answer — and the check is skipped without an LLM call (ADR-009: "most
# healthy-cluster answers pay nothing"). Deliberately broad: a false positive costs one cheap
# call, a false negative lets an unchecked diagnosis through.
_ACTIONABLE_RE = re.compile(
    r"\b(?:root[ -]cause|caused by|because|due to|culprit"
    r"|the (?:cause|problem|issue|failure) (?:is|was)"
    r"|fix(?:es|ed|ing)?|remediat\w*|recommend\w*|resolv\w*|mitigat\w*"
    r"|(?:you|we) should|should be (?:increased|raised|lowered|reduced|changed|updated|set)"
    r"|(?:increase|raise|lower|reduce|bump) the"
    r"|kubectl\s+(?:apply|delete|scale|patch|rollout|set|edit|label|annotate|cordon|uncordon"
    r"|drain|taint|create|replace|autoscale)"
    r"|helm\s+(?:install|upgrade|rollback|uninstall)"
    r"|restart (?:the )?(?:pod|deployment|statefulset|daemonset)|roll(?:ing)? back)\b",
    re.IGNORECASE,
)

# A claim shorter than this is not withdrawn inline (it could match an unrelated fragment of the
# answer); it is still listed in the appended note.
_MIN_WITHDRAW_CHARS = 12
_WITHDRAWN = "[withdrawn — not supported by the evidence gathered this turn]"


@dataclass(frozen=True)
class GroundedClaim:
    claim: str
    support: str  # one of GROUNDING_CLASSES


@dataclass(frozen=True)
class GroundingVerdict:
    """Outcome of one grounding check.

    ``status``:
      ``skipped``     — the draft makes no actionable claim; no LLM call was made.
      ``grounded``    — every claim is ``supported``.
      ``partial``     — no claim is ``none``, but at least one is only ``partial``: consistent with
                        the evidence, not established by it.
      ``unsupported`` — at least one claim is ``none``.
      ``errored``     — the classifier failed or returned no usable verdict (fail-open).
    """
    status: str
    claims: list[GroundedClaim] = field(default_factory=list)

    @property
    def errored(self) -> bool:
        return self.status == "errored"

    def by_class(self, support: str) -> list[str]:
        return [c.claim for c in self.claims if c.support == support]

    def counts(self) -> dict[str, int]:
        return {k: len(self.by_class(k)) for k in GROUNDING_CLASSES}


def has_actionable_claim(draft: str) -> bool:
    """True when the draft makes a claim the grounding check must examine (deterministic)."""
    return isinstance(draft, str) and bool(_ACTIONABLE_RE.search(draft))


async def classify_claims(
    draft: str, evidence: str, *, llm: BaseChatModel | None = None, config: Any = None,
) -> GroundingVerdict:
    """Label each atomic claim of ``draft`` against ``evidence`` (no new retrieval).

    Fails OPEN to ``status="errored"`` — never to ``grounded``. An unusable reply is not a pass:
    the answer is still returned, but `autonomy_ceiling_for` will not let it raise autonomy.
    A label outside the vocabulary counts as ``none``: support the checker did not state is
    support that was not established. An empty claim list for a draft the skip rule found
    actionable is also ``errored`` — the checker examined nothing.
    """
    try:
        if llm is None:
            from app.cortex.models import get_specialist_llm
            llm = get_specialist_llm()
        reply = await llm.ainvoke([
            SystemMessage(content=_GROUND_SYSTEM),
            HumanMessage(content=f"DRAFT:\n{draft}\n\nEVIDENCE:\n{evidence}"),
        ], config)
        obj = _parse_json_object(reply.content if isinstance(reply.content, str) else "")
        raw = obj.get("claims") if obj is not None else None
        if not isinstance(raw, list):
            return GroundingVerdict(status="errored")
        claims: list[GroundedClaim] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            text = str(item.get("claim") or "").strip()
            if not text:
                continue
            support = str(item.get("support") or "").strip().lower()
            claims.append(GroundedClaim(
                claim=text, support=support if support in GROUNDING_CLASSES else "none",
            ))
        if not claims:
            return GroundingVerdict(status="errored")
        if any(c.support == "none" for c in claims):
            status = "unsupported"
        elif any(c.support == "partial" for c in claims):
            status = "partial"
        else:
            status = "grounded"
        return GroundingVerdict(status=status, claims=claims)
    except Exception as exc:
        logger.warning("verify.classify_claims failed open: %s", exc)
        return GroundingVerdict(status="errored")


def autonomy_ceiling_for(verdict: GroundingVerdict) -> str | None:
    """The highest autonomy level this turn's diagnosis may drive (ADR-009 x ADR-003).

    ``None`` — no ceiling from grounding: every claim was supported. ``A1`` — an unsupported claim
    demotes the turn to advisory: it cannot auto-trigger A2/A3. ``A2`` — a claim is only partly
    supported, or the classifier errored: the turn may propose a fix for a human to approve but
    can never apply one on its own, and is never raised on a diagnosis that was not established.
    An action that cannot be undone should rest on what the evidence establishes, not on what it
    is merely consistent with. A ceiling only ever lowers the level the ladder resolved; it
    composes with, never replaces, the ladder, the A3 allowlist and the blast-radius gate.
    """
    if verdict.status == "unsupported":
        return "A1"
    if verdict.status in ("partial", "errored"):
        return "A2"
    return None


def grounding_permits_autofix(record: dict | None) -> bool:
    """May a diagnosis with this recorded grounding outcome trigger an autonomous fix?

    Only a check that RAN and found every claim ``supported``. ``skipped`` (no actionable claim —
    so nothing to fix), ``errored``, ``partial``, ``unsupported`` and a missing record (the check
    did not run, or the turn paused at the approval gate before reaching it) all answer False.
    """
    return (isinstance(record, dict) and record.get("status") == "grounded"
            and record.get("autonomy_ceiling") is None)


def withdraw_unsupported(answer: str, verdict: GroundingVerdict) -> str:
    """Drop each ``none`` claim from the stored answer where it appears verbatim.

    The streamed answer has already reached the client token by token, so there the claim is
    hedged by the appended note instead; this rewrites the copy that persists — the message
    history the next turn reads and the episode `remember` writes — so the unsupported claim is
    not recalled later as an observed fact.
    """
    out = answer
    for claim in verdict.by_class("none"):
        if len(claim) >= _MIN_WITHDRAW_CHARS and claim in out:
            out = out.replace(claim, _WITHDRAWN, 1)
    return out


def render_grounding_note(verdict: GroundingVerdict) -> str:
    """Deterministic block appended to the answer. Empty for ``skipped`` and for a check in which
    every claim was ``supported``; never empty for ``errored`` (an unchecked answer must not look
    like a checked one — the same rule as `render_review_note`)."""
    if verdict.status == "errored":
        return "\n".join([
            "", "---",
            "**⚠ Grounding check NOT PERFORMED.** The claim checker returned no usable verdict, "
            "so the claims above were not checked against the gathered evidence. This turn's "
            "autonomy is not raised on an unchecked diagnosis.",
        ])
    unsupported = verdict.by_class("none")
    partial = verdict.by_class("partial")
    if not unsupported and not partial:
        return ""
    lines = ["", "---"]
    if unsupported:
        lines.append("**⚠ Grounding check:** these claims are not supported by the evidence "
                     "gathered this turn and are withdrawn — do not act on them:")
        lines.extend(f"- {c}" for c in unsupported)
    if partial:
        lines.append("**Partly supported** — consistent with the evidence but not established "
                     "by it; treat as hypotheses:")
        lines.extend(f"- {c}" for c in partial)
    if unsupported:
        lines.append("\n_Autonomy for this turn is demoted to advisory: this diagnosis cannot "
                     "trigger an autonomous fix._")
    elif partial:
        lines.append("\n_Autonomy for this turn is capped at propose: a partly supported diagnosis "
                     "can be proposed for a human to approve but cannot trigger an autonomous "
                     "fix._")
    return "\n".join(lines)


def grounding_record(verdict: GroundingVerdict) -> dict[str, Any]:
    """The per-turn outcome kept in state and the flight recorder (counts, not claim text)."""
    return {
        "status": verdict.status,
        "counts": verdict.counts(),
        "autonomy_ceiling": autonomy_ceiling_for(verdict),
    }
