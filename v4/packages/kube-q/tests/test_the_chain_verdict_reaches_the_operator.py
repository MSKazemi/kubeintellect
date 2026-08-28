"""A `TAMPERED` memory audit chain that only `/healthz` knows about has not been reported.

The server began verifying its memory audit chain on 2026-08-28 — until then nothing in a
running process ever called `verify_memory_chain`, so the tamper-evidence the memory design
rests on was never evaluated. That fix put the verdict under `memory.chain` on `GET /healthz`
and `GET /v1/v5/status`, and stopped there: `/healthz` is a kubelet probe endpoint, and no
human reads one. An operator asking this product whether its memory can be trusted types
`kq v5-status`, and it said nothing.

The rendering is the part that can go wrong quietly, so the claims are specific:

* all five server states reach the screen, including the two that are not faults. A surface
  that appears only when it has bad news cannot be used to confirm anything — a missing row
  would be indistinguishable from an older server that never had the field;
* `off` is not styled as healthy. Hardening off means no chain is written at all, which is a
  reason there is nothing to say, not a clean bill of health;
* `unverified` is not styled as a tamper. It is the state the server type exists to hold, and
  a CLI that collapsed it back into a cross would undo that distinction at the last step;
* the **age** is always shown, because the server reports the last RECORDED verdict and never
  a fresh one, and `stale` is called out in red — a verifier that stopped looks exactly like
  one that keeps agreeing with itself.
"""
from __future__ import annotations

import os
import re

import pytest
import respx
from httpx import Response

from kube_q.cli import v5_status_cmd

_BASE = {
    "arm": "v4", "version": "2.1.0", "cortex_v5_enabled": False,
    "active_flags": [], "set_but_unwired_flags": [], "degraded_experimental_flags": [],
    "unenforceable_guard_config": [],
    "kill_switch_engaged": False, "change_freeze": False, "spend_cap_usd": 0.0,
}


def _body(chain: dict | None, **memory_over) -> dict:
    memory = {"enabled": True, "state": "ready", "reason": "", "observations_dropped": 0}
    memory.update(memory_over)
    if chain is not None:
        memory["chain"] = chain
    return {**_BASE, "memory": memory}


def _chain(state: str, **over) -> dict:
    out = {"state": state, "checks": 1, "checked_at": 1000.0, "age_s": 30.0,
           "valid": state != "TAMPERED", "verified": state not in ("unverified", "off"),
           "stale": False}
    out.update(over)
    return out


@pytest.fixture(autouse=True)
def _clean_kube_q_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")


def _flat(text: str) -> str:
    """rich draws a bordered table and hard-wraps inside the cells."""
    return re.sub(r"\s+", " ", text.replace("│", " ").replace("─", " ")).strip()


def _run(capsys, body: dict) -> tuple[int, str]:
    respx.get("http://test-server/v1/v5/status").mock(return_value=Response(200, json=body))
    rc = v5_status_cmd.run([])
    return rc, _flat(capsys.readouterr().out)


class TestEveryStateReachesTheScreen:
    @respx.mock
    @pytest.mark.parametrize(
        "state", ["intact", "TAMPERED", "unverified", "never-checked", "off"])
    def test_the_state_is_named(self, capsys, state):
        rc, out = _run(capsys, _body(_chain(state)))
        assert rc == 0
        assert "memory_chain" in out
        assert state in out

    @respx.mock
    def test_an_intact_chain_is_still_shown(self, capsys):
        """A row that appears only on bad news cannot confirm anything."""
        _rc, out = _run(capsys, _body(_chain("intact")))
        assert "memory_chain" in out
        assert "nothing contradicted" in out

    @respx.mock
    def test_a_server_without_the_field_renders_no_row(self, capsys):
        """Older server, or hardening never compiled in — say nothing rather than guess."""
        _rc, out = _run(capsys, _body(None))
        assert "memory_chain" not in out

    @respx.mock
    def test_an_unrecognised_state_is_not_silently_dropped(self, capsys):
        """A state this CLI has never heard of is a newer server, not an absence."""
        _rc, out = _run(capsys, _body(_chain("quarantined")))
        assert "memory_chain" in out and "quarantined" in out


class TestTheFourNonIntactStatesReadAsThemselves:
    @respx.mock
    def test_tampered_says_what_to_do_about_it(self, capsys):
        _rc, out = _run(capsys, _body(_chain("TAMPERED")))
        assert "untrusted" in out

    @respx.mock
    def test_unverified_is_explicitly_not_a_tamper(self, capsys):
        _rc, out = _run(capsys, _body(_chain("unverified")))
        assert "NOT a tamper" in out

    @respx.mock
    def test_off_is_not_a_clean_bill_of_health(self, capsys):
        _rc, out = _run(capsys, _body(_chain("off", age_s=None, checks=0)))
        assert "MEMORY_SECURITY_HARDENING is off" in out
        assert "Not a clean bill of health" in out

    @respx.mock
    def test_never_checked_says_nothing_has_asked(self, capsys):
        _rc, out = _run(capsys, _body(_chain("never-checked", age_s=None, checks=0)))
        assert "nothing has asked this chain" in out


class TestTheAgeIsPartOfTheVerdict:
    @respx.mock
    def test_a_recorded_verdict_shows_how_old_it_is(self, capsys):
        _rc, out = _run(capsys, _body(_chain("intact", age_s=42.0)))
        assert "last checked 42s ago" in out

    @respx.mock
    def test_never_checked_says_never_rather_than_zero(self, capsys):
        _rc, out = _run(capsys, _body(_chain("never-checked", age_s=None)))
        assert "never checked" in out
        assert "0s ago" not in out

    @respx.mock
    def test_a_stale_verdict_says_the_verifier_may_have_stopped(self, capsys):
        """`intact` from a verifier that died reads as reassurance. This is the only signal."""
        _rc, out = _run(capsys, _body(_chain("intact", age_s=9000.0, stale=True)))
        assert "STALE" in out and "may have stopped" in out

    @respx.mock
    def test_a_fresh_verdict_is_not_called_stale(self, capsys):
        _rc, out = _run(capsys, _body(_chain("intact")))
        assert "STALE" not in out
