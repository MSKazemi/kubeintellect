"""DeepAgents-based KubeIntellect main agent (v3).

A single compiled deep agent wired with the full tool set from
:data:`app.tools.registry.ALL_TOOLS` (kubectl, Prometheus, Loki, snapshot,
memory read/write, and playbook lookup) plus the M2 subagents.

Provides init_graph / close_graph / get_graph as a singleton lifecycle
that `app/main.py` boots at startup and tears down at shutdown.
"""

from __future__ import annotations

import asyncio

from deepagents import create_deep_agent

from app.agent.prompts import MAIN_INSTRUCTIONS
from app.agent.state import KubeIntellectContext
from app.agent.subagents import build_subagents
from app.core.config import settings
from app.core.llm import get_coordinator_llm
from app.tools.registry import ALL_TOOLS
from app.utils.logger import get_logger

logger = get_logger(__name__)


def build_agent(checkpointer):
    """Compile the DeepAgents graph with the given checkpointer."""
    return create_deep_agent(
        model=get_coordinator_llm(),
        tools=ALL_TOOLS,
        system_prompt=MAIN_INSTRUCTIONS,
        context_schema=KubeIntellectContext,
        subagents=build_subagents(),
        checkpointer=checkpointer,
    )


# ── Compiled-graph singleton ─────────────────────────────────────────────────

_graph = None
_checkpointer_cm = None  # context manager (holds the connection)
_checkpointer = None  # AsyncPostgresSaver / AsyncSqliteSaver instance
_graph_lock = asyncio.Lock()


async def init_graph() -> None:
    """Build and compile the agent. Call once at app startup."""
    global _graph, _checkpointer_cm, _checkpointer
    async with _graph_lock:
        if _graph is not None:
            return
        if settings.USE_SQLITE:
            from pathlib import Path
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            db_path = str(Path(settings.SQLITE_PATH).expanduser())
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            logger.info(
                f"Building DeepAgents workflow with AsyncSqliteSaver ({db_path})"
            )
            _checkpointer_cm = AsyncSqliteSaver.from_conn_string(db_path)
        else:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            logger.info("Building DeepAgents workflow with AsyncPostgresSaver")
            _checkpointer_cm = AsyncPostgresSaver.from_conn_string(
                settings.POSTGRES_DSN
            )
        _checkpointer = await _checkpointer_cm.__aenter__()
        await _checkpointer.setup()
        _graph = build_agent(_checkpointer)
        logger.info("DeepAgents workflow ready")


async def close_graph() -> None:
    """Cleanly close the checkpointer connection. Call at app shutdown."""
    global _graph, _checkpointer_cm, _checkpointer
    if _checkpointer_cm is not None:
        await _checkpointer_cm.__aexit__(None, None, None)
        _checkpointer_cm = None
        _checkpointer = None
        _graph = None


async def get_graph():
    """Return the compiled agent (init_graph runs lazily on first call)."""
    if _graph is None:
        await init_graph()
    return _graph
