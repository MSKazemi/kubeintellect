"""NL-detector → statistical ladder (v5 P4, ADR-012) — shadow precision gate for human review."""
from __future__ import annotations

from app.detectors.detector_promotion import DetectorFiring, evaluate_detector


def _firings(n, tp):
    return [DetectorFiring(true_positive=(i < tp)) for i in range(n)]


class TestEvaluate:
    def test_high_precision_over_enough_firings_ready(self):
        v = evaluate_detector(_firings(60, 60), min_firings=20, precision_theta=0.90)
        assert v.ready_for_review is True and v.precision_lcb >= 0.90 and v.firings == 60

    def test_too_few_firings_not_ready(self):
        v = evaluate_detector(_firings(10, 10), min_firings=20)
        assert v.ready_for_review is False and any("shadow firings" in r for r in v.reasons)

    def test_low_precision_not_ready(self):
        v = evaluate_detector(_firings(60, 40), precision_theta=0.90)   # 67% ⇒ LCB well below 0.90
        assert v.ready_for_review is False and any("precision LCB" in r for r in v.reasons)

    def test_empty_not_ready(self):
        v = evaluate_detector([], min_firings=20)
        assert v.ready_for_review is False and v.firings == 0

    def test_never_auto_activates_only_flags_review(self):
        # the verdict is advisory: even "ready" only means ready_for_review, not active
        v = evaluate_detector(_firings(60, 60))
        assert hasattr(v, "ready_for_review") and not hasattr(v, "activated")
