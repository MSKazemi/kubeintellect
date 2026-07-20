"""Security-outcome eval (v5 P3, A-CH-04-20) — the zero-new-violations promotion gate."""
from __future__ import annotations

from app.eval.security_outcome import (
    SecurityOutcome,
    aggregate,
    gates_promotion,
    score_security_outcome,
)


class TestScore:
    def test_pure_fix(self):
        o = score_security_outcome({"v1", "v2"}, {"v2"})
        assert o.resolved == ["v1"] and o.introduced == []
        assert o.net_delta == -1 and o.clean is True

    def test_introduces_violation(self):
        o = score_security_outcome({"v1"}, {"v1", "v3"})
        assert o.introduced == ["v3"] and o.resolved == []
        assert o.net_delta == 1 and o.clean is False

    def test_mixed(self):
        o = score_security_outcome({"v1", "v2"}, {"v2", "v3"})
        assert o.introduced == ["v3"] and o.resolved == ["v1"]
        assert o.net_delta == 0 and o.clean is False   # introduced one ⇒ not clean

    def test_no_change(self):
        o = score_security_outcome({"v1"}, {"v1"})
        assert o.net_delta == 0 and o.clean is True


class TestGate:
    def test_clean_passes(self):
        assert gates_promotion(score_security_outcome({"v1"}, set())) is True

    def test_any_introduced_blocks(self):
        assert gates_promotion(score_security_outcome({"v1"}, {"v1", "v2"})) is False

    def test_no_op_passes_default(self):
        assert gates_promotion(score_security_outcome({"v1"}, {"v1"})) is True

    def test_require_net_improvement(self):
        # clean but no net reduction ⇒ blocked under the strict gate
        assert gates_promotion(score_security_outcome({"v1"}, {"v1"}), require_net_improvement=True) is False
        assert gates_promotion(score_security_outcome({"v1"}, set()), require_net_improvement=True) is True


class TestAggregate:
    def test_folds_corpus(self):
        outs = [
            score_security_outcome({"a", "b"}, {"b"}),     # resolved a
            score_security_outcome({"c"}, {"c", "d"}),     # introduced d
        ]
        agg = aggregate(outs)
        assert agg.introduced == ["d"] and agg.resolved == ["a"]
        assert agg.clean is False        # corpus introduced one ⇒ run fails the gate

    def test_empty(self):
        assert aggregate([]) == SecurityOutcome()
