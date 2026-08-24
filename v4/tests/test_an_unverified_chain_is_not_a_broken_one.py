"""A tamper warning must not fire when nothing was checked.

`build_postmortem` has three outcomes, and `chain_valid` is a `bool` that can only carry two:

* the records were read and the hashes agree      → intact
* the records were read and the hashes disagree   → **broken** (the alarm)
* nothing was read at all                         → neither

Until 2026-08-24 the markdown renderer read `chain_valid: false` as the second case, so an
episode with no events *and* a recorder that could not be reached both printed
*"**AUDIT CHAIN BROKEN** — the recorded events may have been altered"*. That is a false
statement about records nobody looked at, and on the unreadable path it also contradicts the
same postmortem's own summary line, which says the recorder could not be read.

Why it matters more than the wording: a tamper alarm is only worth printing if it is never
printed when nothing was tampered with. `kq replay` already separates unverified from intact
with its own exit code 4 for exactly this reason — this file applies the same rule one surface
over.
"""
from __future__ import annotations

import pytest

from app.db import flight_recorder as fr
from app.digest.postmortem import build_postmortem, render_markdown

BROKEN = "may have been altered"
INTACT = "verified intact"
UNVERIFIED = "NOT VERIFIED"


@pytest.fixture
def rows(monkeypatch):
    """Drive `fetch_episode` — the one call that decides which of the three states we land in."""
    def _set(value):
        async def _fetch(_episode_id):
            if isinstance(value, Exception):
                raise value
            return value
        monkeypatch.setattr(fr, "fetch_episode", _fetch)
    return _set


class _NullPool:
    """A reachable store that holds no row for this episode.

    These tests drive `fetch_episode` directly, so what the pool answers only matters for the
    two lookups the postmortem still makes for itself: the L1 episode meta, and (since
    2026-08-24) the chain anchor. Both want the same answer here — "asked, and there is
    nothing" — which is *not* what `_pool = None` says. `_pool = None` means the recorder was
    never started, so the anchor is unreadable and truncation undetectable; `head_verdict`
    now reports that as unverified, and it would suppress the very banners this file pins.
    """

    async def fetchrow(self, _sql, *_args):
        return None


@pytest.fixture(autouse=True)
def _no_meta_lookup(monkeypatch):
    monkeypatch.setattr(fr, "_pool", _NullPool(), raising=False)


def _row(seq: int, kind: str = "tool_call") -> dict:
    return {"seq": seq, "kind": kind, "payload": {"tool": "kubectl"}, "created_at": 0.0}


async def _md(episode_id: str = "ep") -> str:
    return render_markdown(await build_postmortem(episode_id))


# ── the two false alarms ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_unreadable_recorder_is_not_a_tamper_alarm(rows):
    rows(fr.RecorderUnavailable("connection refused to postgres:5432"))
    md = await _md()
    assert BROKEN not in md
    assert UNVERIFIED in md
    # the reason is still there — "not verified" without a cause is its own dead end
    assert "postgres:5432" in md


@pytest.mark.asyncio
async def test_an_episode_with_no_events_is_not_a_tamper_alarm(rows):
    rows([])
    md = await _md()
    assert BROKEN not in md
    assert UNVERIFIED in md
    assert "No recorded events" in md


@pytest.mark.asyncio
async def test_the_banner_does_not_contradict_the_summary(rows):
    """The unreadable path already said so in prose; the banner above it said the opposite."""
    rows(fr.RecorderUnavailable("connection refused"))
    md = await _md()
    assert "could not be read" in md
    assert INTACT not in md and BROKEN not in md


# ── the alarm still fires when it should ───────────────────────────────────────

@pytest.mark.asyncio
async def test_a_genuinely_broken_chain_still_says_broken(rows, monkeypatch):
    """Vacuity guard: a renderer that never prints the alarm passes every test above."""
    rows([_row(1), _row(2)])
    monkeypatch.setattr(fr, "verify_chain", lambda _rows: False)
    md = await _md()
    assert BROKEN in md
    assert UNVERIFIED not in md


@pytest.mark.asyncio
async def test_a_verified_chain_still_says_intact(rows, monkeypatch):
    """The other direction: 'not verified' must not swallow the good news either."""
    rows([_row(1), _row(2)])
    monkeypatch.setattr(fr, "verify_chain", lambda _rows: True)
    md = await _md()
    assert INTACT in md
    assert UNVERIFIED not in md and BROKEN not in md


@pytest.mark.asyncio
async def test_the_three_banners_are_mutually_exclusive(rows, monkeypatch):
    """Whatever the state, exactly one chain banner appears."""
    cases = [
        (fr.RecorderUnavailable("x"), None),
        ([], None),
        ([_row(1)], True),
        ([_row(1)], False),
    ]
    for value, verdict in cases:
        rows(value)
        if verdict is not None:
            monkeypatch.setattr(fr, "verify_chain", lambda _rows, v=verdict: v)
        md = await _md()
        hits = sum(marker in md for marker in (INTACT, BROKEN, UNVERIFIED))
        assert hits == 1, f"{value!r} produced {hits} banners:\n{md}"


# ── the field behind it ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chain_verified_is_false_only_when_nothing_was_read(rows, monkeypatch):
    rows(fr.RecorderUnavailable("x"))
    assert (await build_postmortem("ep"))["chain_verified"] is False
    rows([])
    assert (await build_postmortem("ep"))["chain_verified"] is False
    rows([_row(1)])
    monkeypatch.setattr(fr, "verify_chain", lambda _rows: False)
    pm = await build_postmortem("ep")
    assert pm["chain_verified"] is True and pm["chain_valid"] is False


@pytest.mark.asyncio
async def test_chain_valid_keeps_its_existing_contract(rows):
    """`chain_valid` stays a plain bool and stays `false` on the unreadable path — clients and
    `tests/test_an_unreadable_log_is_not_an_empty_one.py` already depend on that."""
    rows(fr.RecorderUnavailable("x"))
    pm = await build_postmortem("ep")
    assert pm["chain_valid"] is False
    assert pm["recorder_available"] is False


def test_an_old_postmortem_dict_still_renders_the_two_way_banner():
    """`render_markdown` is called on dicts built elsewhere (tests, and any caller holding a
    stored postmortem). Without the `.get` default those would lose their banner entirely."""
    legacy = {"episode_id": "ep", "chain_valid": True, "timeline": [], "summary": "s"}
    assert INTACT in render_markdown(legacy)
    legacy["chain_valid"] = False
    assert BROKEN in render_markdown(legacy)


# ── the incompleteness banner is unaffected ────────────────────────────────────

@pytest.mark.asyncio
async def test_a_lost_event_still_reports_the_record_as_incomplete(rows, monkeypatch):
    """Intact, complete and verified are three separate claims; fixing the third must not
    silence the second."""
    gap = {"seq": 2, "kind": fr.GAP_KIND,
           "payload": {"dropped": 3, "reason": "recorder was not running"},
           "created_at": 0.0}
    rows([_row(1), gap])
    monkeypatch.setattr(fr, "verify_chain", lambda _rows: True)
    md = await _md()
    assert INTACT in md
    assert "RECORD INCOMPLETE" in md and "3 event(s)" in md
