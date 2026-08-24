"""The clients must not restate the server's 503 as "this episode has no records".

The server now answers `503` when the decision log cannot be read at all, and keeps `404` for
an episode that genuinely has none (see the server suite's
`test_an_unreadable_log_is_not_an_empty_one.py`). That distinction is worth nothing if `kq`
collapses it again on the way to the terminal — and both commands were written when only one of
the two answers existed:

  * `kq replay` printed *"No recorded episode 'X'."* for any non-200 it recognised,
  * `kq export` reported *"No recorded events for episode 'X' — nothing exported"* whenever the
    postmortem came back with an empty timeline, which is exactly what an unreadable recorder
    produces.

Both are absence claims about an episode nobody was able to look up.
"""
from __future__ import annotations

import json
import os

import pytest
import respx
from httpx import Response

from kube_q.cli import export_cmd, replay_cmd


@pytest.fixture(autouse=True)
def _clean_kube_q_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")


_UNAVAILABLE = {
    "detail": "the flight recorder is not running — no decision log to read — this is not the "
              "same as episode 'ep1' having no records"
}


class TestReplay:
    @respx.mock
    def test_a_503_is_not_reported_as_a_missing_episode(self, capsys):
        respx.get("http://test-server/v1/episodes/ep1/replay").mock(
            return_value=Response(503, json=_UNAVAILABLE))
        code = replay_cmd.run(["ep1"])
        out = capsys.readouterr().out
        assert "No recorded episode" not in out
        assert "unavailable" in out.lower()
        assert code == 1

    @respx.mock
    def test_it_repeats_the_server_reason(self, capsys):
        respx.get("http://test-server/v1/episodes/ep1/replay").mock(
            return_value=Response(503, json=_UNAVAILABLE))
        replay_cmd.run(["ep1"])
        out = capsys.readouterr().out
        assert "flight recorder is not running" in out

    @respx.mock
    def test_a_404_still_says_no_such_episode(self, capsys):
        """Vacuity guard: the 404 wording must survive, or the fix has only moved the lie."""
        respx.get("http://test-server/v1/episodes/ep1/replay").mock(
            return_value=Response(404, json={"detail": "no recorded episode 'ep1'"}))
        code = replay_cmd.run(["ep1"])
        assert "No recorded episode" in capsys.readouterr().out
        assert code == 1

    @respx.mock
    def test_a_503_without_a_json_body_still_explains(self, capsys):
        """A proxy in front of the server returns text/html, not our detail."""
        respx.get("http://test-server/v1/episodes/ep1/replay").mock(
            return_value=Response(503, text="<html>503 Service Unavailable</html>"))
        code = replay_cmd.run(["ep1"])
        out = capsys.readouterr().out
        assert "unavailable" in out.lower()
        assert "HTTP 503" in out
        assert code == 1


def _postmortem(**over) -> dict:
    pm = {"episode_id": "ep1", "chain_valid": True, "timeline": [], "recorder_available": True}
    pm.update(over)
    return pm


class TestExport:
    @respx.mock
    def test_an_unavailable_recorder_is_not_an_empty_episode(self, capsys):
        respx.get("http://test-server/v1/episodes/ep1/postmortem").mock(
            return_value=Response(200, json=_postmortem(
                recorder_available=False,
                summary="The flight recorder could not be read (pool is None).")))
        code = export_cmd.run(["ep1"])
        err = capsys.readouterr().err
        assert "No recorded events" not in err
        assert "unavailable" in err.lower()
        assert code == 1, "exit 4 means 'this episode has no events' — that is not what happened"

    @respx.mock
    def test_a_genuinely_empty_episode_still_exits_four(self, capsys):
        """Vacuity guard in the other direction — exit 4 is a documented contract."""
        respx.get("http://test-server/v1/episodes/ep1/postmortem").mock(
            return_value=Response(200, json=_postmortem()))
        code = export_cmd.run(["ep1"])
        assert "No recorded events" in capsys.readouterr().err
        assert code == 4

    @respx.mock
    def test_an_older_server_without_the_field_is_unchanged(self, capsys):
        """`recorder_available` is new; a server that predates it must not start failing."""
        pm = _postmortem()
        del pm["recorder_available"]
        respx.get("http://test-server/v1/episodes/ep1/postmortem").mock(
            return_value=Response(200, json=pm))
        assert export_cmd.run(["ep1"]) == 4

    @respx.mock
    def test_a_real_export_is_untouched(self, tmp_path, capsys):
        pm = _postmortem(timeline=[{"seq": 0, "kind": "status", "summary": "x"}])
        respx.get("http://test-server/v1/episodes/ep1/postmortem").mock(
            return_value=Response(200, json=pm))
        out = tmp_path / "report.json"
        assert export_cmd.run(["ep1", "--output", str(out)]) == 0
        assert json.loads(out.read_text())["episode_id"] == "ep1"
