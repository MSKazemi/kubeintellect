"""
Unit tests for kube_q/render.py.

Covers: set_output_plain (flag toggling), _should_use_pager (TTY detection,
        line count threshold), c() ANSI helper (TTY vs non-TTY).
"""

import sys

import pytest

import kube_q.cli.renderer as render_mod
from kube_q.cli.renderer import _should_use_pager, c, set_output_plain

# ── set_output_plain ──────────────────────────────────────────────────────────


def test_set_output_plain_true() -> None:
    set_output_plain(True)
    assert render_mod._plain_output is True
    set_output_plain(False)  # reset


def test_set_output_plain_false() -> None:
    set_output_plain(True)
    set_output_plain(False)
    assert render_mod._plain_output is False


# ── _should_use_pager ─────────────────────────────────────────────────────────


def test_should_use_pager_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    # Non-TTY should never page, regardless of length
    long_text = "line\n" * 200
    assert _should_use_pager(long_text) is False


def test_should_use_pager_tty_short_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(render_mod.console, "height", 30)
    short_text = "line\n" * 5
    assert _should_use_pager(short_text) is False


def test_should_use_pager_tty_long_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(render_mod.console, "height", 30)
    # Threshold = min(30-4, 40) = 26 lines; 100 lines should trigger pager
    long_text = "line\n" * 100
    assert _should_use_pager(long_text) is True


def test_should_use_pager_threshold_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Very small terminal (height=5) — floor is 10, not (5-4)=1."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(render_mod.console, "height", 5)
    # 9 lines is below floor of 10 → no pager
    assert _should_use_pager("line\n" * 9) is False
    # 11 lines is above floor → pager
    assert _should_use_pager("line\n" * 11) is True


# ── c() ANSI helper ───────────────────────────────────────────────────────────


def test_c_non_tty_returns_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert c("hello", render_mod.BOLD) == "hello"


def test_c_tty_wraps_with_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    result = c("hello", render_mod.BOLD)
    assert render_mod.BOLD in result
    assert "hello" in result
    assert render_mod.RESET in result
