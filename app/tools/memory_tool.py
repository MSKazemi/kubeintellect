"""read_memory / write_memory — on-demand long-term memory for the agent.

Replaces v2's always-on `memory_loader` node. The agent decides when prior
context matters and calls these tools explicitly. Net effect: smaller prompts
on simple turns, real recall on the turns where it matters.

Both wrap `app.db.memory_store`. In SQLite mode (no PostgreSQL configured)
we no-op gracefully — read returns a notice, write logs and returns.
"""

from __future__ import annotations

from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from app.core.config import settings
from app.db.memory_store import load_memory_context, record_rca_outcome
from app.utils.logger import get_logger

logger = get_logger(__name__)

_TOPIC_INDEX = (
    "Available memory topics:\n"
    "  - user_prefs       — saved preferences for this user\n"
    "  - failure_hints    — auto-seeded patterns from past RCAs\n"
    "  - past_rca         — last few root-cause outcomes for this user\n"
    "  - session_notes    — notes from the current session\n"
    'Call read_memory(topic="<topic>") to drill in.'
)


def _ctx(config: RunnableConfig | None) -> tuple[str, str]:
    """Pull (user_id, session_id) out of the injected RunnableConfig."""
    cfg = (config or {}).get("configurable") or {}
    return cfg.get("user_id", "default"), cfg.get("session_id", "-")


@tool
async def read_memory(
    topic: str | None = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Read long-term memory for the current user/session.

    With no `topic`, returns the index of available topics. With a topic,
    returns the corresponding context block (preferences, failure hints,
    past RCA summaries, session notes).

    Use this to recall: "what does this user prefer?", "have we seen this
    failure pattern before?", "what was the conclusion of the last RCA on
    app X?".
    """
    if settings.USE_SQLITE:
        return "Memory store unavailable in SQLite mode (PostgreSQL required)."

    user_id, session_id = _ctx(config)

    if topic is None:
        return _TOPIC_INDEX

    full = await load_memory_context(user_id=user_id, session_id=session_id)
    if not full:
        return f"(no memory found for topic={topic!r})"

    needle = topic.lower()
    matched = [block for block in full.split("\n\n") if needle in block.lower()]
    if not matched:
        return f"(no memory matched topic={topic!r}; full context follows)\n\n{full}"
    return "\n\n".join(matched)


@tool
async def write_memory(
    topic: str,
    note: str,
    confidence: float = 0.7,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Persist a non-obvious finding for future sessions.

    Use this when you've concluded an investigation with a load-bearing
    insight that you'd want to recall next time the user asks about the
    same area — e.g. "app-x crashloops are usually OOM, container limit
    needs raising", "namespace foo's ingress is misconfigured".

    Args:
        topic: Short identifier (e.g. "crashloop_app_x", "ingress_ns_foo").
        note: One- to three-sentence summary worth remembering.
        confidence: 0.0–1.0; ≥0.9 promotes to a failure_pattern entry.
    """
    if settings.USE_SQLITE:
        logger.info(f"write_memory: skipped (SQLite mode) topic={topic!r}")
        return "Memory write skipped — SQLite mode (PostgreSQL required)."

    user_id, session_id = _ctx(config)
    await record_rca_outcome(
        session_id=session_id,
        user_id=user_id,
        root_cause=f"[{topic}] {note}",
        confidence=max(0.0, min(1.0, confidence)),
        recommended_fix=note,
    )
    return f"memory saved: topic={topic!r} confidence={confidence}"
