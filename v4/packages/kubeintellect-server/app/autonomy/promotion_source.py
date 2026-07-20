"""Postgres-backed shadow-agreement outcome store (v5 P3, ADR-102).

Closes the promotion loop: the statistical engine (`promotion_engine`) needs per-action-class
shadow outcomes, and this is the durable source for them. When an action class runs in shadow, each
attempt's agreement/success is recorded here; the engine reads them back to decide promote/hold/
demote. Purpose-built table (not the hash-chained flight recorder — these are statistical samples).

``decide_from_store`` is the real wiring: async-fetch a class's outcomes from Postgres, then run the
pure engine decision. The engine's sync ``set_outcome_source`` stays for in-memory/cache use.
"""

from __future__ import annotations

from typing import Any

from app.autonomy.promotion_engine import EngineDecision, decide
from app.autonomy.promotion_stats import Event


async def record_outcome(pool: Any, action_class: str, event: Event) -> None:
    """Persist one shadow-agreement outcome for ``action_class``."""
    await pool.execute(
        "INSERT INTO promotion_outcomes (action_class, ts_days, success, incident_id, "
        "incident_type, critical) VALUES ($1, $2, $3, $4, $5, $6)",
        action_class, event.ts_days, event.success, event.incident_id,
        event.incident_type, event.critical,
    )


async def outcomes_from_store(pool: Any, action_class: str, *, limit: int = 500) -> list[Event]:
    """Read a class's shadow outcomes (most recent first, then chronological) as Events."""
    rows = await pool.fetch(
        "SELECT ts_days, success, incident_id, incident_type, critical FROM promotion_outcomes "
        "WHERE action_class = $1 ORDER BY ts_days DESC LIMIT $2",
        action_class, limit,
    )
    events = [Event(ts_days=r["ts_days"], success=r["success"], incident_id=r["incident_id"],
                    incident_type=r["incident_type"], critical=r["critical"]) for r in rows]
    events.reverse()   # chronological for the windowing math
    return events


async def decide_from_store(
    pool: Any, action_class: str, transition: str, current_rung: str, now_days: float, **kw: Any,
) -> EngineDecision:
    """Fetch a class's real recorded outcomes, then run the pure promotion decision."""
    events = await outcomes_from_store(pool, action_class)
    return decide(action_class, transition, current_rung, now_days, events=events, **kw)
