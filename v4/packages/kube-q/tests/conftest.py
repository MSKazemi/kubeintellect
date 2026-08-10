"""Shared pytest fixtures for kube-q.

Pin terminal rendering env for the whole suite. Rich/console output otherwise
depends on the developer's COLUMNS, TERM, and NO_COLOR, which makes unrelated
tests fail on narrow or dumb terminals (see #106).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _pin_terminal_rendering_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
