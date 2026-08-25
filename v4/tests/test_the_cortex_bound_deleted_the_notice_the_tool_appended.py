"""`cortex.graph._bound_tool_content` deleted the truncation notice `run_kubectl` had just written.

`run_kubectl` caps itself at 8 000 characters and appends `[truncated: N chars omitted …]`
*after* that cap, so an over-cap listing comes back at 8 173 characters. The cortex bound was
`content[:8000]` — the same number — which removed the notice with probability 1, on every
over-cap listing, and took the `[Protected] … withheld` sentence off a filtered one the same way.
The model then read a clipped listing as a complete one, on the route every non-OpenAI provider
is required to use (`config.py` warns that `LLM_PROVIDER=anthropic` needs `CORTEX_V4_ENABLED`).

The marker here is never hand-written: each test drives the real `run_kubectl` with a mocked
subprocess and feeds **its actual return value** to the bound, so the two constants staying equal
is the thing under test and not an assumption the fixture bakes in.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.agent.nodes import coordinator
from app.cortex.graph import _bound_tool_content
from app.tools.output_policy import POLICY_LINE_RE, split_policy_lines

# The two strings `_COORDINATOR_SYSTEM` tells the model to treat as "this list is incomplete".
PROMPT_PATTERNS = ("[truncated", "chars omitted")


def kubectl_output(stdout: str, command: str = "kubectl get pods -n default") -> str:
    """What `run_kubectl` really returns for `stdout` — caps, notices and all."""
    from app.tools.kubectl_tool import run_kubectl
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = ""
    proc.returncode = 0
    with patch("subprocess.run", return_value=proc):
        return run_kubectl.invoke({"command": command, "stdin": None})


def long_pod_table(n: int = 400) -> str:
    return "NAME   READY   STATUS    RESTARTS   AGE\n" + "\n".join(
        f"pod-{i:04d}  1/1  Running  0  3d" for i in range(n)
    )


class TestTheNoticeTheToolWroteSurvivesTheBound:
    def test_run_kubectl_really_does_append_past_its_own_cap(self):
        # The premise, measured rather than assumed: if this ever stops being true the rest of
        # this file is testing nothing.
        out = kubectl_output(long_pod_table())
        assert len(out) > 8_000
        assert "chars omitted" in out
        assert out.rstrip().endswith("]")

    def test_the_bound_no_longer_deletes_it(self):
        bounded = _bound_tool_content(kubectl_output(long_pod_table()))
        assert any(p in bounded for p in PROMPT_PATTERNS), (
            "the model was handed a clipped listing with nothing saying so"
        )

    def test_the_count_is_carried_across_unchanged(self):
        raw = kubectl_output(long_pod_table())
        notice = next(ln for ln in raw.splitlines() if "chars omitted" in ln)
        assert notice in _bound_tool_content(raw)

    def test_a_withheld_sentence_survives_too(self):
        from app.tools.namespace_guard import withheld_sentence
        raw = long_pod_table() + withheld_sentence(3, "namespace")
        bounded = _bound_tool_content(raw)
        assert "[Protected]" in bounded

    def test_it_survives_the_flagged_bound_as_well(self, monkeypatch):
        from app.core.config import settings
        monkeypatch.setattr(settings, "CORTEX_V5_ENABLED", True)
        monkeypatch.setattr(settings, "KI_V5_HARNESS_FANOUT", True)
        bounded = _bound_tool_content(kubectl_output(long_pod_table()))
        assert any(p in bounded for p in PROMPT_PATTERNS)

    def test_the_notice_is_the_last_thing_the_model_reads(self):
        bounded = _bound_tool_content(kubectl_output(long_pod_table()))
        assert POLICY_LINE_RE.search(bounded.splitlines()[-1])


class TestItStillBounds:
    def test_the_body_is_still_cut_to_the_budget(self):
        bounded = _bound_tool_content(kubectl_output(long_pod_table()))
        body, _ = split_policy_lines(bounded)
        # `rstrip` only removes the newline this function puts back before the notice — the
        # budget is on the rows, and 8 001 vs 8 000 is that separator, not a row.
        assert len(body.rstrip("\n")) <= 8_000

    def test_a_listing_that_fits_is_returned_byte_identical(self):
        raw = kubectl_output(long_pod_table(20))
        assert len(raw) < 8_000
        assert _bound_tool_content(raw) == raw

    def test_a_long_listing_with_no_policy_line_is_still_a_plain_chop(self):
        # v4's silence about its *own* chop is what the ADR-101 flag exists to change; this fix
        # is only about not destroying a sentence some other layer already wrote.
        raw = "x" * 9_000
        assert _bound_tool_content(raw) == raw[:8_000]

    def test_the_bound_actually_removes_rows(self):
        raw = kubectl_output(long_pod_table())
        assert len(_bound_tool_content(raw).splitlines()) < len(raw.splitlines())


class TestTheNoticeIsCarriedOnceAndOnlyOnce:
    """A policy line that lands *inside* the budget must not be counted twice.

    Lifting the policy lines out and re-attaching them is only correct if the bound then runs on
    the body. Bounding the original content instead looks identical for the common case — the
    notice sits at the very end, past the cut — and duplicates it for any listing whose notice
    arrives early, which is what a mid-stream `[Protected]` refusal inside a multi-command result
    looks like.
    """

    def _early_notice(self) -> str:
        rows = "\n".join(f"pod-{i:04d}  1/1  Running  0  3d" for i in range(400))
        return "NAME   READY   STATUS\n[Protected] 3 namespace(s) withheld by policy.\n" + rows

    def test_the_default_bound_does_not_duplicate_it(self):
        bounded = _bound_tool_content(self._early_notice())
        assert bounded.count("[Protected]") == 1

    def test_the_flagged_bound_does_not_duplicate_it_either(self, monkeypatch):
        from app.core.config import settings
        monkeypatch.setattr(settings, "CORTEX_V5_ENABLED", True)
        monkeypatch.setattr(settings, "KI_V5_HARNESS_FANOUT", True)
        bounded = _bound_tool_content(self._early_notice())
        assert bounded.count("[Protected]") == 1

    def test_an_early_notice_does_not_spend_the_row_budget_twice(self):
        raw = self._early_notice()
        body, _ = split_policy_lines(_bound_tool_content(raw))
        assert "[Protected]" not in body


class TestBothRoutesAskTheSameQuestion:
    # ⚠️ There is deliberately no `coordinator._POLICY_LINE_RE is POLICY_LINE_RE` assertion here.
    # `re.compile` caches, so two independent calls with the same pattern and flags return the
    # *same object* — an identity check passes for a copy-pasted duplicate and proves nothing.
    # What can be asserted is the only thing that matters at runtime: the two layers agree.

    @pytest.mark.parametrize("line", [
        "[Protected] 3 namespace(s) withheld — they belong to a blocked namespace.",
        "[truncated: 1200 chars omitted — output was cut short.]",
        "  [protected] lowercase still counts",
    ])
    def test_both_layers_classify_the_same_lines_as_policy(self, line):
        assert POLICY_LINE_RE.search(line)
        assert coordinator._POLICY_LINE_RE.search(line)

    @pytest.mark.parametrize("line", [
        "pod-0001  1/1  Running  0  3d",
        "NAME   READY   STATUS",
        "a line that merely mentions truncated output",
    ])
    def test_ordinary_rows_are_not_treated_as_policy(self, line):
        assert not POLICY_LINE_RE.search(line)


class TestSplitPolicyLines:
    def test_content_without_policy_lines_comes_back_unchanged(self):
        text = "a\nb\nc\n"
        body, policy = split_policy_lines(text)
        assert body == text and policy == ""

    def test_line_endings_are_preserved(self):
        body, _ = split_policy_lines("a\nb\n[Protected] x\n")
        assert body == "a\nb\n"

    def test_a_policy_line_in_the_middle_is_lifted_out(self):
        body, policy = split_policy_lines("a\n[truncated: 1 chars omitted]\nb\n")
        assert body == "a\nb\n"
        assert policy == "[truncated: 1 chars omitted]"

    def test_several_policy_lines_all_come_out(self):
        _, policy = split_policy_lines("a\n[Protected] one\nb\n[truncated: 2 chars omitted]\n")
        assert "[Protected] one" in policy and "[truncated" in policy

    def test_the_empty_string_is_not_a_special_case(self):
        assert split_policy_lines("") == ("", "")
