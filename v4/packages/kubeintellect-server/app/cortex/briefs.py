"""Escalation-avoidance briefs (v5 P2, A-CH-17-07).

Only ~20% of incidents resolve without escalation, ~5 engineers each (C4 / CH-17): the cost is
reflexive escalation, not hard incidents. The brief flips the default — a responder-skill-calibrated
plan with **explicit escalate-only-if boundaries**, so a responder escalates on a stated condition
rather than on uncertainty. The roadmap notes no competitor ships this.

``build_brief`` takes an injectable ``llm`` (unit-testable, no network), parses a strict JSON
brief, and FAILS SAFE: if the model errors or returns garbage, it emits a conservative brief that
tells the responder to escalate if unsure — never a false "you're safe to proceed alone" signal.
``render_brief`` is a pure, deterministic markdown renderer.
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

RESPONDER_LEVELS = ("junior", "intermediate", "senior")

_BRIEF_SYSTEM = (
    "You write escalation-avoidance briefs for an on-call responder at skill level '{level}'. "
    "Given an investigation's ROOT CAUSE and EVIDENCE, produce a brief that lets a {level} "
    "responder act WITHOUT escalating unless a stated boundary is crossed. Reply with ONLY a JSON "
    'object: {{"summary": "<1-2 sentence situation>", "actions": ["<safe step the responder can '
    'take>", ...], "escalate_if": ["<explicit condition under which to page a senior/SME>", ...], '
    '"confidence": 0.0-1.0}}. The escalate_if list MUST be concrete and checkable (a symptom, a '
    "threshold, a failed step) — never vague. Prefer 2-4 actions and 2-4 escalate_if conditions."
)

# The always-safe fallback: uncertainty defaults to escalation, never to solo action.
_FALLBACK_ESCALATE_IF = [
    "the recommended actions do not resolve the symptom within one attempt",
    "any action would touch a protected namespace or a stateful/irreversible resource",
    "you are unsure the root cause is correct",
]


@dataclass(frozen=True)
class EscalationBrief:
    summary: str
    actions: list[str] = field(default_factory=list)
    escalate_if: list[str] = field(default_factory=list)
    responder_level: str = "intermediate"
    confidence: float = 0.0
    fell_back: bool = False


def _parse_json_object(text: str) -> dict[str, Any] | None:
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


def _normalize_level(level: str) -> str:
    return level if level in RESPONDER_LEVELS else "intermediate"


def _fallback(level: str) -> EscalationBrief:
    return EscalationBrief(
        summary="Automated brief unavailable — proceed conservatively.",
        actions=["Review the investigation evidence above before taking any action."],
        escalate_if=list(_FALLBACK_ESCALATE_IF),
        responder_level=level,
        confidence=0.0,
        fell_back=True,
    )


async def build_brief(
    root_cause: str, evidence: str, *, responder_level: str = "intermediate",
    llm: BaseChatModel | None = None,
) -> EscalationBrief:
    """Build a skill-calibrated escalation-avoidance brief. Fails safe to a conservative brief."""
    level = _normalize_level(responder_level)
    try:
        if llm is None:
            from app.cortex.models import get_specialist_llm
            llm = get_specialist_llm()
        reply = await llm.ainvoke([
            SystemMessage(content=_BRIEF_SYSTEM.format(level=level)),
            HumanMessage(content=f"ROOT CAUSE:\n{root_cause}\n\nEVIDENCE:\n{evidence}"),
        ])
        obj = _parse_json_object(reply.content if isinstance(reply.content, str) else "")
        if obj is None:
            return _fallback(level)
        actions = _clean_list(obj.get("actions"))
        escalate_if = _clean_list(obj.get("escalate_if"))
        summary = str(obj.get("summary", "")).strip()
        if not summary or not actions or not escalate_if:
            # A brief with no actions or no escalation boundary is unsafe — fall back.
            return _fallback(level)
        try:
            confidence = max(0.0, min(1.0, float(obj.get("confidence", 0.0))))
        except (ValueError, TypeError):
            confidence = 0.0
        # Always keep the safety net conditions in addition to any model-specific ones.
        for cond in _FALLBACK_ESCALATE_IF:
            if cond not in escalate_if:
                escalate_if.append(cond)
        return EscalationBrief(summary=summary, actions=actions, escalate_if=escalate_if,
                               responder_level=level, confidence=confidence)
    except Exception as exc:
        logger.warning("briefs.build_brief failed safe: %s", exc)
        return _fallback(level)


def render_brief(brief: EscalationBrief) -> str:
    """Deterministic markdown for the brief, appended to an investigation answer."""
    lines = ["", "---", f"### Responder brief ({brief.responder_level})", "", brief.summary, "",
             "**Do:**"]
    lines.extend(f"1. {a}" if i == 0 else f"{i + 1}. {a}" for i, a in enumerate(brief.actions))
    lines.append("")
    lines.append("**Escalate only if:**")
    lines.extend(f"- {c}" for c in brief.escalate_if)
    if brief.confidence:
        lines.append(f"\n_RCA confidence: {brief.confidence:.0%}._")
    return "\n".join(lines)
