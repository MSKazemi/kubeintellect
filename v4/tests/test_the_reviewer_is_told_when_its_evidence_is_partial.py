"""Evidence handed to the adversarial reviewer must never be silently clipped.

THE DEFECT
----------
`_gathered_evidence` ended in a bare `[:8000]`. That text is the *entire* world of the
adversarial RCA reviewer: it is given the claim and this string, asked which statements the
evidence does not support, and told by its system prompt to treat any "not found" / "missing"
conclusion with SUSPICION. So a silent cut does not merely lose evidence — it manufactures the
reviewer's grounds for objecting, and the caveat it produces is rendered to the user.

Measured 2026-08-24 on a six-read gather with the decisive lines arriving last:

    evidence actually gathered  8,749 chars
    given to the reviewer       8,000 chars  (91%)
    'OOMKilled' in the real evidence      True
    'OOMKilled' in the reviewer's copy    False
    truncation marker in the reviewer's copy   None
    last characters handed over  '…  web-021   1/1   Running   0   4d\n  web-022   1/1'

That final fragment is a partial row that reads as a complete one, and the two lines carrying
the root cause were 749 characters past the cut. Note what this file does NOT claim: no LLM was
run here, so this is not a measurement of the reviewer wrongly flagging a claim. What is proven
is that the reviewer's input was incomplete, misleading at its boundary, and unmarked.

The same subsystem already had the never-silent form (`harness.bound_summary`, line-aligned and
stamped) and the same language for partial tool output ("absence from it is NOT evidence") —
neither had been applied here.

WHAT IS ASSERTED
----------------
1. Under budget, the evidence is returned verbatim — no marker, nothing added.
2. Over budget, the cut is line-aligned, marked, and states how much was dropped and that the
   dropped part is the most recent evidence.
3. The marker survives into the string the reviewer is actually given.
4. The bound still bounds: the result does not grow without limit.
"""

from __future__ import annotations

import pytest
from app.cortex.graph import _EVIDENCE_BUDGET, _gathered_evidence
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def _msgs(n_filler: int, decisive: bool = True) -> list:
    out = [
        ToolMessage(
            tool_call_id=f"t{i}",
            content=f"kubectl get pods (batch {i})\n"
            + "\n".join(f"  web-{j:03d}   1/1   Running   0   4d" for j in range(40)),
        )
        for i in range(n_filler)
    ]
    if decisive:
        out.append(ToolMessage(
            tool_call_id="tX",
            content="kubectl describe pod web-1\n  Reason:    OOMKilled\n  memory: 128Mi\n",
        ))
    return out


def _ev(msgs, start: int = 0) -> str:
    return _gathered_evidence({"messages": msgs, "turn_start_index": start})


class TestUnderBudgetIsUntouched:
    def test_short_evidence_is_returned_verbatim(self):
        msgs = _msgs(0)
        out = _ev(msgs)
        assert out == msgs[0].content
        assert "TRUNCATED" not in out, "a marker on evidence that was never cut is a lie too"

    def test_evidence_exactly_at_the_budget_is_not_marked(self):
        msgs = [ToolMessage(tool_call_id="t", content="x" * _EVIDENCE_BUDGET)]
        out = _ev(msgs)
        assert len(out) == _EVIDENCE_BUDGET and "TRUNCATED" not in out

    def test_empty_evidence_stays_empty(self):
        assert _ev([]) == ""
        assert _ev([ToolMessage(tool_call_id="t", content="   ")]) == ""

    def test_the_turn_start_index_is_still_honoured(self):
        msgs = [HumanMessage(content="earlier turn"), ToolMessage(tool_call_id="t", content="this turn")]
        assert _ev(msgs, start=1) == "this turn"
        assert "earlier turn" in _ev(msgs, start=0), "vacuity guard: the slice does nothing"


class TestOverBudgetIsMarked:
    def test_a_cut_is_announced(self):
        out = _ev(_msgs(6))
        assert "TRUNCATED" in out
        assert "NOT evidence that it was not observed" in out

    def test_the_marker_says_how_much_was_dropped(self):
        msgs = _msgs(6)
        full = "\n\n".join(m.content for m in msgs)
        out = _ev(msgs)
        assert str(len(full)) in out, "the total size of the real evidence is not stated"
        # and the stated drop is arithmetically right
        body = out.split("\n\n…[TRUNCATED:")[0]
        assert str(len(full) - len(body.rstrip())) in out

    def test_the_marker_says_which_end_was_lost(self):
        # Head-first truncation drops the newest evidence, which is usually the decisive part.
        assert "MOST RECENT" in _ev(_msgs(6))

    def test_the_cut_lands_on_a_line_boundary(self):
        out = _ev(_msgs(6))
        body = out.split("\n\n…[TRUNCATED:")[0]
        # every retained line is a whole line of the original
        original = "\n\n".join(m.content for m in _msgs(6))
        assert original.startswith(body), "the retained text is not a prefix of the evidence"
        assert original[len(body)] in "\n ", "the cut landed inside a line"

    def test_a_pathological_single_line_still_bounds(self):
        # No line break to align to: it must still cut, and still say so.
        msgs = [ToolMessage(tool_call_id="t", content="y" * (_EVIDENCE_BUDGET * 3))]
        out = _ev(msgs)
        assert "TRUNCATED" in out
        assert len(out) < _EVIDENCE_BUDGET * 2, "the bound stopped bounding"

    def test_a_wastefully_early_line_break_is_not_used(self):
        # Line-aligning is a preference, not a rule. One header line followed by a single huge
        # line has its only newline at offset 6 — aligning to it would hand the reviewer the
        # word "header" and call it the evidence.
        msgs = [ToolMessage(tool_call_id="t", content="header\n" + "z" * (_EVIDENCE_BUDGET * 2))]
        out = _ev(msgs)
        body = out.split("\n\n…[TRUNCATED:")[0]
        assert len(body) > _EVIDENCE_BUDGET // 2, (
            f"aligning to an early newline kept only {len(body)} chars of evidence"
        )

    @pytest.mark.parametrize("n", [6, 12, 40])
    def test_the_result_stays_bounded(self, n):
        out = _ev(_msgs(n))
        assert len(out) <= _EVIDENCE_BUDGET + 400, f"{n} batches produced {len(out)} chars"


class TestWhatTheReviewerActuallyReceives:
    def test_the_decisive_tail_is_gone_but_its_absence_is_declared(self):
        # This is the measured scenario. The fix does not recover the evidence — it stops the
        # reviewer from reading its absence as a fact.
        out = _ev(_msgs(6))
        assert "OOMKilled" not in out, "vacuity guard: the tail was not actually dropped"
        assert "TRUNCATED" in out

    def test_the_reviewer_input_does_not_end_mid_row(self):
        out = _ev(_msgs(6))
        body = out.split("\n\n…[TRUNCATED:")[0]
        last = body.rstrip().rsplit("\n", 1)[-1]
        assert last.strip() == "" or last.count("/") == 1, (
            f"the last line handed over is a fragment: {last!r}"
        )

    def test_fanout_ai_messages_are_included_too(self):
        msgs = [AIMessage(content="subagent finding: node pressure on node-3")]
        assert "node pressure" in _ev(msgs), "vacuity guard: fan-out text never reaches the reviewer"
