"""Tests for the ADR-101 harness bound wired into the cortex gather node."""

from __future__ import annotations

from app.core.config import settings
from app.cortex.graph import _bound_tool_content


def test_flag_off_is_v4_silent_8k_chop(monkeypatch):
    monkeypatch.setattr(settings, "CORTEX_V5_ENABLED", False)
    monkeypatch.setattr(settings, "KI_V5_HARNESS_FANOUT", True)
    big = "x" * 9000
    out = _bound_tool_content(big)
    assert out == big[:8000]           # byte-identical to v4
    assert "truncated" not in out       # v4 chop is silent


def test_small_content_untouched_either_way(monkeypatch):
    monkeypatch.setattr(settings, "CORTEX_V5_ENABLED", True)
    monkeypatch.setattr(settings, "KI_V5_HARNESS_FANOUT", True)
    small = "line1\nline2\n"
    assert _bound_tool_content(small) == small


def test_flag_on_is_never_silent_and_line_aligned(monkeypatch):
    monkeypatch.setattr(settings, "CORTEX_V5_ENABLED", True)
    monkeypatch.setattr(settings, "KI_V5_HARNESS_FANOUT", True)
    big = ("some log line here\n" * 1000)  # >8000 chars, many line boundaries
    out = _bound_tool_content(big)
    assert "truncated" in out                     # never silent (ADR-101)
    # The marker used to be spelled out here as a literal; when its wording changed on 2026-08-24
    # the `replace` matched nothing, the assertion below became a statement about the marker
    # rather than about the cut, and this test passed while testing nothing. Strip whatever the
    # last line is instead.
    body = "\n".join(out.splitlines()[:-1])
    assert body.endswith("here")                  # cut on a line boundary, not mid-token


def test_flag_on_requires_both_flags(monkeypatch):
    # CORTEX_V5 off but fanout on → still v4 behavior (defense in depth)
    monkeypatch.setattr(settings, "CORTEX_V5_ENABLED", False)
    monkeypatch.setattr(settings, "KI_V5_HARNESS_FANOUT", True)
    big = "y" * 9000
    assert _bound_tool_content(big) == big[:8000]
