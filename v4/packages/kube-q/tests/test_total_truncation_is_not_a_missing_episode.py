"""The one case where every record is gone must not exit like a typo.

`GET /v1/episodes/{id}/replay` distinguishes two empty results on purpose. If the chain
anchor proves records were written and none survive, it answers **409** with

    episode 'X' has no surviving records but its chain anchor says it had some — every
    record has been removed. This is NOT the same as the episode never existing.

and its own comment calls that "the one wrong answer this endpoint must not give: it
launders a total truncation into an absence". That is the strongest tamper signal the system
can produce.

Measured 2026-08-24, before this file, with a stubbed server:

    HTTP 409 (every record removed)  -> exit 1
    HTTP 404 (no such episode)       -> exit 1
    HTTP 500 (server error)          -> exit 1

So the documented tamper branch — `kq replay X; [ $? -eq 3 ]` — could not fire on total
truncation, and 409 was indistinguishable from a mistyped episode id.

The second half of the same defect, found while reproducing the first: the 409 output was

    Replay failed: Client error '409 Conflict' for url http://…/replay

`explain()` exists to print "the server's own words for a failure, not just the status
line", and it does that by reading `detail` out of the response body. This request is
**streamed**, so the body was never read, `response.json()` raised, and it fell back to
httpx's status line — discarding every server explanation on this path. The 503 branch
already called `response.read()` first; nothing else did.
"""
from __future__ import annotations

import os

import pytest
import respx
from httpx import Response

from kube_q.cli import replay_cmd

URL = "http://test-server/v1/episodes/ep-1/replay"

TRUNCATED = (
    "episode 'ep-1' has no surviving records but its chain anchor says it had some — "
    "every record has been removed. This is NOT the same as the episode never existing."
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    monkeypatch.setenv("COLUMNS", "400")


def _run(status: int, detail: str, capsys) -> tuple[int, str]:
    with respx.mock:
        respx.get(URL).mock(return_value=Response(status, json={"detail": detail}))
        rc = replay_cmd.run(["ep-1"])
    return rc, capsys.readouterr().out


class TestTruncationExitsAsTampering:
    def test_409_exits_3_not_1(self, capsys):
        rc, _ = _run(409, TRUNCATED, capsys)
        assert rc == 3, (
            "total truncation exited like a missing episode, so the documented tamper "
            "branch could not fire on the one case where every record is gone")

    def test_the_operator_is_told_the_records_were_removed(self, capsys):
        _, out = _run(409, TRUNCATED, capsys)
        assert "CHAIN BROKEN" in out
        assert "every record has been removed" in out

    def test_the_servers_own_sentence_survives(self, capsys):
        """The distinction is the server's to make, and it wrote it down."""
        _, out = _run(409, TRUNCATED, capsys)
        assert "NOT the same as the episode never existing" in out, (
            "the server's explanation was replaced by httpx's status line")
        assert "Client error" not in out, "the raw transport message leaked instead"

    def test_the_client_says_why_this_is_not_an_absence(self, capsys):
        """Distinct from the assertion above: that one quotes the SERVER's sentence.

        This command is also read by people who never see the server's wording (a proxy that
        rewrites `detail`, an older server), so the reason a 409 is not a missing episode has
        to be in the client's own output too — the chain anchor proves records were written.
        """
        _, out = _run(409, "opaque proxy message", capsys)
        assert "chain anchor" in out, (
            "the client printed a verdict without saying what makes it a verdict")
        assert "audit trail as compromised" in out

    def test_a_missing_episode_is_still_not_tampering(self, capsys):
        rc, out = _run(404, "no recorded episode 'ep-1'", capsys)
        assert rc == 1, "a genuinely absent episode must not be reported as a broken chain"
        assert "CHAIN BROKEN" not in out


class TestTheStreamedBodyIsReadBeforeItIsExplained:
    """`explain()` can only quote a body that was read. On a stream, nothing reads it."""

    @pytest.mark.parametrize("status", [500, 502, 400, 403])
    def test_the_server_detail_reaches_the_operator(self, status, capsys):
        rc, out = _run(status, "the recorder pool is exhausted", capsys)
        assert rc == 1
        assert "the recorder pool is exhausted" in out, (
            f"HTTP {status}: the server's detail was discarded because the streamed body "
            "was never read")
        assert f"HTTP {status}" in out, "the status code is still worth printing"

    def test_a_non_json_error_body_still_says_something(self, capsys):
        with respx.mock:
            respx.get(URL).mock(return_value=Response(502, text="<html>bad gateway</html>"))
            rc = replay_cmd.run(["ep-1"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "Replay failed" in out, "an unparseable error body printed nothing at all"

    def test_503_keeps_its_own_wording(self, capsys):
        """The one branch that already read the body must not be changed by this fix."""
        rc, out = _run(503, "flight recorder database unreachable", capsys)
        assert rc == 1
        assert "decision log is unavailable" in out
        assert "flight recorder database unreachable" in out
        assert "NOT a statement" in out, (
            "the 503 branch's careful 'this is not an absence claim' wording was lost")


class TestTheHappyPathIsUntouched:
    def test_an_intact_episode_still_exits_0(self, capsys):
        body = (
            'data: {"type": "replay_meta", "episode_id": "ep-1", "records": 1, '
            '"chain_valid": true}\n\n'
            'data: {"type": "status", "message": "started"}\n\n'
            "data: [DONE]\n\n"
        )
        with respx.mock:
            respx.get(URL).mock(return_value=Response(200, text=body))
            rc = replay_cmd.run(["ep-1"])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "chain intact" in out

    def test_a_broken_chain_still_exits_3_after_rendering(self, capsys):
        """409 and a false `chain_valid` are two routes to the same verdict."""
        body = (
            'data: {"type": "replay_meta", "episode_id": "ep-1", "records": 1, '
            '"chain_valid": false}\n\n'
            'data: {"type": "status", "message": "started"}\n\n'
            "data: [DONE]\n\n"
        )
        with respx.mock:
            respx.get(URL).mock(return_value=Response(200, text=body))
            rc = replay_cmd.run(["ep-1"])
        out = capsys.readouterr().out
        assert rc == 3, out
        assert "CHAIN BROKEN" in out
        assert "started" in out, "the records must still be rendered before the verdict"
