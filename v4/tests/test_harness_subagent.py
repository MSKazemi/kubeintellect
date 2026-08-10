"""Unit tests for the ADR-101 investigation-subagent contract (v5 specs/00)."""

from __future__ import annotations

import pytest
from app.cortex.harness import (
    SubagentContract,
    bound_summary,
    enforce_read_only_allowlist,
)
from app.cortex.harness.subagent import (
    SUMMARY_MAX_CHARS,
    SubagentContractError,
    finalize_result,
)
from app.tools.aci import ACI_READ_VERB_ALLOWLIST


def test_default_contract_allows_only_the_four_read_verbs():
    c = SubagentContract(objective="find the crashloop cause")
    assert c.allowed_verbs == ACI_READ_VERB_ALLOWLIST
    assert c.may_mutate is False


def test_contract_rejects_mutation():
    with pytest.raises(SubagentContractError):
        SubagentContract(objective="x", may_mutate=True)


def test_allowlist_rejects_non_read_verb():
    with pytest.raises(SubagentContractError):
        enforce_read_only_allowlist(frozenset({"inspect", "apply"}))


def test_allowlist_accepts_subset_of_read_verbs():
    enforce_read_only_allowlist(frozenset({"inspect", "logs"}))  # no raise


def test_bound_summary_under_budget_untouched():
    text = "short summary"
    out, truncated = bound_summary(text)
    assert out == text and truncated is False


def test_bound_summary_over_budget_truncates_with_marker():
    text = "x" * (SUMMARY_MAX_CHARS + 500)
    out, truncated = bound_summary(text)
    assert truncated is True
    assert len(out) <= SUMMARY_MAX_CHARS + 80  # marker allowance
    assert "truncated" in out


def test_finalize_result_enforces_allowlist_and_bounds():
    c = SubagentContract(objective="investigate")
    res = finalize_result(c, "found it: pod OOMed", ["inspect", "logs"])
    assert res.truncated is False
    assert res.verbs_used == ("inspect", "logs")


def test_finalize_result_rejects_out_of_allowlist_usage():
    c = SubagentContract(objective="investigate")
    with pytest.raises(SubagentContractError):
        finalize_result(c, "summary", ["inspect", "delete"])
