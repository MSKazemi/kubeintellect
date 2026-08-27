"""Unit tests for evaluation/scorers/aggregate.py."""
from __future__ import annotations

import pytest

# `evaluation/` is the offline scoring harness that produced the campaign numbers
# in the papers. It is deliberately not part of this repository (dropped in
# cb3dd1c) — the artifacts it carries are published on request, not shipped — so
# on any clone the imports below cannot resolve. Because they run at module
# scope, that was not one broken module: pytest aborted collection for the WHOLE
# v2 suite, so the other 13 modules never ran either, on a clone *or* here (the
# harness sits at the repo root, and `make test` runs from `v2/`). Skip this
# module when the harness is absent so the rest of the suite is runnable and CI
# can guard it. See #155.
pytest.importorskip(
    "evaluation",
    reason="needs the out-of-tree `evaluation/` scoring harness",
)

from evaluation.models import AgentTelemetry, SignalScores, TraceIssue  # noqa: E402
from evaluation.scorers.aggregate import WEIGHTS, WEIGHTS_WITH_VERIFY, aggregate  # noqa: E402


# ── Weight invariants ─────────────────────────────────────────────────────────

class TestWeightSums:
    def test_weights_sum_to_1(self):
        assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

    def test_weights_with_verify_sum_to_1(self):
        assert abs(sum(WEIGHTS_WITH_VERIFY.values()) - 1.0) < 1e-9

    def test_weights_has_required_keys(self):
        required = {"completion", "tool_correctness", "error_free", "hitl_discipline", "latency"}
        assert required == set(WEIGHTS.keys())

    def test_weights_with_verify_has_cluster_resolved(self):
        assert "cluster_resolved" in WEIGHTS_WITH_VERIFY


# ── Helpers ───────────────────────────────────────────────────────────────────


def _scores(
    *,
    completion: float = 1.0,
    tool_correctness: float = 1.0,
    error_free: float = 1.0,
    hitl_discipline: float = 1.0,
    latency: float = 1.0,
    cluster_resolved: float | None = None,
) -> SignalScores:
    return SignalScores(
        completion=completion,
        tool_correctness=tool_correctness,
        error_free=error_free,
        hitl_discipline=hitl_discipline,
        latency=latency,
        cluster_resolved=cluster_resolved,
    )


# ── Path selection ────────────────────────────────────────────────────────────

class TestPathSelection:
    def test_perfect_without_verify_is_1(self):
        assert aggregate(_scores()) == 1.0

    def test_perfect_with_verify_is_1(self):
        assert aggregate(_scores(cluster_resolved=1.0)) == 1.0

    def test_zero_without_verify_is_0(self):
        s = _scores(completion=0, tool_correctness=0, error_free=0, hitl_discipline=0, latency=0)
        assert aggregate(s) == 0.0

    def test_cluster_resolved_0_drives_score_down(self):
        s = _scores(cluster_resolved=0.0)
        assert aggregate(s) < 1.0

    def test_no_cluster_resolved_uses_no_verify_weights(self):
        s = _scores(completion=1.0, tool_correctness=0.0, error_free=0.0,
                    hitl_discipline=0.0, latency=0.0, cluster_resolved=None)
        expected = round(WEIGHTS["completion"], 4)
        assert aggregate(s) == expected

    def test_with_verify_uses_verify_weights(self):
        # cluster_resolved=1, all others 0
        s = _scores(completion=0.0, tool_correctness=0.0, error_free=0.0,
                    hitl_discipline=0.0, latency=0.0, cluster_resolved=1.0)
        expected = round(WEIGHTS_WITH_VERIFY["cluster_resolved"], 4)
        assert aggregate(s) == expected


# ── TOKEN_EXPLOSION penalty ───────────────────────────────────────────────────

class TestTokenExplosionPenalty:
    def test_token_explosion_reduces_score(self):
        issue = TraceIssue(id="TOKEN_EXPLOSION", severity="LOW", detail="too many tokens")
        base = aggregate(_scores())
        penalised = aggregate(_scores(), issues=[issue])
        assert penalised < base

    def test_other_issues_have_no_effect(self):
        issue = TraceIssue(id="SLOW_QUERY", severity="MEDIUM", detail="slow")
        base = aggregate(_scores())
        with_issue = aggregate(_scores(), issues=[issue])
        assert with_issue == base

    def test_penalty_does_not_go_below_zero(self):
        issue = TraceIssue(id="TOKEN_EXPLOSION", severity="LOW", detail="")
        s = _scores(completion=0, tool_correctness=0, error_free=0, hitl_discipline=0, latency=0)
        assert aggregate(s, issues=[issue]) >= 0.0


# ── Telemetry bonuses ─────────────────────────────────────────────────────────

class TestTelemetryBonuses:
    def test_no_telemetry_no_bonus(self):
        base = aggregate(_scores())
        assert aggregate(_scores(), telemetry=None) == base

    def test_playbook_hit_adds_005(self):
        tel = AgentTelemetry(matched_playbooks=["CrashLoopBackOff"])
        # Use a base that leaves room for bonuses (completion=0.9 leaves 0.1 space)
        s = _scores(completion=0.9, tool_correctness=0.9, error_free=0.9,
                    hitl_discipline=0.9, latency=0.9)
        base = aggregate(s)
        with_tel = aggregate(s, telemetry=tel)
        assert abs((with_tel - base) - 0.05) < 0.001

    def test_plan_emitted_adds_003(self):
        tel = AgentTelemetry(investigation_plan=["Check pods", "Check events"])
        s = _scores(completion=0.9, tool_correctness=0.9, error_free=0.9,
                    hitl_discipline=0.9, latency=0.9)
        base = aggregate(s)
        with_tel = aggregate(s, telemetry=tel)
        assert abs((with_tel - base) - 0.03) < 0.001

    def test_both_bonuses_add_008(self):
        tel = AgentTelemetry(
            matched_playbooks=["CrashLoopBackOff"],
            investigation_plan=["Check pods"],
        )
        s = _scores(completion=0.9, tool_correctness=0.9, error_free=0.9,
                    hitl_discipline=0.9, latency=0.9)
        base = aggregate(s)
        with_tel = aggregate(s, telemetry=tel)
        assert abs((with_tel - base) - 0.08) < 0.001

    def test_empty_playbooks_no_bonus(self):
        tel = AgentTelemetry(matched_playbooks=[])
        s = _scores()
        assert aggregate(s, telemetry=tel) == aggregate(s)

    def test_empty_plan_no_bonus(self):
        tel = AgentTelemetry(investigation_plan=[])
        s = _scores()
        assert aggregate(s, telemetry=tel) == aggregate(s)

    def test_score_capped_at_1(self):
        tel = AgentTelemetry(
            matched_playbooks=["CrashLoopBackOff"],
            investigation_plan=["step"],
        )
        assert aggregate(_scores(), telemetry=tel) == 1.0
