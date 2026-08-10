"""Shared pytest configuration for the kube-q CLI suite."""

from __future__ import annotations

import os

import pytest

_PINNED_TERMINAL_ENV = {
    "COLUMNS": "120",
    "TERM": "xterm-256color",
}
_ORIGINAL_TERMINAL_ENV: dict[str, str | None] = {}


def _pin_terminal_environment() -> None:
    for key, value in _PINNED_TERMINAL_ENV.items():
        os.environ[key] = value
    os.environ.pop("NO_COLOR", None)


def pytest_configure() -> None:
    """Pin Rich before test modules create consoles during collection."""
    for key in (*_PINNED_TERMINAL_ENV, "NO_COLOR"):
        _ORIGINAL_TERMINAL_ENV[key] = os.environ.get(key)
    _pin_terminal_environment()


def pytest_unconfigure() -> None:
    """Restore the invoking process environment after the suite."""
    for key, value in _ORIGINAL_TERMINAL_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def _stable_terminal_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep Rich output deterministic across contributor terminals without weakening assertions.
    for key, value in _PINNED_TERMINAL_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("NO_COLOR", raising=False)
