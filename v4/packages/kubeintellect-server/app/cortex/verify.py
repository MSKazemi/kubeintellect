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
