"""tests/test_a_dead_detector_is_not_a_failed_request.py

`POST /v1/detectors/{name}/promote` answers **409** for one thing, and its own comment says why:
"409, not a cheerful 200. Flipping the row would make this endpoint answer `status: active`
about a detector that can never match anything." That is the failure mode this project actually
shipped once — a stray space in an alternation made a detector a permanent no-op through a fully
green suite — so it is the single most important thing this command can be told.

`kq detector promote` funnelled it into `except Exception` → exit **1**, documented as "the
request failed". Measured 2026-08-24, all four states through one code:

    409 the predicate can never fire -> exit 1   Detector command failed: …
    404 no such detector             -> exit 1   Detector command failed: … (HTTP 404)
    503 store unavailable            -> exit 1   Detector command failed: … (HTTP 503)
    500 server error                 -> exit 1   Detector command failed: … (HTTP 500)

`1` is the code a script retries. A dead detector is never worth retrying, and the command
already owns a code for "understood, refused on the merits, nothing changed": `3`, which `new`
uses for a description that would not compile. 409 now maps there, and 404 stops reading as
though the command itself broke.
"""
from __future__ import annotations

import os

import pytest
import respx
from httpx import Response

from kube_q.cli import detector_cmd

URL = "http://test-server"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KUBE_Q_URL", URL)
    monkeypatch.setenv("COLUMNS", "400")


DEAD = ("detector 'nl:disk' can never fire: the alternation '(a |b)' has a stray space, "
        "so no observation can match it")


def call(sub, status, body, capsys, name="nl:disk"):
    action = "promote" if sub == "promote" else "demote"
    respx.post(f"{URL}/v1/detectors/{name}/{action}").mock(
        return_value=Response(status, json=body))
    rc = detector_cmd.run([sub, name])
    return rc, capsys.readouterr().out


BOTH = pytest.mark.parametrize("sub", ["promote", "reject"])


class TestADeadDetectorGetsItsOwnExitCode:
    @respx.mock
    @BOTH
    def test_a_409_is_exit_three_not_one(self, sub, capsys):
        rc, _ = call(sub, 409, {"detail": DEAD}, capsys)
        assert rc == 3

    @respx.mock
    def test_the_servers_reason_reaches_the_operator(self, capsys):
        _rc, out = call("promote", 409, {"detail": DEAD}, capsys)
        assert "stray space" in out

    @respx.mock
    def test_it_says_nothing_changed(self, capsys):
        _rc, out = call("promote", 409, {"detail": DEAD}, capsys)
        assert "Nothing changed" in out

    @respx.mock
    def test_it_tells_a_script_not_to_retry(self, capsys):
        _rc, out = call("promote", 409, {"detail": DEAD}, capsys)
        assert "retrying will not help" in out.lower()

    @respx.mock
    def test_it_does_not_blame_the_request(self, capsys):
        _rc, out = call("promote", 409, {"detail": DEAD}, capsys)
        assert "Detector command failed" not in out


class TestTheOtherFailuresStayRetryable:
    @respx.mock
    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_a_server_side_failure_is_still_exit_one(self, status, capsys):
        rc, _ = call("promote", status, {"detail": "store unavailable"}, capsys)
        assert rc == 1

    @respx.mock
    def test_a_missing_detector_is_exit_one(self, capsys):
        rc, _ = call("promote", 404, {"detail": "detector 'nl:disk' not found"}, capsys)
        assert rc == 1

    @respx.mock
    def test_a_missing_detector_names_what_is_missing(self, capsys):
        _rc, out = call("promote", 404, {"detail": "x"}, capsys)
        assert "No detector named 'nl:disk'" in out
        assert "Detector command failed" not in out

    @respx.mock
    def test_the_dead_and_the_unavailable_no_longer_share_a_code(self, capsys):
        dead, _ = call("promote", 409, {"detail": DEAD}, capsys)
        respx.clear()
        down, _ = call("promote", 503, {"detail": "store unavailable"}, capsys)
        assert dead != down


class TestSuccessIsUnchanged:
    @respx.mock
    @BOTH
    def test_a_promotion_still_exits_zero(self, sub, capsys):
        rc, out = call(sub, 200, {"name": "nl:disk", "status": "active"}, capsys)
        assert rc == 0
        assert "nl:disk → active" in out

    @respx.mock
    def test_no_arguments_is_still_a_usage_error(self):
        assert detector_cmd.run(["promote"]) == 2


class TestTheDocumentedTableMatchesTheCode:
    def test_the_docstring_no_longer_says_new_only(self):
        assert "`new` only" not in detector_cmd.__doc__

    def test_the_docstring_explains_why_three_is_not_one(self):
        doc = detector_cmd.__doc__
        assert "1 is worth retrying and 3 never is" in doc

    def test_the_cli_reference_says_the_same_thing(self):
        from pathlib import Path
        ref = Path(__file__).resolve().parents[3] / "docs" / "cli-reference.md"
        text = ref.read_text()
        assert "rejected on its merits" in text
        assert "`1` is worth retrying and `3` never is" in text


class TestOneSharedServerDetailReader:
    """`server_detail` replaced two copies; only one of them knew about 422's list form."""

    def test_a_validation_error_list_renders_as_prose(self, capsys):
        from kube_q.core.transport import server_detail

        class R:
            status_code = 422

            @staticmethod
            def json():
                return {"detail": [{"msg": "field required"}, {"msg": "not an int"}]}

        assert server_detail(R()) == "field required; not an int"

    def test_a_non_json_body_is_none_not_a_crash(self):
        from kube_q.core.transport import server_detail

        class R:
            status_code = 500

            @staticmethod
            def json():
                raise ValueError("not json")

        assert server_detail(R()) is None

    def test_an_empty_detail_is_none(self):
        from kube_q.core.transport import server_detail

        class R:
            status_code = 500

            @staticmethod
            def json():
                return {"detail": "   "}

        assert server_detail(R()) is None
