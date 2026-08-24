"""Wilson-LCB autonomy-promotion criterion (v5 ADR-102, specs/00 trust keystone).

Pure, deterministic, auditor-reproducible: an action-class is promoted a rung only when the
one-sided 95% Wilson score lower confidence bound of its transition metric over a rolling
window clears the per-transition threshold AND the n_min / min-time / diversity / zero-critical
gates all pass. No LLM, no DB, no wallclock inside the math — a timeline is passed in.

Constants are the ADR-102 pinned-draft values, marked ``draft-for-simulation`` pending the P0
replay-fixture study (specs/04). Nothing here promotes anything on its own; it is the decision
function the trust plane calls.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

# One-sided z for α=0.05.
_Z_95 = 1.6448536269514722

# Rolling window: last N events OR D days, whichever is smaller.
WINDOW_MAX_EVENTS = 200
WINDOW_MAX_DAYS = 90

Rung = Literal["L1", "L2", "L3", "L4"]
RollbackClass = Literal["versioned-workload", "declarative-revert", "irreversible"]


@dataclass(frozen=True)
class TransitionRule:
    frm: Rung
    to: Rung
    theta: float | None        # LCB₉₅ threshold; None ⇒ never
    n_min: int
    t_min_days: int
    min_incidents: int
    min_types: int


# ADR-102 §"Threshold table" (pinned-draft-for-simulation).
_RULES: dict[str, TransitionRule] = {
    "L1->L2":                     TransitionRule("L1", "L2", 0.95, 20, 7, 5, 1),
    "L2->L3":                     TransitionRule("L2", "L3", 0.90, 30, 14, 10, 3),
    "L3->L4:versioned-workload":  TransitionRule("L3", "L4", 0.95, 60, 30, 15, 1),
    "L3->L4:declarative-revert":  TransitionRule("L3", "L4", 0.99, 300, 90, 30, 1),
    "L3->L4:irreversible":        TransitionRule("L3", "L4", None, 0, 0, 0, 0),  # hard ceiling
}


@dataclass(frozen=True)
class Event:
    """One outcome of an action-class attempt (built from flight-recorder fixtures)."""
    ts_days: float          # monotonic day offset (no wallclock in the math)
    success: bool           # transition metric numerator
    incident_id: str
    incident_type: str = "generic"
    critical: bool = False  # M4: a Sev-1/Sev-2 attributed to this action
    offline: bool = False   # derived from offline analysis (down-weighted) vs a live shadow run


@dataclass
class PromotionDecision:
    promote: bool
    transition: str
    lcb: float
    n: int
    theta: float | None
    reasons: list[str] = field(default_factory=list)  # which gates failed (empty ⇒ all passed)


def wilson_lcb(successes: float, n: float, z: float = _Z_95) -> float:
    """One-sided lower Wilson score bound for a binomial proportion. n<=0 ⇒ 0.0.

    Closed-form and deterministic — reproducible from span rows alone (ADR-102). Accepts float
    counts so it also serves the offline-weighted variant below.
    """
    if n <= 0:
        return 0.0
    phat = successes / n
    denom = 1.0 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return max(0.0, (center - margin) / denom)


def calibrate_offline_weight(
    matched: list[tuple[bool, bool]], *, cap: float = 0.5,
) -> float:
    """ADR-102 arm-3 calibration harness: derive the offline-shadow weight from MATCHED
    (offline_verdict, live_verdict) pairs — how much an offline-derived outcome should count vs a
    live shadow run. The weight is the offline↔live agreement rate, clamped to ``cap`` (offline
    evidence never outweighs live). Empty ⇒ 0.0 (no evidence ⇒ don't trust offline at all).

    This discharges arm-3's *methodology* (simulation-validatable like arms 1/2/4); the production
    weight still requires REAL matched offline+live samples fed in — data that accrues over time.
    """
    if not matched:
        return 0.0
    agreement = sum(1 for off, live in matched if off == live) / len(matched)
    return max(0.0, min(cap, agreement))


def weighted_lcb(events: list[Event], *, offline_weight: float, z: float = _Z_95) -> float:
    """Wilson-LCB where offline-derived events count ``offline_weight`` (≤ ADR-102 cap 0.5) and live
    events count 1.0 — arm-3's offline-shadow weighting. The cap VALUE awaits live calibration, but
    the mechanism is here: more offline (vs live) evidence ⇒ a lower effective LCB (less certainty).
    """
    w = max(0.0, min(1.0, offline_weight))
    n = sum(w if e.offline else 1.0 for e in events)
    s = sum((w if e.offline else 1.0) for e in events if e.success)
    return wilson_lcb(s, n, z)


def _window(events: list[Event], now_days: float) -> list[Event]:
    """Last WINDOW_MAX_EVENTS within WINDOW_MAX_DAYS of ``now_days``."""
    recent = [e for e in events if now_days - e.ts_days <= WINDOW_MAX_DAYS]
    recent.sort(key=lambda e: e.ts_days)
    return recent[-WINDOW_MAX_EVENTS:]


def rule_for(transition: str) -> TransitionRule:
    if transition not in _RULES:
        raise KeyError(f"unknown transition {transition!r}; known: {sorted(_RULES)}")
    return _RULES[transition]


def evaluate_promotion(transition: str, events: list[Event], now_days: float) -> PromotionDecision:
    """Decide whether ``transition`` may fire given the event timeline as of ``now_days``.

    All gates must pass. ``reasons`` lists every failed gate (empty ⇒ promote).
    """
    rule = rule_for(transition)
    win = _window(events, now_days)
    n = len(win)
    successes = sum(1 for e in win if e.success)
    lcb = wilson_lcb(successes, n)
    reasons: list[str] = []

    if rule.theta is None:  # irreversible ⇒ never
        return PromotionDecision(False, transition, lcb, n, None, ["irreversible: hard ceiling, never auto-promoted"])

    if lcb < rule.theta:
        reasons.append(f"LCB {lcb:.4f} < θ {rule.theta}")
    if n < rule.n_min:
        reasons.append(f"n {n} < n_min {rule.n_min}")
    span_days = (win[-1].ts_days - win[0].ts_days) if n >= 2 else 0.0
    if span_days < rule.t_min_days:
        reasons.append(f"time span {span_days:.1f}d < T_min {rule.t_min_days}d")
    distinct_incidents = len({e.incident_id for e in win})
    if distinct_incidents < rule.min_incidents:
        reasons.append(f"distinct incidents {distinct_incidents} < {rule.min_incidents}")
    distinct_types = len({e.incident_type for e in win})
    if distinct_types < rule.min_types:
        reasons.append(f"distinct types {distinct_types} < {rule.min_types}")
    if any(e.critical for e in win):
        reasons.append("M4 > 0: a critical event occurred in the window")

    return PromotionDecision(not reasons, transition, lcb, n, rule.theta, reasons)


# ── Demotion (ADR-102 §"Demotion is automatic, asymmetric — fast down, slow up") ──
# Promotion earns rungs slowly; demotion drops them fast and without approval. These are the
# five triggers the ADR mandates, deterministic and auditor-recomputable from the same events.

RUNGS: list[str] = ["L0", "L1", "L2", "L3", "L4"]

HYSTERESIS_LAST_N = 50          # LCB window for the anti-flap band
HYSTERESIS_BAND = 0.05          # demote if last-50 LCB < θ − 0.05
CUSUM_FAILS = 2                 # postcondition failures …
CUSUM_WINDOW_DAYS = 1.0         # … within 24 h ⇒ trip (don't wait for the rolling window)
SEV_ATTRIBUTION_DROP = 2        # Sev-1/Sev-2 attributed to an L4 action ⇒ drop two rungs
FLEET_FREEZE_DAYS = 14          # … and freeze the class fleet-wide


def _rung_index(rung: str) -> int:
    if rung not in RUNGS:
        raise KeyError(f"unknown rung {rung!r}")
    return RUNGS.index(rung)


def _demote_to(rung: str, rungs_down: int) -> str:
    return RUNGS[max(0, _rung_index(rung) - rungs_down)]


@dataclass(frozen=True)
class DemotionDecision:
    demote: bool
    from_rung: str
    to_rung: str
    reason: str
    fleet_freeze_days: int = 0
    stale: bool = False           # class-definition drift flags the rung for re-qualification


def cusum_trip(events: list[Event], *, fails: int = CUSUM_FAILS,
               window_days: float = CUSUM_WINDOW_DAYS) -> bool:
    """True iff ``fails`` postcondition failures fall within any ``window_days`` window.
    Checked on raw events (not the rolling window) — the ADR's "don't wait for the window".

    EVERY failure counts, including one that also caused a critical incident. This used to read
    ``not e.success and not e.critical``, which made the worst failures the only ones the fast
    trigger could not see. Measured 2026-08-24 on an L3 class with 48 clean runs and then two
    failures 12 h apart: as ordinary failures they tripped CUSUM and the class dropped to L2; with
    ``critical=True`` on the same two events the answer was *"no demotion trigger"* and the class
    stayed at L3. The severity triggers that would have caught them (§4.4 rows 1 and 4) are
    L4-scoped by the ADR's own wording, so below L4 nothing else was watching — and hysteresis
    cannot help, because a class with an earned record keeps its last-50 LCB above θ − 0.05
    through exactly two failures. Promotion already treats a critical as disqualifying
    (``M4 > 0``); demotion must not treat it as exculpatory.
    """
    fail_ts = sorted(e.ts_days for e in events if not e.success)
    if len(fail_ts) < fails:
        return False
    for i in range(len(fail_ts) - fails + 1):
        if fail_ts[i + fails - 1] - fail_ts[i] <= window_days:
            return True
    return False


def hysteresis_breach(events: list[Event], theta: float, *,
                      last_n: int = HYSTERESIS_LAST_N, band: float = HYSTERESIS_BAND) -> bool:
    """True iff the LCB over the last ``last_n`` events has fallen below ``θ − band``."""
    win = sorted(events, key=lambda e: e.ts_days)[-last_n:]
    if not win:
        return False
    successes = sum(1 for e in win if e.success)
    return wilson_lcb(successes, len(win)) < theta - band


def evaluate_demotion(
    current_rung: str, theta: float, events: list[Event], now_days: float, *,
    sev_attributed: bool = False, m4_at_l4: bool = False, class_drift: bool = False,
) -> DemotionDecision:
    """Return the single most-severe demotion action that fires (or none).

    Precedence mirrors the ADR: Sev-attribution (2-rung + freeze) → M4-at-L4 (→L3) → class drift
    (stale/re-qualify) → CUSUM trip (−1) → hysteresis breach (−1).
    """
    at_l4 = current_rung == "L4"
    if sev_attributed and at_l4:
        return DemotionDecision(True, current_rung, _demote_to(current_rung, SEV_ATTRIBUTION_DROP),
                                "Sev-1/2 attributed to an L4 action: 2-rung drop + fleet freeze",
                                fleet_freeze_days=FLEET_FREEZE_DAYS)
    if m4_at_l4 and at_l4:
        return DemotionDecision(True, current_rung, "L3", "M4 critical event at L4: immediate demote")
    if class_drift:
        return DemotionDecision(False, current_rung, current_rung,
                                "class-definition drift: rung flagged stale, re-qualification required",
                                stale=True)
    win = _window(events, now_days)
    if cusum_trip(win):
        # The action is −1 either way; the reason still has to say what happened, or the audit
        # trail records a routine trip where a critical incident occurred.
        sev = " (at least one caused a critical incident)" if any(
            e.critical and not e.success for e in win) else ""
        return DemotionDecision(True, current_rung, _demote_to(current_rung, 1),
                                f"CUSUM: {CUSUM_FAILS} postcondition failures within 24 h{sev}")
    if hysteresis_breach(win, theta):
        return DemotionDecision(True, current_rung, _demote_to(current_rung, 1),
                                f"hysteresis: last-{HYSTERESIS_LAST_N} LCB < θ − {HYSTERESIS_BAND}")
    if sev_attributed or m4_at_l4:
        # The 2-rung drop and the M4 rule are L4-scoped in ADR §4.4, so neither applies here and
        # this function must not invent a policy the ADR does not state. What it must not do is
        # discard the caller's severity signal in silence: "no demotion trigger" would report a
        # quiet class while a Sev-1 was attributed to it.
        which = " and ".join(
            n for n, on in (("Sev-1/2 attribution", sev_attributed), ("M4 critical", m4_at_l4)) if on)
        return DemotionDecision(
            False, current_rung, current_rung,
            f"{which} reported at {current_rung}, where ADR §4.4 scopes that trigger to L4; no "
            f"other trigger fired — this is NOT a clean class, escalate for manual review")
    return DemotionDecision(False, current_rung, current_rung, "no demotion trigger")
