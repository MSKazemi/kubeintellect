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
    # The budget applies to the summary; the marker is the cost of saying so. Measured against
    # the marker this build actually emits rather than a fixed allowance — the marker carries a
    # count now, so its length varies with the number, and an "80 chars ought to do" bound fails
    # on a wider one for no reason connected to the budget.
    marker = out.splitlines()[-1]
    body = "\n".join(out.splitlines()[:-1])
    assert len(body) <= SUMMARY_MAX_CHARS
    assert "[truncated" in marker and "chars omitted" in marker


def test_finalize_result_enforces_allowlist_and_bounds():
    c = SubagentContract(objective="investigate")
    res = finalize_result(c, "found it: pod OOMed", ["inspect", "logs"])
    assert res.truncated is False
    assert res.verbs_used == ("inspect", "logs")


def test_finalize_result_rejects_out_of_allowlist_usage():
    c = SubagentContract(objective="investigate")
    with pytest.raises(SubagentContractError):
        finalize_result(c, "summary", ["inspect", "delete"])
