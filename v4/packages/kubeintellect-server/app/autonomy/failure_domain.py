"""Change-schedule + failure-domain budget (v5 P3 blast-radius, REQ-sysadmin-18).

A second dimension of the blast-radius budget (alongside spend + staged propagation): a disruptive
op must not push a failure domain — a zone/rack/node-pool — past its unavailability cap, and must
fall inside an allowed maintenance window when one is required. So the agent can never take down
more than a safe fraction of any single domain at once, and disruptive changes respect change
freezes / maintenance schedules.

Pure/deterministic — fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DomainDecision:
    allow: bool
    reason: str = ""


def zone_disruption_ok(
    zone_total: int, currently_unavailable: int, *, max_unavailable_frac: float = 0.34,
) -> DomainDecision:
    """Would disrupting ONE more target in a zone keep it within its unavailability cap?

    Denies if (currently_unavailable + 1) / zone_total would exceed ``max_unavailable_frac``. A
    zone of size ≤1 is never auto-disrupted (no redundancy) unless the cap is ≥1.0.
    """
    if zone_total <= 0:
        return DomainDecision(False, "unknown zone size — fail-closed")
    projected = (currently_unavailable + 1) / zone_total
    if projected > max_unavailable_frac:
        return DomainDecision(
            False,
            f"would make {projected:.0%} of the zone unavailable (cap {max_unavailable_frac:.0%})",
        )
    return DomainDecision(True)


def in_maintenance_window(now_epoch: float, windows: list[tuple[float, float]]) -> bool:
    """True iff ``now`` is inside an allowed [start, end) maintenance window. Empty ⇒ always allowed
    (no schedule configured)."""
    if not windows:
        return True
    return any(start <= now_epoch < end for start, end in windows)


def gate_disruption(
    *,
    zone_total: int,
    currently_unavailable: int,
    max_unavailable_frac: float = 0.34,
    now_epoch: Optional[float] = None,
    maintenance_windows: Optional[list[tuple[float, float]]] = None,
) -> DomainDecision:
    """Compose the failure-domain + change-schedule checks. First denial wins (schedule → domain)."""
    if now_epoch is not None and maintenance_windows and not in_maintenance_window(now_epoch, maintenance_windows):
        return DomainDecision(False, "outside the allowed maintenance window")
    return zone_disruption_ok(zone_total, currently_unavailable, max_unavailable_frac=max_unavailable_frac)
