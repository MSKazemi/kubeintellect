"""Responsiveness: never-silent progress + latency budgets (v5 P2, REQ-developer-19).

The §9 responsiveness invariant: the AI path may not present as a silent block, and must not be
slower to first signal than the expert kubectl path it replaces. Two pure, injectable primitives:

- ``heartbeat`` — an async context manager that emits a progress StatusEvent every ``interval``
  seconds while a slow phase runs (gather LLM call, tool execution), so the SSE stream never goes
  quiet for longer than the heartbeat. Cancels cleanly on exit; a fast phase emits nothing.
- ``PhaseBudget`` — a deterministic latency tracker (injectable clock) that reports first-signal /
  full-investigation budget breaches for OpsMemBench latency families and operator-facing warnings.

Both are default-off behind ``KI_V5_RESPONSIVENESS`` at the call sites; with the flag off the
V4 streaming byte-stream is unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from app.streaming.emitter import Event, StatusEvent
from app.streaming.emitter import emit as _default_emit
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Contravariant in the event: an injected fake that accepts a broader type (``object``)
# still satisfies this, which is what the tests do.
EmitFn = Callable[[str, Event], Awaitable[None]]
Clock = Callable[[], float]


@contextlib.asynccontextmanager
async def heartbeat(
    session_id: str,
    phase: str,
    message: str,
    *,
    interval: float = 10.0,
    emit_fn: Optional[EmitFn] = None,
):
    """Emit a progress StatusEvent every ``interval`` s until the wrapped block exits.

    The first heartbeat fires after one full interval (a fast phase emits nothing). Any error in
    the beat loop is swallowed — a progress ping must never break the wrapped work.
    """
    # Bound to a separate non-Optional name: the closure below captures it, and a
    # reassignment of the parameter itself would not narrow away the ``None``.
    emit: EmitFn = emit_fn or _default_emit

    async def _beat() -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await emit(session_id, StatusEvent(
                    phase=phase, message=message, session_id=session_id,
                ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken heartbeat must not surface to the user.
            logger.debug("heartbeat suppressed error: %s", exc)

    task = asyncio.ensure_future(_beat()) if interval > 0 else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@dataclass
class PhaseBudget:
    """Deterministic first-signal / full-investigation latency tracker (injectable clock)."""
    first_signal_s: float = 30.0
    full_s: float = 120.0
    clock: Clock = time.monotonic

    _start: float = 0.0
    _first_signal_at: Optional[float] = None

    @classmethod
    def since(cls, start: float, *, first_signal_s: float = 30.0, full_s: float = 120.0,
              clock: Clock = time.monotonic) -> "PhaseBudget":
        """Build a budget already started at a known monotonic ``start`` (e.g. from graph state)."""
        b = cls(first_signal_s=first_signal_s, full_s=full_s, clock=clock)
        b._start = start
        return b

    def start(self) -> None:
        self._start = self.clock()
        self._first_signal_at = None

    def mark_first_signal(self) -> float:
        """Record (once) when the first useful result was produced; returns elapsed seconds."""
        if self._first_signal_at is None:
            self._first_signal_at = self.clock()
        return self._first_signal_at - self._start

    def elapsed(self) -> float:
        return self.clock() - self._start

    def first_signal_breached(self) -> bool:
        return self._first_signal_at is not None and (self._first_signal_at - self._start) > self.first_signal_s

    def full_breached(self) -> bool:
        return self.elapsed() > self.full_s

    def warning(self) -> str:
        """Operator-facing budget warning, or '' when within budget."""
        msgs = []
        if self.first_signal_breached():
            msgs.append(f"first signal {self.mark_first_signal():.0f}s > {self.first_signal_s:.0f}s target")
        if self.full_breached():
            msgs.append(f"full investigation {self.elapsed():.0f}s > {self.full_s:.0f}s target")
        return "; ".join(msgs)
