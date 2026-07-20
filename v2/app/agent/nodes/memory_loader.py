"""memory_loader node — async DB reads pinned into coordinator SystemMessage."""
from __future__ import annotations

from langchain_core.messages import SystemMessage

from app.agent.state import AgentState
from app.db.memory_store import load_memory_context
from app.streaming.emitter import StatusEvent, emit
from app.utils.logger import get_logger

logger = get_logger(__name__)


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

    context = await load_memory_context(user_id=user_id, session_id=session_id)

    # findings=None triggers _findings_reducer's reset path, clearing any
    # stale findings that accumulated during a previous RCA in this session.
    return {"memory_context": context, "findings": None}
