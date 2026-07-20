"""Per-change ephemeral watchdog agents (v5 P4, A-CH-02-14).

The flagship MTTD play: a change (from the P1 change ledger) arms a bounded, TTL-scoped read-only
investigation that "reads the diff" and checks whether that specific change degraded health —
change-conditioned detection, not blanket polling. Degrades gracefully to status quo when off.

This module is the pure scheduler: turn recent changes into deduped, bounded WatchdogTasks with
TTLs. Dispatch (actually running the investigation via the harness fan-out) is pluggable and
defaults to a log-only stub — wiring it to a real bounded read-only investigation is the follow-up,
exactly as prospective memory defers its dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from app.cortex.change_rca import ChangeRecord
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class WatchdogTask:
    target: str
    kind: str
    objective: str
    created_epoch: float
    ttl_seconds: int
    dedup_key: str

    def expired(self, now_epoch: float) -> bool:
        return now_epoch - self.created_epoch > self.ttl_seconds


def _dedup_key(change: ChangeRecord) -> str:
    return f"{change.namespace}/{change.kind}/{change.target}"


def plan_watchdogs(
    changes: list[ChangeRecord], *, now_epoch: float, ttl_seconds: int = 300,
    max_active: int = 5, since_epoch: float = 0.0, seen: Optional[set[str]] = None,
) -> list[WatchdogTask]:
    """Arm a watchdog per recent, not-yet-watched change (most recent first), bounded by max_active.

    ``since_epoch`` filters to changes newer than it; ``seen`` dedups against already-armed watchdogs.
    """
    seen = seen if seen is not None else set()
    recent = sorted((c for c in changes if c.ts_epoch >= since_epoch),
                    key=lambda c: c.ts_epoch, reverse=True)
    tasks: list[WatchdogTask] = []
    for c in recent:
        key = _dedup_key(c)
        if key in seen:
            continue
        seen.add(key)
        tasks.append(WatchdogTask(
            target=c.target, kind=c.kind, dedup_key=key,
            objective=(f"A {c.kind} to {c.target}"
                       + (f" in {c.namespace}" if c.namespace else "")
                       + " just happened — verify it did not degrade health (readiness, restarts, "
                         "events); read the change, don't blanket-scan."),
            created_epoch=now_epoch, ttl_seconds=ttl_seconds,
        ))
        if len(tasks) >= max_active:
            break
    return tasks


# Pluggable dispatch — the real one runs a bounded read-only investigation (harness fan-out).
Dispatch = Callable[[WatchdogTask], None]


def _log_dispatch(task: WatchdogTask) -> None:
    logger.info("watchdog armed (log-only stub): %s", task.objective)


_dispatch: Dispatch = _log_dispatch


def set_dispatch(fn: Dispatch) -> None:
    global _dispatch
    _dispatch = fn


def fire(tasks: list[WatchdogTask]) -> int:
    """Dispatch each watchdog task. Returns the count fired. Never raises out."""
    n = 0
    for t in tasks:
        try:
            _dispatch(t)
            n += 1
        except Exception as exc:
            logger.warning("watchdog dispatch failed for %s: %s", t.target, exc)
    return n
