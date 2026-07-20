"""Misconfig auto-repair (v5 P3 first write class, A-CH-04-02).

Given a manifest and a policy violation, propose the minimal corrected manifest. This is the LLM
step the fix-PR generator (`fix_pr.py`) packages into a PR — the actual repair, kept separate so
the PR mechanics stay pure/deterministic and this stays the one token-spending piece.

Fails SAFE: on any error or an empty/echoed response, returns the ORIGINAL manifest unchanged, so
the downstream fix-PR is a no-op (nothing is proposed) rather than a corrupted manifest.
"""

from __future__ import annotations

from typing import Optional

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


async def propose_fix(
    manifest: str, violation: str, *, llm: Optional[BaseChatModel] = None,
) -> str:
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
        if not fixed or "kind:" not in fixed:
            return manifest
        return fixed
    except Exception as exc:
        logger.warning("repair.propose_fix failed safe (no change): %s", exc)
        return manifest
