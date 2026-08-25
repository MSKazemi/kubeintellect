"""The access log line must carry the request id the middleware just handed the caller.

`RequestLoggingMiddleware` binds `request_id_var` so "all downstream log lines for this request
carry the same ID" — its own docstring. It did, except for the middleware's own access line: the
`finally` that reset the context var ran *before* that line was emitted, so `POST /path 200 3ms`
logged `[-]` while `X-Request-ID` went back to the caller with a real uuid.

That is the worst possible line to lose. It is the only one that maps a request to its status and
duration, and it is what an operator greps for first. The defect is invisible to every functional
test — the response is correct, the header is correct, the id is even correct in every *other*
module's lines — so it survived until the campaign's Loki data was read directly: 159 of 159
`api.middleware` access lines in a four-minute window read `[-]`.

These tests assert the record, not the response, because the record is where the bug lived.
"""
from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.middleware import RequestLoggingMiddleware
from app.utils.logger import get_logger, request_id_var


class _Capture(logging.Handler):
    """Records the LogRecords, with request_id resolved the way production resolves it."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        # Production injects request_id via a filter on the handler. Do the same here so the
        # test measures the value at emit time — which is exactly what the bug got wrong.
        self.seen.append((record.getMessage(), request_id_var.get("-")))


@pytest.fixture
def app_and_log():
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/thing")
    async def thing():
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    cap = _Capture()
    lg = get_logger("app.api.middleware")
    lg.addHandler(cap)
    prior = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        yield app, cap
    finally:
        lg.removeHandler(cap)
        lg.setLevel(prior)


def _access_lines(cap: _Capture) -> list[tuple[str, str]]:
    return [(m, r) for m, r in cap.seen if " 200 " in m or " 500 " in m or "exception" in m]


def test_the_access_line_carries_the_id_the_caller_was_given(app_and_log):
    app, cap = app_and_log
    with TestClient(app) as client:
        resp = client.get("/thing")

    header_id = resp.headers["X-Request-ID"]
    assert header_id and header_id != "-"

    lines = _access_lines(cap)
    assert lines, "the middleware logged no access line at all"
    for msg, rid in lines:
        assert rid == header_id, (
            f"access line {msg!r} was emitted with request_id={rid!r}, but the caller was told "
            f"{header_id!r} — the context var was reset before the line was logged"
        )


def test_the_caller_supplied_id_is_the_one_that_gets_logged(app_and_log):
    """An operator correlating by their own X-Request-ID must find the access line."""
    app, cap = app_and_log
    mine = "trace-me-0001"
    with TestClient(app) as client:
        resp = client.get("/thing", headers={"X-Request-ID": mine})

    assert resp.headers["X-Request-ID"] == mine
    assert [rid for _, rid in _access_lines(cap)] == [mine]


def test_the_id_survives_to_the_error_line_too(app_and_log):
    app, cap = app_and_log
    mine = "trace-me-0002"
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/boom", headers={"X-Request-ID": mine})

    errs = [(m, r) for m, r in cap.seen if "exception" in m]
    assert errs, "the unhandled exception was not logged"
    assert all(r == mine for _, r in errs)


def test_the_context_var_is_still_reset_when_the_request_is_over(app_and_log):
    """Fixing the ordering must not leak the id into the next request's context."""
    app, _ = app_and_log
    with TestClient(app) as client:
        client.get("/thing", headers={"X-Request-ID": "trace-me-0003"})
    assert request_id_var.get("-") == "-"


def test_the_context_var_is_reset_even_when_the_handler_raises(app_and_log):
    app, _ = app_and_log
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/boom", headers={"X-Request-ID": "trace-me-0004"})
    assert request_id_var.get("-") == "-"


def test_probe_paths_are_still_silent(app_and_log):
    """The noise suppression the ordering fix moved past must still hold."""
    app, cap = app_and_log
    with TestClient(app) as client:
        client.get("/healthz")
    assert not _access_lines(cap), "healthz should not produce an access line"
