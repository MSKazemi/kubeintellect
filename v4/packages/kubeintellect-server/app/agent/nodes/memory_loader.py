"""memory_loader node — async DB reads pinned into coordinator SystemMessage."""
from __future__ import annotations

import time

from app.agent.state import AgentState
from app.db.memory_store import MemoryStoreUnavailable, load_memory_context
from app.streaming.emitter import StatusEvent, emit
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def _hierarchy_context(state: AgentState) -> str:
    """V4 hippocampus injection: similar episodes (L1) + recent KG changes (L2).

    Budget-bounded and latency-tracked (exit gate: < 200 ms p95). Returns ''
    when the hierarchy is inactive or has nothing relevant.
    """
    from app.core.config import settings
    from app.memory import episodes, kg
    from app.memory.service import memory_active

    if not memory_active():
        return ""
    started = time.perf_counter()
    try:
        from app.cluster_id import get_cluster_id
        cluster_id = get_cluster_id()
    except Exception:
        cluster_id = "unknown"

    query_text = ""
    for message in reversed(state.get("messages", [])):
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            query_text = content
            break

    parts = []
    memory_degraded = False
    try:
        recalled = await episodes.recall_episodes(query_text, cluster_id, k=3)
    except episodes.MemoryUnavailable as exc:
        # Do NOT fail the turn: an investigation with no memory is still worth far more than no
        # investigation. But do not let the model read the absence as evidence either — silently
        # omitting the block is what made a Postgres outage look like "this cluster has no history".
        logger.warning(f"memory_recall_unavailable cluster={cluster_id}: {exc}")
        recalled = []
        memory_degraded = True
        parts.append(
            "## Memory unavailable\n"
            "Recall of past episodes for this cluster FAILED — this is not the same as there being "
            "none. Do not state or assume that this issue has no precedent; say that prior history "
            "could not be checked."
        )
    block = episodes.render_recall_block(recalled)
    if block:
        parts.append(block)

    # Memory V5 P8 (spec R7): zoom out — inject matching theme summaries so a query gets
    # cross-incident context ("this whole theme keeps failing"), not just the top-k episodes.
    themes = []
    if settings.MEMORY_SUMMARY_TREE:
        from app.memory import summaries
        themes = await summaries.recall_theme_summaries(query_text, cluster_id, k=2)
        theme_block = summaries.render_summaries_block(themes)
        if theme_block:
            parts.append(theme_block)

    changes = await kg.recent_changes_block(cluster_id, minutes=15, limit=12)
    if changes:
        parts.append("## Recent cluster changes (last 15m)\n" + changes)

    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        f"memory_hierarchy_injected ms={elapsed_ms:.1f} "
        f"episodes={len(recalled)} themes={len(themes)} "
        f"degraded={memory_degraded}"
    )
    return "\n\n".join(parts)


async def memory_loader(state: AgentState) -> dict:
    """Load user prefs, failure hints, past RCA, runbooks into memory_context."""
    session_id = state["session_id"]
    user_id = state["user_id"]

    await emit(session_id, StatusEvent(
        phase="loading",
        message="Loading conversation context…",
        session_id=session_id,
    ))

    logger.debug(f"memory_loader: loading context for user={user_id} session={session_id}")

    try:
        context = await load_memory_context(user_id=user_id, session_id=session_id)
    except MemoryStoreUnavailable as exc:
        # Same contract as the hierarchy block: degrade the turn, never fail it — but never let the
        # model read the empty context as "this user has no preferences and no prior incidents".
        logger.warning(f"memory_store_unavailable session={session_id}: {exc}")
        context = (
            "## Memory unavailable\n"
            "Stored operator preferences, failure hints and past RCA could NOT be loaded — this is "
            "not the same as there being none. Do not assume the user has no preferences or that "
            "this issue has no precedent; say that stored history could not be checked."
        )
    hierarchy = await _hierarchy_context(state)
    if hierarchy:
        context = f"{context}\n\n{hierarchy}" if context else hierarchy

    # findings=None triggers _findings_reducer's reset path, clearing any
    # stale findings that accumulated during a previous RCA in this session.
    return {"memory_context": context, "findings": None}
