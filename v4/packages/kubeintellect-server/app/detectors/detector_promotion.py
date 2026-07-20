"""NL-detector → statistical ladder (v5 P4, ADR-012).

An NL-authored detector (ADR-012) starts in shadow — it fires but never reaches the watchtower.
This joins that flow to the statistical ladder: each shadow firing is scored true/false positive,
and when the detector's true-positive-rate lower bound clears the bar over enough firings, it is
SURFACED for human promotion to active. Human review is retained as the final gate — a detector is
NEVER auto-activated; the stats only decide when it has earned a review.

Pure/deterministic (reuses the ADR-102 Wilson-LCB) — fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.autonomy.promotion_stats import wilson_lcb


@dataclass(frozen=True)
class DetectorFiring:
    true_positive: bool     # did this shadow firing correspond to a real issue?


@dataclass(frozen=True)
class DetectorVerdict:
    ready_for_review: bool          # earned enough evidence to surface for HUMAN promotion
    precision_lcb: float
    firings: int
    reasons: list[str] = field(default_factory=list)


def evaluate_detector(
    firings: list[DetectorFiring], *, min_firings: int = 20, precision_theta: float = 0.90,
) -> DetectorVerdict:
    """Decide whether a shadow detector has earned a human promotion review.

    ``ready_for_review`` requires BOTH enough firings and a precision (TP-rate) LCB ≥ theta. Never
    activates anything — this only flags the detector for a human's final decision.
    """
    n = len(firings)
    tp = sum(1 for f in firings if f.true_positive)
    lcb = wilson_lcb(tp, n)
    reasons: list[str] = []
    if n < min_firings:
        reasons.append(f"only {n} shadow firings (< {min_firings})")
    if lcb < precision_theta:
        reasons.append(f"precision LCB {lcb:.3f} < θ {precision_theta}")
    return DetectorVerdict(ready_for_review=not reasons, precision_lcb=lcb, firings=n, reasons=reasons)
