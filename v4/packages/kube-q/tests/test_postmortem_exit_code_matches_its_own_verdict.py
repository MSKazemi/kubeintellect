"""`kq postmortem` must not exit 0 while printing AUDIT CHAIN BROKEN.

Three commands render the same flight-recorder verdict. `kq replay` and `kq export` both map it
to the project's documented convention — 0 intact and complete · 1 fetch failed · 2 usage ·
3 chain BROKEN · 4 chain NOT VERIFIED · 5 intact but INCOMPLETE — and `docs/cli-reference.md`
states it twice, once per command, the second saying "same convention as `kq replay`".

`kq postmortem` returned **0** in all of those cases. It could not do better: it asks the server
for `format=markdown`, which used to return `{"markdown": ...}` only, so the verdict arrived as
one of four English banners and nothing the code could branch on. Measured 2026-08-24, this is
what `kq postmortem X > report.md && publish` could not distinguish.
"""
from __future__ import annotations

import inspect

import pytest
from rich.console import Console

from kube_q.cli import postmortem_cmd, replay_cmd

_INTACT = {"markdown": "# Incident postmortem", "chain_verified": True, "chain_valid": True,
           "events_lost": 0, "gaps": []}


def _body(**overrides) -> dict:
    return {**_INTACT, **overrides}


@pytest.fixture
def verdict(capsys):
    def _run(**overrides):
        rc = postmortem_cmd._verdict_exit(_body(**overrides), Console())
        return rc, capsys.readouterr().out
    return _run


class TestEachVerdictGetsItsOwnCode:
    def test_intact_and_complete_is_zero(self, verdict):
        rc, out = verdict()
        assert rc == 0
        assert out.strip() == "", f"the healthy path printed a warning: {out!r}"

    def test_a_broken_chain_is_three(self, verdict):
        rc, out = verdict(chain_valid=False)
        assert rc == 3, "a tampered postmortem reported success"
        assert "CHAIN BROKEN" in out

    def test_an_unverified_chain_is_four(self, verdict):
        rc, out = verdict(chain_verified=False, chain_valid=False)
        assert rc == 4, "an unchecked chain reported success"
        assert "NOT VERIFIED" in out

    def test_unverified_outranks_broken(self, verdict):
        """`chain_valid=False` with `chain_verified=False` means nothing was read — 4, not 3.
        Reporting BROKEN there is a claim about records nobody looked at."""
        rc, out = verdict(chain_verified=False, chain_valid=False)
        assert rc == 4 and "BROKEN" not in out

    def test_intact_but_incomplete_is_five(self, verdict):
        rc, out = verdict(events_lost=3, gaps=[{"reason": "recorder_gap", "dropped": 3}])
        assert rc == 5, "a postmortem missing 3 events reported a clean bill of health"
        assert "INCOMPLETE" in out and "3 event(s)" in out

    def test_a_broken_chain_outranks_incompleteness(self, verdict):
        rc, _ = verdict(chain_valid=False, events_lost=3)
        assert rc == 3, "tampering is the more severe finding and must win"


class TestItFailsClosed:
    def test_a_server_that_sends_no_verdict_is_not_verified(self):
        """An older or proxied server sends prose only. That is the definition of unverified."""
        rc = postmortem_cmd._verdict_exit({"markdown": "# Incident postmortem"}, Console())
        assert rc == 4, (
            "a server that sent no verdict was treated as having sent a good one — the exact "
            "'missing check mistaken for a passed check' this convention exists to prevent")

    def test_events_lost_of_none_is_not_a_crash(self, verdict):
        rc, _ = verdict(events_lost=None)
        assert rc == 0

    def test_a_non_numeric_events_lost_does_not_pass_silently(self):
        with pytest.raises((TypeError, ValueError)):
            postmortem_cmd._verdict_exit(_body(events_lost="three"), Console())


class TestTheReportIsStillPrinted:
    def test_a_non_zero_exit_does_not_cost_the_markdown(self, monkeypatch, capsys):
        """It is still a render command; failing must not withhold what it rendered."""
        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return _body(chain_valid=False)

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, *a, **k): return FakeResp()

        monkeypatch.setattr(postmortem_cmd, "make_client", lambda *a, **k: FakeClient())
        rc = postmortem_cmd.run(["ep-1"])
        out = capsys.readouterr().out
        assert rc == 3
        assert "Incident postmortem" in out and "CHAIN BROKEN" in out


class TestTheConventionIsShared:
    def test_postmortem_uses_the_same_codes_replay_does(self):
        """Structural: if `replay` ever renumbers, this must fail rather than drift apart."""
        replay_src = inspect.getsource(replay_cmd.run)
        pm_src = inspect.getsource(postmortem_cmd._verdict_exit)
        for code, marker in ((3, "chain_valid"), (4, "chain_verified"), (5, "events_lost")):
            assert f"return {code}" in replay_src, (
                f"`kq replay` no longer returns {code}; the shared convention moved and "
                f"postmortem_cmd must be re-checked against it")
            assert f"return {code}" in pm_src, f"postmortem lost its exit {code} ({marker})"

    def test_usage_and_fetch_failure_codes_are_unchanged(self, monkeypatch):
        assert postmortem_cmd.run([]) == 2

        def _boom(*a, **k):
            raise RuntimeError("connection refused")
        monkeypatch.setattr(postmortem_cmd, "make_client", _boom)
        assert postmortem_cmd.run(["ep-1"]) == 1
