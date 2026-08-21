"""`kq -q` must report failure through its exit status.

A one-shot query is the form used in scripts, CI jobs and `watch` loops. Before
this suite existed, every failure path — 401, non-200, invalid JSON, retries
exhausted — printed a red message and exited **0**, so a caller could not tell an
answer from an outage. That is the same failure family as the `--version` bug
that reached PyPI under a green job: the human sees the error, the machine does
not.

Both transports already signal failure uniformly by returning empty text, so
these tests patch at the transport boundary and assert on `SystemExit.code`.
The happy-path and HITL cases are what stop "always exit 1" from passing as a
fix.
"""

from __future__ import annotations

import pytest

import kube_q.cli.main as cli_main

_FAILURE = ("", False, None, None)
_ANSWER = ("the api-server pod is crashlooping on OOMKilled", False, None, None)
_HITL_PENDING = ("", True, "action-123", None)


@pytest.fixture(autouse=True)
def _no_auth_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the CLI on the one-shot path without touching a real backend."""
    monkeypatch.setenv("KUBE_Q_API_KEY", "test-key")
    monkeypatch.setenv("KUBE_Q_URL", "https://kube-q.invalid")


def _run(monkeypatch: pytest.MonkeyPatch, transport_result: tuple) -> int:
    monkeypatch.setattr(
        cli_main, "stream_query", lambda *a, **k: transport_result
    )
    monkeypatch.setattr(
        cli_main, "non_stream_query", lambda *a, **k: transport_result
    )
    monkeypatch.setattr(
        "sys.argv", ["kq", "-q", "why is my api-server pod crashlooping?"]
    )
    try:
        cli_main.main()
    except SystemExit as exc:  # failure path calls sys.exit(1)
        return 0 if exc.code is None else int(exc.code)
    return 0  # a normal return from main() is a successful run


def test_failed_query_exits_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty text from the transport is every failure path — it must not exit 0."""
    assert _run(monkeypatch, _FAILURE) == 1


def test_answered_query_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """The negative control: a real answer must still be a success."""
    assert _run(monkeypatch, _ANSWER) == 0


def test_hitl_pending_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutation paused for approval answered correctly — it is not a failure.

    The server returns no assistant text in this case, so a naive `bool(text)`
    check would report a working approval gate as a broken query.
    """
    assert _run(monkeypatch, _HITL_PENDING) == 0


def test_run_single_query_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seam itself, independent of argument parsing."""
    monkeypatch.setattr(cli_main, "stream_query", lambda *a, **k: _FAILURE)
    assert cli_main.run_single_query("https://kube-q.invalid", "q", True) is False


def test_run_single_query_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_main, "stream_query", lambda *a, **k: _ANSWER)
    assert cli_main.run_single_query("https://kube-q.invalid", "q", True) is True
