"""Shared fault-isolation boundary for ReAct tool batches."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.errors import GraphInterrupt
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from app.utils.logger import get_logger
from app.utils.redact import redact_secrets

logger = get_logger(__name__)


async def _isolate_tool_failure(
    request: ToolCallRequest,
    execute: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
) -> ToolMessage | Command:
    """Convert one ordinary failure to a result without weakening HITL."""
    try:
        return await execute(request)
    except GraphInterrupt:
        raise
    except Exception as exc:
        call = request.tool_call
        name = call.get("name", "")
        command = (call.get("args") or {}).get("command")
        safe_command = redact_secrets(command, max_chars=500) if isinstance(command, str) else ""
        invocation = f" command={safe_command!r}" if safe_command else ""
        reason = redact_secrets(str(exc), max_chars=500)
        logger.warning(
            f"tool call failed independently: {name}{invocation}: {reason}",
            extra={"tool_name": name, "tool_call_id": call.get("id", "")},
        )
        return ToolMessage(
            tool_call_id=call.get("id", ""),
            name=name,
            status="error",
            content=f"Tool error: {name}{invocation}: {reason}",
        )


def fault_isolated_tool_node(tools: Sequence[BaseTool]) -> ToolNode:
    """Build a parallel ToolNode where each ordinary call fails independently."""
    return ToolNode(tools, awrap_tool_call=_isolate_tool_failure)
