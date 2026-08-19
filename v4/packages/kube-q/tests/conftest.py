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


@pytest.fixture(autouse=True)
def _contain_kube_q_env_leaks() -> object:
    """Restore every ``KUBE_Q_*`` variable after each test.

    `kube_q.core.config._load_dotenv_file` copies `.env` entries straight into
    `os.environ`. `monkeypatch` can only undo what *it* set, so a test that
    points `CONFIG_DIR` at a tmp `.env` and calls `load_config()` leaks that
    file's variables into the real process environment for the rest of the
    session — `test_config.py` leaked `KUBE_Q_URL`, `KUBE_Q_MODEL` and an
    invalid `KUBE_Q_TIMEOUT=-10`, which makes any later `load_config(strict=True)`
    exit 2.

    Nothing downstream read those values, so the suite stayed green while the
    environment was quietly poisoned mid-run — the failure only appears when a
    new test happens to land after `test_config.py` alphabetically. Containing
    it here fixes the class rather than the one instance.
    """
    saved = {k: v for k, v in os.environ.items() if k.startswith("KUBE_Q_")}
    yield
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        if key not in saved:
            del os.environ[key]
    os.environ.update(saved)
