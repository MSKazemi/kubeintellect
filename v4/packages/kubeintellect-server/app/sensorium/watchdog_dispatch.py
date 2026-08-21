"""Watchdog → fan-out dispatch (v5 P4 closed loop).

Closes the anticipation loop: a change-armed WatchdogTask actually runs a bounded read-only
investigation via the ADR-101 harness fan-out, instead of only being logged. This is the real
dispatch that `change_watchdog.set_dispatch` installs — turning "a change happened" into "an agent
looked at what the change did" with a TTL-bounded, read-only investigation.

Fire-and-forget by design (a watchdog must never block the sensorium); the investigation runner is
injectable so the wiring is unit-testable without an LLM.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import HumanMessage

from app.agent.state import PlanStep
from app.sensorium.change_watchdog import WatchdogTask, set_dispatch
from app.utils.logger import get_logger

logger = get_logger(__name__)

# runner(state, config) -> dict — defaults to the harness fan-out. `...` args: run_fanout's typed
# signature (CortexState, RunnableConfig) is compatible at the call site.
Runner = Callable[..., Awaitable[Any]]


def _watchdog_state(task: WatchdogTask) -> dict:
    """A minimal investigate-mode CortexState for a watchdog's read-only investigation."""
    return {
        "messages": [HumanMessage(content=task.objective)],
        "session_id": f"watchdog-{task.dedup_key}",
        "cluster_id": "",
        "memory_context": "",
        "cluster_snapshot": "",
        "matched_playbooks": [],
        "investigation_plan": [PlanStep(description=task.objective, status="in_progress")],
        "plan_cursor": 0,
        "gather_rounds": 0,
        "turn_start_index": 1,
        "triage_mode": "investigate",
    }


async def investigate(task: WatchdogTask, *, runner: Runner | None = None) -> Any:
    """Run one watchdog's bounded read-only investigation. Never raises out."""
    active: Runner
    if runner is None:
        from app.cortex.harness.runner import run_fanout
        active = run_fanout
    else:
        active = runner
    try:
        return await active(_watchdog_state(task), {})  # type: ignore[arg-type]  # partial CortexState
    except Exception as exc:
        logger.warning("watchdog investigation failed for %s: %s", task.target, exc)
        return None


def _schedule(task: WatchdogTask) -> None:
    """Fire-and-forget the investigation on the running loop (no-op if none is running)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.info("watchdog: no running loop; skipping dispatch for %s", task.target)
        return
    loop.create_task(investigate(task))


def install() -> None:
    """Register the real fan-out dispatch as the watchdog's dispatcher (replaces the log stub)."""
    set_dispatch(_schedule)
