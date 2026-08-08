"""
Unit tests for kube_q/costs.py.

Covers: estimate_cost (known model, unknown model, prefix matching, overrides),
        format_tokens, format_cost.
"""

from kube_q.costs import (
    DEFAULT_COST_PER_1K,
    estimate_cost,
    format_cost,
    format_tokens,
)

# ── estimate_cost — known models ──────────────────────────────────────────────


def test_known_model_returns_correct_cost() -> None:
    cost = estimate_cost("gpt-4o", 1000, 1000)
    assert cost is not None
    expected = DEFAULT_COST_PER_1K["gpt-4o"]["prompt"] + DEFAULT_COST_PER_1K["gpt-4o"]["completion"]
    assert abs(cost - expected) < 1e-9


def test_kubeintellect_cost() -> None:
    cost = estimate_cost("kubeintellect-v2", 2000, 500)
    assert cost is not None
    expected = (2000 / 1000) * 0.003 + (500 / 1000) * 0.006
    assert abs(cost - expected) < 1e-9


def test_zero_tokens_returns_zero_cost() -> None:
    cost = estimate_cost("gpt-4o", 0, 0)
    assert cost == 0.0


# ── estimate_cost — unknown model ─────────────────────────────────────────────


def test_unknown_model_no_override_returns_none() -> None:
    cost = estimate_cost("totally-unknown-model-xyz", 100, 100)
    assert cost is None


def test_none_model_no_override_returns_none() -> None:
    cost = estimate_cost(None, 100, 100)
    assert cost is None


def test_unknown_model_with_both_overrides_returns_cost() -> None:
    cost = estimate_cost(
        "custom-model", 1000, 2000, override_prompt=0.001, override_completion=0.002
    )
    assert cost is not None
    expected = (1000 / 1000) * 0.001 + (2000 / 1000) * 0.002
    assert abs(cost - expected) < 1e-9


def test_unknown_model_with_only_prompt_override_returns_none() -> None:
    # Both overrides needed; only prompt provided → can't compute completion rate
    cost = estimate_cost("custom-model", 100, 100, override_prompt=0.001)
    assert cost is None


def test_unknown_model_with_only_completion_override_returns_none() -> None:
    cost = estimate_cost("custom-model", 100, 100, override_completion=0.001)
    assert cost is None


# ── estimate_cost — prefix matching ───────────────────────────────────────────


def test_prefix_match_with_date_suffix() -> None:
    cost_exact = estimate_cost("kubeintellect-v2", 1000, 500)
    cost_prefix = estimate_cost("kubeintellect-v2-20260401", 1000, 500)
    assert cost_prefix is not None
    assert cost_exact is not None
    assert abs(cost_prefix - cost_exact) < 1e-9


def test_prefix_match_gpt4o_mini() -> None:
    cost = estimate_cost("gpt-4o-mini-extended", 1000, 1000)
    assert cost is not None
    expected = (
        DEFAULT_COST_PER_1K["gpt-4o-mini"]["prompt"]
        + DEFAULT_COST_PER_1K["gpt-4o-mini"]["completion"]
    )
    assert abs(cost - expected) < 1e-9


def test_prefix_match_does_not_match_partial_word() -> None:
    # "gpt-4" does not prefix-match "gpt-4o" or "gpt-4o-mini"
    cost = estimate_cost("gpt-4-legacy", 100, 100)
    assert cost is None


# ── estimate_cost — override values ───────────────────────────────────────────


def test_overrides_take_precedence_over_known_model() -> None:
    # gpt-4o has prompt=0.005 but we override with 0.001
    cost = estimate_cost("gpt-4o", 1000, 0, override_prompt=0.001, override_completion=0.002)
    assert cost is not None
    assert abs(cost - 0.001) < 1e-9  # 1000/1000 * 0.001


def test_partial_override_prompt_uses_model_for_completion() -> None:
    # Known model; override prompt only → completion from model table
    cost = estimate_cost("gpt-4o", 1000, 1000, override_prompt=0.001)
    assert cost is not None
    expected = 0.001 + DEFAULT_COST_PER_1K["gpt-4o"]["completion"]
    assert abs(cost - expected) < 1e-9


def test_partial_override_completion_uses_model_for_prompt() -> None:
    cost = estimate_cost("gpt-4o", 1000, 1000, override_completion=0.001)
    assert cost is not None
    expected = DEFAULT_COST_PER_1K["gpt-4o"]["prompt"] + 0.001
    assert abs(cost - expected) < 1e-9


# ── format_tokens ─────────────────────────────────────────────────────────────


def test_format_tokens_basic() -> None:
    result = format_tokens(120, 340)
    assert "120" in result
    assert "340" in result
    assert "460" in result


def test_format_tokens_uses_arrow() -> None:
    result = format_tokens(100, 200)
    assert "→" in result


def test_format_tokens_total_is_sum() -> None:
    result = format_tokens(1000, 2000)
    assert "3,000" in result


def test_format_tokens_zero() -> None:
    result = format_tokens(0, 0)
    assert "0" in result


# ── format_cost ───────────────────────────────────────────────────────────────


def test_format_cost_none_returns_cost_unknown() -> None:
    assert format_cost(None) == "cost unknown"


def test_format_cost_zero() -> None:
    result = format_cost(0.0)
    assert result == "$0.0000"


def test_format_cost_typical_value() -> None:
    result = format_cost(0.0024)
    assert result == "$0.0024"


def test_format_cost_larger_value() -> None:
    result = format_cost(1.2345)
    assert "$" in result
    assert "1.2345" in result


def test_format_cost_very_small_value() -> None:
    result = format_cost(0.000001)
    assert "$" in result
    assert "0.000001" in result


def test_format_cost_starts_with_dollar() -> None:
    assert format_cost(0.05).startswith("$")
