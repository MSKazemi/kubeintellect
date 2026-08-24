"""A capped memory section must not read to the model as the operator's complete state.

`load_memory_context` builds a ~500-token block pinned into the coordinator's SystemMessage.
Every section caps its rows — 8 preferences, 5 failure patterns, 3 session notes, 3 past RCAs.
The cap is correct; the silence about it was not. Measured 2026-08-24 against the pre-fix code:
an operator with 12 explicit preferences got 8, and

    NEVER drain node-07, it hosts the license server

was one of the four dropped — with no marker, under a header reading "(remembered)". The only
honest reading of that block is "these are the preferences", so the model infers the instruction
does not exist. This is the sibling of `test_partial_memory_is_not_empty_memory.py`: that file
proves a *missing* section does not read as an *empty* one; this one proves a *capped* section
does not read as a *complete* one.

Each loader now asks for `cap + 1` rows and renders `cap`. The extra row coming back is proof
more exist; it not coming back is proof they do not. One query, and the notice therefore never
claims a count nobody measured.
"""
from __future__ import annotations

import re

import pytest

from app.db import memory_store as M

_LONG_FIX = (
    "kubectl -n payments set resources deployment/api --limits=memory=2Gi,cpu=1 "
    "&& kubectl -n payments rollout status deployment/api --timeout=300s "
    "&& kubectl -n payments annotate deployment/api change-cause='raise mem'"
)


class FakeConn:
    """Applies whatever LIMIT the loader's own SQL asks for — a faithful stand-in for Postgres.

    Deliberately reads the limit out of the query rather than taking it as a parameter: a change
    that lowers a `LIMIT` back to the cap must show up here as a lost notice, not be masked.
    """

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.limits: list[int] = []

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        m = re.search(r"LIMIT (\d+)", sql)
        assert m, f"a capped loader issued a query with no LIMIT: {sql[:80]!r}"
        self.limits.append(int(m.group(1)))
        return self.rows[: int(m.group(1))]


def _prefs(n: int) -> list[dict]:
    return [
        {"key": f"rule-{i:02d}", "value": f"preference number {i}", "source": "explicit",
         "confidence": 1.0}
        for i in range(1, n + 1)
    ]


def _patterns(n: int) -> list[dict]:
    return [
        {"pattern_name": f"P{i}", "description": f"desc {i}", "recommended_fix": f"fix {i}",
         "occurrence_count": 10 - i}
        for i in range(1, n + 1)
    ]


def _notes(n: int) -> list[dict]:
    return [{"note": f"note {i}"} for i in range(1, n + 1)]


def _rcas(n: int, fix: str = "kubectl rollout restart deployment/api") -> list[dict]:
    return [
        {"root_cause": f"cause {i}", "recommended_fix": fix, "namespace": "payments",
         "date": "2026-08-24", "verified_resolved": True}
        for i in range(1, n + 1)
    ]


class TestTheOperatorsDroppedPreferencesAreDeclared:
    """The measured case, and its vacuity guard."""

    @pytest.mark.asyncio
    async def test_more_preferences_than_the_cap_produce_a_notice(self):
        out = await M._load_user_prefs(FakeConn(_prefs(12)), "u1")
        assert "MORE operator preferences are stored" in out[0], (
            "8 of 12 preferences reached the model with nothing saying so")

    @pytest.mark.asyncio
    async def test_exactly_the_cap_produces_no_notice(self):
        out = await M._load_user_prefs(FakeConn(_prefs(8)), "u1")
        assert "MORE" not in out[0], (
            f"a complete list was labelled incomplete, which is its own false statement: {out[0]!r}")

    @pytest.mark.asyncio
    async def test_one_past_the_cap_is_the_boundary_that_must_fire(self):
        """cap+1 is exactly the row the probe exists to see."""
        out = await M._load_user_prefs(FakeConn(_prefs(9)), "u1")
        assert "MORE" in out[0], "the +1 probe row did not register as evidence of more"

    @pytest.mark.asyncio
    async def test_fewer_than_the_cap_produces_no_notice(self):
        out = await M._load_user_prefs(FakeConn(_prefs(3)), "u1")
        assert "MORE" not in out[0]

    @pytest.mark.asyncio
    async def test_the_probe_row_is_never_rendered(self):
        """Asking for 9 must still show 8 — the extra row is evidence, not content."""
        out = await M._load_user_prefs(FakeConn(_prefs(12)), "u1")
        shown = [ln for ln in out[0].split("\n") if re.match(r"\s+rule-\d+:", ln)]
        assert len(shown) == 8, f"the budget was blown by the probe row: {len(shown)} rendered"

    @pytest.mark.asyncio
    async def test_the_capped_rows_are_still_the_top_ranked_ones(self):
        out = await M._load_user_prefs(FakeConn(_prefs(12)), "u1")
        assert "rule-01" in out[0] and "rule-09" not in out[0], (
            "the cap dropped the wrong end of the ranking")


class TestTheNoticeSaysOnlyWhatWasMeasured:
    """Rule 7 reaches prompt text: the probe proves 'more exist', never how many."""

    @pytest.mark.asyncio
    async def test_the_notice_claims_no_count(self):
        out = await M._load_user_prefs(FakeConn(_prefs(40)), "u1")
        notice = [ln for ln in out[0].split("\n") if "MORE" in ln][0]
        assert not re.search(r"\b\d+\s+(more|other|remaining)", notice), (
            f"the notice states a count the single LIMIT query never measured: {notice!r}")

    @pytest.mark.asyncio
    async def test_the_notice_names_the_inference_it_is_blocking(self):
        out = await M._load_user_prefs(FakeConn(_prefs(12)), "u1")
        notice = [ln for ln in out[0].split("\n") if "MORE" in ln][0]
        assert "NOT evidence" in notice, (
            f"the notice reports a fact but not what the model must not conclude: {notice!r}")


class TestEverySectionThatCapsSaysSo:
    @pytest.mark.asyncio
    async def test_failure_patterns(self):
        assert "MORE known failure patterns" in (
            await M._load_failure_hints(FakeConn(_patterns(6))))[0]

    @pytest.mark.asyncio
    async def test_failure_patterns_at_the_cap_are_quiet(self):
        assert "MORE" not in (await M._load_failure_hints(FakeConn(_patterns(5))))[0]

    @pytest.mark.asyncio
    async def test_session_notes(self):
        assert "MORE notes from this session" in (
            await M._load_session_notes(FakeConn(_notes(4)), "s1"))[0]

    @pytest.mark.asyncio
    async def test_session_notes_at_the_cap_are_quiet(self):
        assert "MORE" not in (await M._load_session_notes(FakeConn(_notes(3)), "s1"))[0]

    @pytest.mark.asyncio
    async def test_past_rca(self):
        assert "MORE past RCA outcomes" in (await M._load_past_rca(FakeConn(_rcas(4)), "u1"))[0]

    @pytest.mark.asyncio
    async def test_past_rca_at_the_cap_is_quiet(self):
        assert "MORE" not in (await M._load_past_rca(FakeConn(_rcas(3)), "u1"))[0]

    @pytest.mark.asyncio
    async def test_no_section_is_silently_left_out_of_this_test(self):
        """A new capped loader must fail here rather than ship without a notice."""
        import inspect
        src = inspect.getsource(M)
        capped = re.findall(r"async def (_load_\w+)\(", src)
        covered = {"_load_user_prefs", "_load_failure_hints", "_load_session_notes",
                   "_load_past_rca"}
        assert set(capped) == covered, (
            f"memory_store has loaders this file does not cover: {set(capped) - covered}")


class TestATruncatedRemediationSaysItIsTruncated:
    """A fix cut mid-command reads as a complete command."""

    @pytest.mark.asyncio
    async def test_a_long_fix_is_marked(self):
        out = (await M._load_past_rca(FakeConn(_rcas(1, _LONG_FIX)), "u1"))[0]
        assert "[fix truncated" in out, (
            f"a remediation was cut at 160 chars with nothing saying so: {out!r}")

    @pytest.mark.asyncio
    async def test_the_cut_really_did_lose_a_command(self):
        """Non-vacuity: the marker is only meaningful because content was actually lost."""
        out = (await M._load_past_rca(FakeConn(_rcas(1, _LONG_FIX)), "u1"))[0]
        assert "change-cause" not in out, "the fixture no longer exceeds the 160-char cut"

    @pytest.mark.asyncio
    async def test_a_short_fix_is_not_marked(self):
        out = (await M._load_past_rca(FakeConn(_rcas(1)), "u1"))[0]
        assert "truncated" not in out, "a complete command was labelled truncated"

    @pytest.mark.asyncio
    async def test_a_fix_of_exactly_the_cut_length_is_not_marked(self):
        out = (await M._load_past_rca(FakeConn(_rcas(1, "x" * 160)), "u1"))[0]
        assert "truncated" not in out, "an off-by-one marked an untruncated fix"

    @pytest.mark.asyncio
    async def test_a_missing_fix_does_not_crash_or_get_marked(self):
        rows = _rcas(1)
        rows[0]["recommended_fix"] = None
        out = (await M._load_past_rca(FakeConn(rows), "u1"))[0]
        assert "truncated" not in out


class TestTheQueriesActuallyProbeOnePastTheCap:
    """The notices are only as good as the extra row. Guard the mechanism, not just the output."""

    @pytest.mark.asyncio
    async def test_each_loader_asks_for_one_more_row_than_it_renders(self):
        conns = {"prefs": FakeConn(_prefs(20)), "hints": FakeConn(_patterns(20)),
                 "notes": FakeConn(_notes(20)), "rca": FakeConn(_rcas(20))}
        await M._load_user_prefs(conns["prefs"], "u1")
        await M._load_failure_hints(conns["hints"])
        await M._load_session_notes(conns["notes"], "s1")
        await M._load_past_rca(conns["rca"], "u1")
        for key, want in {"prefs": 9, "hints": 6, "notes": 4, "rca": 4}.items():
            assert conns[key].limits == [want], (
                f"{key} queried LIMIT {conns[key].limits} — a LIMIT equal to the rendered cap "
                f"makes 'more exist' undetectable and the notice can never fire")


class TestSplitCapped:
    def test_it_reports_more_only_when_the_probe_row_arrived(self):
        assert M._split_capped([1, 2, 3], 3) == ([1, 2, 3], False)
        assert M._split_capped([1, 2, 3, 4], 3) == ([1, 2, 3], True)
        assert M._split_capped([], 3) == ([], False)

    def test_it_never_returns_the_probe_row_as_content(self):
        shown, more = M._split_capped(list(range(50)), 8)
        assert len(shown) == 8 and more is True
