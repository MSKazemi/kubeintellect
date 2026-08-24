"""The coordinator's trimmer dropped 170 of 200 rows without saying so — and deleted the
`[Protected]` notice that `run_kubectl` had just attached.

`_trim_tool_output` shrinks tool output to fit the model's context. It announced the loss only
when what *remained* still exceeded the character cap, which is the uncommon case. Measured
2026-08-24 on a 200-pod `kubectl get pods`:

    real output    12463 chars, 200 pod rows
    model receives  1923 chars,  30 pod rows      <- 170 rows gone, no marker of any kind

`_KUBECTL_KEEP_RE` deliberately retains the *unhealthy* rows, so the ones dropped are the healthy
ones. A model asked "how many pods are Running?" reads a table that looks whole and answers 30.

The worse half is what it did to the layer below. `run_kubectl` ends a filtered listing with
`[Protected] N namespace(s) withheld … This listing is NOT the complete set.` — the guarantee
`docs/security.md` states as *"Every filtered listing says so"*. That sentence is the **last**
line, which is exactly where the row cap cuts, and it matches no "important row" pattern. So the
announcement was built in 2026-08-20, extended to `GET /v1/namespaces` in 2026-08-24, and then
deleted here before the model ever saw it. The guarantee held for `run_kubectl`'s return value
and not for what the agent actually reads.

Third, smaller: the marker it did emit read `[+N chars trimmed from LLM context]`, while
`_COORDINATOR_SYSTEM` — four hundred lines up the same file — tells the model to warn when it
sees `[truncated` or `chars omitted`. An instruction and its trigger in one file, not agreeing
on a string.
"""

from __future__ import annotations

import pytest
from app.agent.nodes.coordinator import (
    _COORDINATOR_SYSTEM,
    _KUBECTL_TABLE_ROWS,
    _LOG_LINES_KEPT,
    _TOOL_OUTPUT_MAX_CHARS,
    _trim_tool_output,
)
from app.tools.namespace_guard import withheld_note

HEADER = "NAME                          READY   STATUS    RESTARTS   AGE\n"


def table(n: int, status: str = "Running") -> str:
    return HEADER + "".join(
        f"web-{i:03d}-abcde                 1/1     {status}   0          3d\n" for i in range(n)
    )


def logs(n: int) -> str:
    return "".join(f"2026-08-24T10:00:{i % 60:02d}Z line {i} of application log\n" for i in range(n))


# ── 1. dropped rows are announced ─────────────────────────────────────────────────────────────


class TestARowThatWasDroppedIsCounted:
    def test_the_200_pod_listing_says_it_is_short(self):
        out = _trim_tool_output(table(200))
        assert "truncated" in out

    def test_it_says_how_many(self):
        out = _trim_tool_output(table(200))
        assert "170 row(s) omitted" in out

    def test_the_count_matches_what_was_actually_kept(self):
        out = _trim_tool_output(table(200))
        kept_rows = sum(1 for ln in out.splitlines() if ln.startswith("web-"))
        assert kept_rows == _KUBECTL_TABLE_ROWS
        assert f"{200 - kept_rows} row(s) omitted" in out

    def test_dropped_log_lines_are_counted_too(self):
        out = _trim_tool_output(logs(500))
        assert f"{500 - _LOG_LINES_KEPT} line(s) omitted" in out

    def test_a_mid_line_cut_is_a_separate_count(self):
        """"I see 30 of 200 rows" and "my last row is cut in half" are different losses."""
        out = _trim_tool_output(logs(500))
        assert "line(s) omitted" in out and "chars omitted" in out


# ── 2. the tool's own notice survives ─────────────────────────────────────────────────────────


class TestItNoLongerDeletesTheLayerBelow:
    def test_the_withheld_note_survives_the_trim(self):
        listing = ("NAME              STATUS   AGE\n"
                   + "".join(f"ns-{i:03d}           Active   3d\n" for i in range(120))
                   + withheld_note(3, "namespace"))
        out = _trim_tool_output(listing)
        assert "withheld" in out, "the trimmer deleted a security announcement"

    def test_it_is_still_the_last_thing_the_model_reads(self):
        listing = ("NAME              STATUS   AGE\n"
                   + "".join(f"ns-{i:03d}           Active   3d\n" for i in range(120))
                   + withheld_note(3, "namespace"))
        assert "[Protected]" in _trim_tool_output(listing).splitlines()[-1]

    def test_a_protected_refusal_is_never_trimmed_away(self):
        blob = "[Protected] Access to namespace 'kube-system' is not permitted.\n" + "x\n" * 3000
        assert "[Protected]" in _trim_tool_output(blob)

    def test_a_tools_own_truncation_marker_survives_too(self):
        """`run_kubectl` caps its output and appends `[truncated: N chars omitted …]`. That line
        carries no `[Protected]`, sits last, and is not an "important row" — so the row cap ate
        it, and a doubly-shortened output reached the model announcing neither loss."""
        listing = (HEADER
                   + "".join(f"web-{i:03d}   1/1   Running   0   3d\n" for i in range(120))
                   + "\n[truncated: 4211 chars omitted — output was cut short.]")
        out = _trim_tool_output(listing)
        assert "4211 chars omitted" in out

    def test_the_notice_is_not_counted_as_a_row(self):
        """Counting the notice as a dropped row would report 121 of 120."""
        listing = ("NAME              STATUS   AGE\n"
                   + "".join(f"ns-{i:03d}           Active   3d\n" for i in range(120))
                   + withheld_note(3, "namespace"))
        out = _trim_tool_output(listing)
        assert f"{120 - _KUBECTL_TABLE_ROWS} row(s) omitted" in out


# ── 3. the instruction and the marker agree ───────────────────────────────────────────────────


class TestTheModelIsToldToLookForWhatIsActuallyEmitted:
    @pytest.mark.parametrize("content", [table(200), logs(500), "x\n" * 5000])
    def test_every_marker_matches_a_pattern_the_prompt_names(self, content):
        out = _trim_tool_output(content)
        markers = [ln for ln in out.splitlines() if "omitted from LLM context" in ln]
        assert markers, "nothing announced the loss at all"
        for m in markers:
            assert "[truncated" in m or "chars omitted" in m

    def test_the_prompt_still_names_those_patterns(self):
        """Vacuity guard, and the other direction of the same drift: if the instruction is
        reworded, these markers stop being the thing the model was told to look for."""
        assert "[truncated" in _COORDINATOR_SYSTEM
        assert "chars omitted" in _COORDINATOR_SYSTEM


# ── 4. nothing else changed ───────────────────────────────────────────────────────────────────


class TestItStillTrims:
    def test_short_output_is_returned_untouched(self):
        short = "NAME   READY\napi    1/1\n"
        assert _trim_tool_output(short) == short

    def test_output_at_the_cap_is_untouched(self):
        exact = "a" * _TOOL_OUTPUT_MAX_CHARS
        assert _trim_tool_output(exact) == exact

    def test_a_many_row_table_under_the_cap_is_left_whole(self):
        """The character budget is the trigger, not the row count. Dropping 70 of 100 rows from
        an output that already fits would be loss for nothing — and would then be announced,
        which is worse: a correct listing labelled incomplete."""
        small = HEADER + "".join(f"w-{i:03d} 1/1 Running 0 3d\n" for i in range(80))
        assert len(small) <= _TOOL_OUTPUT_MAX_CHARS
        assert 80 > _KUBECTL_TABLE_ROWS, "the row cap must be the thing not applied"
        assert _trim_tool_output(small) == small

    def test_unhealthy_rows_are_still_kept_over_healthy_ones(self):
        mixed = HEADER + "".join(
            (f"bad-{i:03d}   0/1   CrashLoopBackOff  9   3d\n" if i % 50 == 0
             else f"web-{i:03d}   1/1   Running   0   3d\n")
            for i in range(200)
        )
        out = _trim_tool_output(mixed)
        assert out.count("CrashLoopBackOff") == 4

    def test_nothing_dropped_means_nothing_claimed(self):
        """A long output with few rows is cut by chars only — it must not report rows omitted."""
        out = _trim_tool_output(HEADER + "web-000   1/1   Running   0   3d " + "x" * 4000 + "\n")
        assert "row(s) omitted" not in out

    def test_the_result_still_fits_the_budget(self):
        out = _trim_tool_output(table(500))
        body = [ln for ln in out.splitlines() if "omitted from LLM context" not in ln]
        assert len("\n".join(body)) <= _TOOL_OUTPUT_MAX_CHARS + len(HEADER)
