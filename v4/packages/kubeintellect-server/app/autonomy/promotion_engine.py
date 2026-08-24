"""Statistical autonomy promotion engine (v5 P3 Trust plane, ADR-102).

The decision layer over the pure ADR-102 stats (`promotion_stats`): given an action class's
shadow-agreement outcomes, decide whether it earns the next rung, holds, or is auto-demoted — the
"earned, not configured" autonomy no published tool ships (C5). Precedence follows the ADR's
asymmetry: **fast down, slow up** — demotion is checked first, promotion only if not demoting.

The outcome **source** (per-class shadow-agreement Events reconstructed from flight-recorder data)
is pluggable and defaults empty, exactly like the change ledger: with no shadow data the engine
holds everything at its current rung (a no-op), and when action classes start running in shadow a
real source is registered via `set_outcome_source`. The decision logic is pure and fully tested
against the P0 replay fixtures.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NamedTuple

from app.autonomy.promotion_stats import (
    Event,
    evaluate_demotion,
    evaluate_promotion,
    rule_for,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

# source(action_class) -> that class's shadow-agreement outcome timeline.
OutcomeSource = Callable[[str], list[Event]]


def _empty_source(action_class: str) -> list[Event]:
    return []


_outcome_source: OutcomeSource = _empty_source


def set_outcome_source(fn: OutcomeSource) -> None:
    """Register the shadow-agreement outcome reader (populated once classes run in shadow)."""
    global _outcome_source
    _outcome_source = fn


class OutcomeRead(NamedTuple):
    """What a read of the outcome source produced, and whether it produced anything at all."""

    events: list[Event]
    source_failed: bool


def read_outcomes(action_class: str) -> OutcomeRead:
    """Read an action class's outcomes, saying whether the source answered. Never raises.

    "No outcomes yet" and "the source raised" are the same empty list, and until now they were
    also the same silence: the exception was swallowed with no log line anywhere. The decision
    that follows then reports ``hold`` with reasons like *"n 0 < n_min 20"* — which reads as
    *not enough evidence yet* when the truth is *the evidence could not be read*. The demotion
    direction is where that bites: ADR-102 is **fast down, slow up**, so a class whose shadow
    agreement has collapsed is held at its rung by a read failure, silently and indefinitely.

    Callers that only want the timeline can keep using :func:`outcomes_for`.
    """
    try:
        return OutcomeRead(list(_outcome_source(action_class)), False)
    except Exception as exc:
        logger.warning(f"promotion: outcome source failed for {action_class!r}: {exc}")
        return OutcomeRead([], True)


def outcomes_for(action_class: str) -> list[Event]:
    """Read an action class's outcomes from the registered source. Never raises."""
    return read_outcomes(action_class).events


@dataclass(frozen=True)
class EngineDecision:
    action: str                       # "promote" | "hold" | "demote"
    action_class: str
    transition: str
    to_rung: str
    lcb: float = 0.0
    n: int = 0
    reasons: list[str] = field(default_factory=list)


def decide(
    action_class: str,
    transition: str,
    current_rung: str,
    now_days: float,
    *,
    events: list[Event] | None = None,
    sev_attributed: bool = False,
    m4_at_l4: bool = False,
    class_drift: bool = False,
) -> EngineDecision:
    """Decide promote / hold / demote for ``action_class`` on ``transition``.

    Fast-down-slow-up: demotion is evaluated first; promotion only if not demoting. ``events`` may
    be injected (tests/backtests); otherwise they come from the registered outcome source.
    """
    evts = events if events is not None else outcomes_for(action_class)
    rule = rule_for(transition)
    theta = rule.theta if rule.theta is not None else 1.0

    demotion = evaluate_demotion(
        current_rung, theta, evts, now_days,
        sev_attributed=sev_attributed, m4_at_l4=m4_at_l4, class_drift=class_drift,
    )
    if demotion.demote:
        return EngineDecision("demote", action_class, transition, demotion.to_rung,
                              reasons=[demotion.reason])
    if demotion.stale:  # class-definition drift: hold at current rung, flagged for re-qualification
        return EngineDecision("hold", action_class, transition, current_rung,
                              reasons=[demotion.reason])

    promotion = evaluate_promotion(transition, evts, now_days)
    if promotion.promote:
        return EngineDecision("promote", action_class, transition, rule.to,
                              lcb=promotion.lcb, n=promotion.n)
    return EngineDecision("hold", action_class, transition, current_rung,
                          lcb=promotion.lcb, n=promotion.n, reasons=promotion.reasons)
