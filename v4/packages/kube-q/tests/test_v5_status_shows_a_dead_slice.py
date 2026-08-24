"""`kq v5-status` must show that an "active" flag is doing nothing.

The server reports `degraded_experimental_flags` — flags whose code *does* read them but whose
subsystem is not running — and keeps them in `active_flags`, because that list is rollout
identity and must not flap when Postgres blips. That deliberate overlap is exactly why the CLI
has to render the second list: without it the operator reads "active" and is wrong, which is the
same failure `set_but_unwired_flags` was added to prevent one level in.
"""
from __future__ import annotations

import os
import re

import pytest
import respx
from httpx import Response

from kube_q.cli import v5_status_cmd

_HEALTHY = {
    "arm": "v4", "version": "2.1.0", "cortex_v5_enabled": False,
    "active_flags": ["MEMORY_HIERARCHY_ENABLED"], "set_but_unwired_flags": [],
    "degraded_experimental_flags": [],
    "memory": {"enabled": True, "state": "ready", "reason": "", "observations_dropped": 0},
    "unenforceable_guard_config": [],
    "kill_switch_engaged": False, "change_freeze": False, "spend_cap_usd": 0.0,
}
_DEAD = {
    **_HEALTHY,
    "degraded_experimental_flags": ["MEMORY_HIERARCHY_ENABLED", "MEMORY_KG_PPR"],
    "memory": {"enabled": False, "state": "unavailable",
               "reason": "connection refused to postgres:5432", "observations_dropped": 41},
}


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


@respx.mock
def test_a_degraded_flag_is_named(capsys):
    rc, out = _run(capsys, _DEAD)
    assert rc == 0
    assert "degraded_experimental_flags" in out
    assert "MEMORY_KG_PPR" in out


@respx.mock
def test_the_reason_travels_with_it(capsys):
    """A list of names with no cause sends the operator back to the server logs.

    The reason is printed **twice** on purpose — once in the degraded row and once in the memory
    row — so each row is readable on its own. Asserting the count is what makes this test fail if
    the degraded row loses its explanation and only the memory row keeps one.
    """
    _, out = _run(capsys, _DEAD)
    assert "unavailable" in out
    assert out.count("postgres:5432") >= 2, out
    assert "the memory hierarchy is unavailable" in out


@respx.mock
def test_the_memory_row_shows_what_was_lost(capsys):
    _, out = _run(capsys, _DEAD)
    assert "41 observation(s) dropped" in out


@respx.mock
def test_the_flag_is_still_listed_as_active(capsys):
    """The overlap is deliberate — identity is not liveness. If the CLI hid it here the two
    surfaces would disagree about what the pod was configured as."""
    _, out = _run(capsys, _DEAD)
    assert "active_flags" in out
    assert out.count("MEMORY_HIERARCHY_ENABLED") >= 2


# ── vacuity guards, both directions ────────────────────────────────────────────

@respx.mock
def test_a_healthy_server_prints_no_degraded_row(capsys):
    """Without this, a renderer that always prints the row passes every assertion above."""
    rc, out = _run(capsys, _HEALTHY)
    assert rc == 0
    assert "degraded_experimental_flags" not in out
    assert "change nothing while it is down" not in out


@respx.mock
def test_a_healthy_server_still_shows_the_memory_row(capsys):
    """An empty degraded list is only evidence if the hierarchy is visibly up — otherwise
    "nothing degraded" and "nothing reporting" look identical."""
    _, out = _run(capsys, _HEALTHY)
    assert "memory" in out and "ready" in out
    # …and it must render as the healthy branch, not the outage one with an empty reason.
    assert "no reason recorded" not in out
    assert "observation(s) dropped" not in out


@respx.mock
def test_an_older_server_without_the_fields_still_renders(capsys):
    """A server predating both fields must not crash the command or grow an empty red row."""
    body = {k: v for k, v in _HEALTHY.items()
            if k not in ("degraded_experimental_flags", "memory")}
    rc, out = _run(capsys, body)
    assert rc == 0
    assert "2.1.0" in out
    assert "degraded_experimental_flags" not in out
    assert "no reason recorded" not in out


# ── the guard that caught this in the first place ─────────────────────────────

def test_every_v5_status_field_is_mentioned_by_this_command():
    """`tests/test_v5_flag_wiring.py` asserts the same thing from the server side and is what
    forced this renderer to exist. Repeat it here so the kq suite alone is enough to catch a
    field the CLI silently drops."""
    from pathlib import Path

    src = Path(v5_status_cmd.__file__).read_text(encoding="utf-8")
    for field in ("arm", "version", "cortex_v5_enabled", "active_flags",
                  "set_but_unwired_flags", "degraded_experimental_flags", "memory",
                  "unenforceable_guard_config", "kill_switch_engaged", "change_freeze",
                  "spend_cap_usd"):
        assert f'"{field}"' in src, f"`kq v5-status` never mentions {field}"
