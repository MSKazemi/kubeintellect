r"""When the server explains a failure, `kq` must print the explanation.

The server does not answer errors with bare status codes. `GET /v1/detectors` returns **503**
with "no memory pool — the detector store is not configured" rather than an empty 200, so an
operator cannot mistake *unqueryable* for *unmonitored*; `POST /v1/detectors/{name}/promote`
returns **409** naming the reason a detector can never fire. Every `kq` command called
`raise_for_status()` and printed the resulting exception, whose message is the status line plus
a link to MDN, so none of that reached the terminal:

    Detector command failed: Client error '409 Conflict' for url '…/promote'
    For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/409

`DetectorStoreUnavailable` was written because "the server logged a warning nobody reading the
CLI would ever see". This is that same failure one layer further out, and these tests drive the
real command against a real socket rather than asserting on a mock.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kube_q.core.transport import explain

_DETAIL_409 = ("detector 'nl:dead' can never fire: reason_regex '^(A | B)$' can only be "
               "satisfied by 'A ', which is not a legal Kubernetes reason")
_DETAIL_503 = "no memory pool — the detector store is not configured"


class _Handler(BaseHTTPRequestHandler):
    cases: dict = {}

    def _reply(self):
        code, body = self.cases.get(self.path.split("?")[0], (404, {"detail": "no such route"}))
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _reply

    def log_message(self, *_a):
        pass


@pytest.fixture
def server(monkeypatch):
    """A real loopback server the real CLI talks to."""
    def _start(cases):
        _Handler.cases = cases
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        monkeypatch.setenv("KUBE_Q_URL", f"http://127.0.0.1:{srv.server_address[1]}")
        monkeypatch.setenv("KUBE_Q_API_KEY", "test-key")
        return srv
    started: list = []
    yield lambda cases: started.append(_start(cases)) or started[-1]
    for srv in started:
        srv.shutdown()


def test_a_409_reaches_the_operator_with_its_reason(server, capsys):
    server({"/v1/detectors/nl:dead/promote": (409, {"detail": _DETAIL_409})})
    from kube_q.cli import detector_cmd

    # 3, not 1, since 2026-08-24: this file decides that the server's *reason* must reach the
    # terminal, and the exit code was only whatever it happened to be when that was written.
    # A 409 here means the detector can never fire — never worth a retry, so it must not share
    # a code with "the store is down". See test_a_dead_detector_is_not_a_failed_request.py.
    assert detector_cmd.run(["promote", "nl:dead"]) == 3
    out = capsys.readouterr().out
    assert "can never fire" in out.replace("\n", " ")
    assert "developer.mozilla.org" not in out


def test_a_503_says_why_the_store_could_not_be_read(server, capsys):
    server({"/v1/detectors": (503, {"detail": _DETAIL_503})})
    from kube_q.cli import detector_cmd

    assert detector_cmd.run(["list"]) == 1
    out = capsys.readouterr().out.replace("\n", " ")
    assert "detector store is not configured" in out


def test_findings_also_surfaces_the_reason(server, capsys):
    """One command proving it is a coincidence; the helper is shared, so check a second."""
    server({"/v1/findings": (503, {"detail": "sensorium: disabled"})})
    from kube_q.cli import findings_cmd

    assert findings_cmd.run([]) == 1
    assert "sensorium: disabled" in capsys.readouterr().out.replace("\n", " ")


class TestExplainFallsBackRatherThanSwallowing:
    """A missing explanation must never become a missing error."""

    def test_a_plain_exception_is_unchanged(self):
        exc = RuntimeError("connection reset by peer")
        assert explain(exc) == "connection reset by peer"

    @pytest.mark.parametrize("body", [b"not json at all", b"[]", b'{"other": "field"}',
                                      b'{"detail": ""}', b'{"detail": null}'])
    def test_an_unusable_body_falls_back_to_the_exception(self, body):
        import httpx

        request = httpx.Request("GET", "http://x/v1/detectors")
        response = httpx.Response(503, content=body, request=request)
        exc = httpx.HTTPStatusError("boom", request=request, response=response)
        assert explain(exc) == "boom"

    def test_a_fastapi_validation_list_is_joined(self):
        import httpx

        request = httpx.Request("POST", "http://x/v1/detectors")
        body = json.dumps({"detail": [{"msg": "field required"}, {"msg": "not a string"}]})
        response = httpx.Response(422, content=body.encode(), request=request)
        exc = httpx.HTTPStatusError("boom", request=request, response=response)
        rendered = explain(exc)
        assert "field required" in rendered and "not a string" in rendered

    def test_the_status_code_is_kept_alongside_the_detail(self):
        import httpx

        request = httpx.Request("GET", "http://x/v1/detectors")
        response = httpx.Response(503, content=json.dumps({"detail": "why"}).encode(),
                                  request=request)
        exc = httpx.HTTPStatusError("boom", request=request, response=response)
        assert explain(exc) == "why (HTTP 503)"
