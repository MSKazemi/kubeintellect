# app/services/memory_orchestrator.py
"""
MemoryOrchestrator — single entry point for all pre-request memory loading.

Replaces the three separate injection blocks that previously existed in
run_kubeintellect_workflow():
  1. Reflection memory load (per-user routing lessons)
  2. Failure pattern match (pre-query diagnostic hints)
  3. User preference load (verbosity, format, default namespace, etc.)

All three are fetched in parallel via asyncio.gather() to add zero serial
latency vs. the previous sequential approach.

The results are rendered into a single pinned SystemMessage (≤ 400 tokens)
with clearly labelled sections.  Only non-empty, non-default sections are
included — if a user has no preferences and no pattern matches, the message
is omitted entirely to avoid noise.

Also fires detect_and_save() as a background asyncio.Task (not awaited) so
preference heuristics accumulate without adding any latency.

Usage in workflow.py::run_kubeintellect_workflow():

    from app.services.memory_orchestrator import MemoryOrchestrator
    memory_ctx = await MemoryOrchestrator.build_context(
        user_id=user_id,
        query=_user_query,
        conversation_history=messages,
        current_namespace=...,
        current_cluster=...,
        pool=_langgraph_pool,
    )
    if memory_ctx.pinned_message:
        messages = [memory_ctx.pinned_message] + list(messages)
    initial_state = {
        ...
        "reflection_memory": [],   # rendered into pinned_message; suppress routing.py re-injection
    }
    # Fire-and-forget update_seen for the matched failure pattern
    if memory_ctx.matched_pattern_id:
        asyncio.create_task(
            FailurePatternService.update_seen(memory_ctx.matched_pattern_id, pool)
        )
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import tiktoken

from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")

_enc = tiktoken.encoding_for_model("gpt-4o")


def _tokens(text: str) -> int:
    return len(_enc.encode(text))


# Total token budget for the combined pinned SystemMessage
_PINNED_MSG_BUDGET = 550
# Max tokens allocated to the registered tools section
_TOOLS_SECTION_BUDGET = 150
# Max tokens allocated to the failure-pattern section
_FP_SECTION_BUDGET = 200
# Max tokens allocated to the reflections section
_REFLECTIONS_BUDGET = 120
# Max tokens allocated to user-preferences section
_PREFS_BUDGET = 80


# ---------------------------------------------------------------------------
# MemoryContext — returned by build_context()
# ---------------------------------------------------------------------------

@dataclass
class MemoryContext:
    """Aggregated pre-request memory for one workflow invocation."""

    reflection_lessons: List[str] = field(default_factory=list)
    user_prefs: Dict[str, Optional[str]] = field(default_factory=dict)
    matched_pattern_id: Optional[str] = None   # for update_seen() after state is built
    pinned_message: Optional[object] = None    # LangChain SystemMessage, or None if nothing to inject


# ---------------------------------------------------------------------------
# MemoryOrchestrator
# ---------------------------------------------------------------------------

class MemoryOrchestrator:
    """
    Parallel memory loader and renderer.

    Single public method: build_context().
    """

    @classmethod
    async def build_context(
        cls,
        user_id: Optional[str],
        query: Optional[str],
        conversation_history: list,
        current_namespace: Optional[str],
        current_cluster: Optional[str],
        pool,
    ) -> MemoryContext:
        """
        Run all memory lookups in parallel and return a MemoryContext.

        Never raises — all errors inside the parallel tasks are caught.
        """
        ctx = MemoryContext()
        if not pool:
            return ctx

        # ── Run all lookups in parallel ────────────────────────────────────
        results = await asyncio.gather(
            cls._load_reflections(user_id, pool),
            cls._load_failure_pattern(query, pool),
            cls._load_user_prefs(user_id, pool),
            cls._load_registered_tools(pool),
            return_exceptions=True,
        )

        reflections, fp_result, prefs, registered_tools = results

        if isinstance(reflections, Exception):
            logger.debug("MemoryOrchestrator: reflection load error: %s", reflections)
            reflections = []
        if isinstance(fp_result, Exception):
            logger.debug("MemoryOrchestrator: failure pattern error: %s", fp_result)
            fp_result = None
        if isinstance(prefs, Exception):
            logger.debug("MemoryOrchestrator: user prefs error: %s", prefs)
            prefs = {}
        if isinstance(registered_tools, Exception):
            logger.debug("MemoryOrchestrator: registered tools load error: %s", registered_tools)
            registered_tools = []

        ctx.reflection_lessons = reflections or []
        ctx.user_prefs = prefs or {}
        if fp_result:
            ctx.matched_pattern_id = fp_result.pattern_id

        # ── Fire-and-forget: detect & save new preferences ─────────────────
        if user_id:
            try:
                from app.services.user_preference_service import UserPreferenceService
                asyncio.create_task(
                    UserPreferenceService.detect_and_save(
                        user_id, conversation_history, current_namespace, current_cluster, pool
                    )
                )
            except Exception as exc:
                logger.debug("MemoryOrchestrator: detect_and_save task error: %s", exc)

        # ── Render pinned SystemMessage ────────────────────────────────────
        pinned_text = cls._render(ctx.reflection_lessons, fp_result, ctx.user_prefs, registered_tools or [])
        if pinned_text:
            from langchain_core.messages import SystemMessage
            ctx.pinned_message = SystemMessage(content=pinned_text)
            logger.debug(
                "MemoryOrchestrator: pinned_message=%d tokens reflections=%d fp=%s prefs_keys=%d tools=%d",
                _tokens(pinned_text),
                len(ctx.reflection_lessons),
                fp_result.pattern_id if fp_result else None,
                sum(1 for v in ctx.user_prefs.values() if v and v != "default"),
                len(registered_tools or []),
            )

        return ctx

    # ── Private loaders ─────────────────────────────────────────────────────

    @staticmethod
    async def _load_reflections(user_id: Optional[str], pool) -> List[str]:
        if not user_id:
            return []
        from app.services.reflection_memory_service import load_reflection_memories
        return await load_reflection_memories(user_id, pool, limit=3)

    @staticmethod
    async def _load_failure_pattern(query: Optional[str], pool):
        """Returns the top FailurePattern match or None."""
        if not query:
            return None
        from app.services.failure_pattern_service import FailurePatternService
        matches = await FailurePatternService.match(query, pool, top_k=1)
        return matches[0] if matches else None

    @staticmethod
    async def _load_user_prefs(user_id: Optional[str], pool) -> Dict[str, Optional[str]]:
        if not user_id:
            return {}
        from app.services.user_preference_service import UserPreferenceService
        return await UserPreferenceService.load(user_id, pool)

    @staticmethod
    async def _load_registered_tools(pool) -> List[Dict]:
        """Load enabled tools from tool_registry so the supervisor can route to them directly."""
        try:
            async with pool.connection() as conn:
                rows = await conn.execute(
                    "SELECT name, description FROM tool_registry WHERE status = 'enabled' ORDER BY created_at DESC LIMIT 10"
                )
                return [{"name": row[0], "description": row[1]} for row in await rows.fetchall()]
        except Exception:
            return []

    # ── Renderer ────────────────────────────────────────────────────────────

    @staticmethod
    def _render(
        reflections: List[str],
        fp,                          # Optional[FailurePattern]
        prefs: Dict[str, Optional[str]],
        registered_tools: List[Dict] = [],
    ) -> str:
        """
        Build the pinned SystemMessage text from the three memory sources.

        Returns an empty string if all sections are empty (no injection).
        Total length is kept under _PINNED_MSG_BUDGET tokens by trimming
        each section independently before assembly.
        """
        from app.services.user_preference_service import DEFAULT_VALUE

        sections: list[str] = []

        # ── Section 0: Registered custom tools (highest routing priority) ─
        if registered_tools:
            lines = [
                f"  - {t['name']}: {t['description'][:80]}"
                for t in registered_tools
            ]
            tools_text = (
                "[Registered custom tools — route to DynamicToolsExecutor if request matches, "
                "do NOT route to CodeGenerator:]\n" + "\n".join(lines)
            )
            if _tokens(tools_text) <= _TOOLS_SECTION_BUDGET:
                sections.append(tools_text)
            else:
                # Trim to fit budget
                trimmed: list[str] = []
                used = _tokens("[Registered custom tools — route to DynamicToolsExecutor if request matches, do NOT route to CodeGenerator:]\n")
                for line in lines:
                    if used + _tokens(line + "\n") > _TOOLS_SECTION_BUDGET:
                        break
                    trimmed.append(line)
                    used += _tokens(line + "\n")
                if trimmed:
                    sections.append(
                        "[Registered custom tools — route to DynamicToolsExecutor if request matches, "
                        "do NOT route to CodeGenerator:]\n" + "\n".join(trimmed)
                    )

        # ── Section 1: User preferences ──────────────────────────────────
        non_default = {
            k: v for k, v in prefs.items()
            if v is not None and v != DEFAULT_VALUE
        }
        if non_default:
            pairs = ", ".join(f"{k}={v}" for k, v in non_default.items())
            pref_line = f"[User preferences: {pairs}]"
            if _tokens(pref_line) <= _PREFS_BUDGET:
                sections.append(pref_line)

        # ── Section 2: Failure pattern ────────────────────────────────────
        if fp is not None:
            checks = "\n".join(
                f"  {i+1}. {c}"
                for i, c in enumerate(fp.recommended_checks[:3])
            )
            remediation = "\n".join(
                f"  {i+1}. {s}"
                for i, s in enumerate(fp.remediation_steps[:2])
            )
            fp_text = (
                f"[Pattern match: {fp.type} — confidence {fp.confidence:.2f}]\n"
                f"Recommended checks:\n{checks}\n"
                f"Remediation:\n{remediation}"
            )
            # Trim to budget if needed
            if _tokens(fp_text) > _FP_SECTION_BUDGET:
                # Fall back to checks-only abbreviated form
                fp_text = (
                    f"[Pattern match: {fp.type} — confidence {fp.confidence:.2f}]\n"
                    + "\n".join(f"  • {c}" for c in fp.recommended_checks[:2])
                )
            sections.append(fp_text)

        # ── Section 3: Reflection lessons ─────────────────────────────────
        if reflections:
            lessons = reflections[-3:]   # most recent 3
            refl_text = (
                "⚠️ Past routing lessons:\n"
                + "\n".join(f"- {r}" for r in lessons)
            )
            # Trim individual lessons if the block is too big
            if _tokens(refl_text) > _REFLECTIONS_BUDGET:
                truncated = []
                used = _tokens("⚠️ Past routing lessons:\n")
                for lesson in lessons:
                    line = f"- {lesson}\n"
                    if used + _tokens(line) > _REFLECTIONS_BUDGET:
                        break
                    truncated.append(f"- {lesson}")
                    used += _tokens(line)
                refl_text = "⚠️ Past routing lessons:\n" + "\n".join(truncated)
            sections.append(refl_text)

        if not sections:
            return ""

        combined = "\n\n".join(sections)

        # Final hard cap: trim trailing sections if total exceeds budget
        while _tokens(combined) > _PINNED_MSG_BUDGET and len(sections) > 1:
            sections.pop()   # drop least-priority last section
            combined = "\n\n".join(sections)

        return combined


# Module-level alias used by tests (avoids reaching into private methods)
_render_as_text = MemoryOrchestrator._render
