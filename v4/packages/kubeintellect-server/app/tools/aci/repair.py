"""Misconfig auto-repair (v5 P3 first write class, A-CH-04-02).

Given a manifest and a policy violation, propose the minimal corrected manifest. This is the LLM
step the fix-PR generator (`fix_pr.py`) packages into a PR — the actual repair, kept separate so
the PR mechanics stay pure/deterministic and this stays the one token-spending piece.

Fails SAFE: on any error or an empty/echoed response, returns the ORIGINAL manifest unchanged, so
the downstream fix-PR is a no-op (nothing is proposed) rather than a corrupted manifest.

Failing safe is not the same as **reporting** safely. Returning the original was the whole answer,
so a repair that never ran was byte-identical to a repair that found nothing to change — and
measured 2026-08-24 an LLM exception, an empty response and a refusal in prose all reached
`open_pr` as *"no change to propose"*, the sentence a compliant manifest earns. On a security
misconfig path that reads as "nothing to do" when the violation is untouched. Hence
`RepairProposal.repaired`.
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.utils.logger import get_logger

logger = get_logger(__name__)

_REPAIR_SYSTEM = (
    "You are a Kubernetes security auto-repair. Given a MANIFEST and a policy VIOLATION, return "
    "ONLY the corrected manifest as valid YAML — change the MINIMUM needed to resolve the "
    "violation, preserve everything else exactly. No prose, no explanations, no ``` fences."
)


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines)
    return t.strip()


@dataclass(frozen=True)
class RepairProposal:
    """What the repair step produced, and whether it produced anything at all."""

    manifest: str
    # False ⇒ nothing was proposed and the violation still stands. The manifest above is then the
    # ORIGINAL, and an empty diff downstream is a statement about *this step*, not about the
    # manifest's compliance.
    repaired: bool
    reason: str = ""


async def propose_fix(
    manifest: str, violation: str, *, llm: BaseChatModel | None = None,
) -> RepairProposal:
    """Propose a corrected manifest for ``violation``. Returns the ORIGINAL on any failure."""
    try:
        if llm is None:
            from app.cortex.models import get_synthesis_llm
            llm = get_synthesis_llm()   # the larger tier — repair must be reliable
        reply = await llm.ainvoke([
            SystemMessage(content=_REPAIR_SYSTEM),
            HumanMessage(content=f"MANIFEST:\n{manifest}\n\nVIOLATION:\n{violation}"),
        ])
        fixed = _strip_fences(reply.content if isinstance(reply.content, str) else "")
        # Guard: never return empty or something that lost the manifest's kind.
        if not fixed:
            return RepairProposal(manifest, False, "the model returned an empty response")
        if "kind:" not in fixed:
            return RepairProposal(
                manifest, False,
                "the model's reply was not a manifest (no `kind:` line) — it was most likely prose",
            )
        return RepairProposal(fixed, True)
    except Exception as exc:
        logger.warning("repair.propose_fix failed safe (no change): %s", exc)
        return RepairProposal(manifest, False, f"the repair call raised: {exc}")
