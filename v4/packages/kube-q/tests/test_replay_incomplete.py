"""An intact chain is not a complete record — `kq replay` / `kq export` must not conflate them.

The flight recorder is fire-and-forget: during an outage it drops events. It now writes that
loss into the chain as a `recorder_gap` record, so both commands can see it. Before this,
`kq replay` printed "✓ chain intact" and exited 0 over an episode with a hole in it, and
`kq export` wrote a report that read as the whole story.

These drive the real `run()` of each command against a mocked HTTP surface — never a
re-implementation of their logic.
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


def _sse(*events: dict) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


def _meta(valid: bool, records: int) -> dict:
    return {"type": "replay_meta", "episode_id": "ep1", "records": records, "chain_valid": valid}


def _gap(dropped: int = 4, reason: str = "the decision_log table was missing") -> dict:
    return {
        "type": "recorder_gap", "dropped": dropped, "reason": reason,
        "message": f"{dropped} recorded event(s) were LOST at this point — {reason}.",
    }


def _mock_replay(*events: dict) -> None:
    respx.get("http://test-server/v1/episodes/ep1/replay").mock(
        return_value=Response(200, text=_sse(*events),
                              headers={"content-type": "text/event-stream"})
    )


class TestReplay:
    @respx.mock
    def test_a_gap_is_not_reported_as_success(self, capsys):
        _mock_replay(_meta(True, 3), {"type": "status", "message": "a"}, _gap(),
                     {"type": "status", "message": "b"})
        code = replay_cmd.run(["ep1"])
        out = capsys.readouterr().out
        assert code == 5, "an episode with a hole in it exited 0"
        assert "EPISODE INCOMPLETE" in out
        assert "4 event(s) were never written" in out

    @respx.mock
    def test_the_reason_reaches_the_operator(self, capsys):
        _mock_replay(_meta(True, 2), _gap(2, "connection refused"),
                     {"type": "status", "message": "b"})
        replay_cmd.run(["ep1"])
        assert "connection refused" in capsys.readouterr().out

    @respx.mock
    def test_several_gaps_are_totalled(self, capsys):
        _mock_replay(_meta(True, 4), _gap(3), {"type": "status", "message": "a"},
                     _gap(5), {"type": "status", "message": "b"})
        code = replay_cmd.run(["ep1"])
        out = capsys.readouterr().out
        assert code == 5
        assert "8 event(s) were never written across 2 gap(s)" in out

    @respx.mock
    def test_a_broken_chain_still_wins_over_a_gap(self, capsys):
        # Tampering is the more serious verdict; exit 3 must not be masked by exit 5.
        _mock_replay(_meta(False, 2), _gap(), {"type": "status", "message": "a"})
        assert replay_cmd.run(["ep1"]) == 3
        assert "CHAIN BROKEN" in capsys.readouterr().out

    @respx.mock
    def test_a_complete_episode_still_exits_0(self, capsys):
        _mock_replay(_meta(True, 2), {"type": "status", "message": "a"},
                     {"type": "final"})
        assert replay_cmd.run(["ep1"]) == 0
        out = capsys.readouterr().out
        assert "chain intact" in out
        assert "INCOMPLETE" not in out

    @respx.mock
    def test_the_gap_row_is_rendered_in_the_table(self, capsys):
        _mock_replay(_meta(True, 2), _gap(), {"type": "status", "message": "a"})
        replay_cmd.run(["ep1"])
        out = capsys.readouterr().out
        assert "recorder_gap" in out

    def test_the_exit_code_is_documented(self):
        assert "5" in replay_cmd.__doc__
        assert "INCOMPLETE" in replay_cmd.__doc__


class TestExport:
    @staticmethod
    def _mock_report(**extra) -> None:
        report = {"episode_id": "ep1", "chain_valid": True, "events_lost": 0, "gaps": [],
                  "timeline": [{"seq": 0, "kind": "status", "summary": "a"}], **extra}
        respx.get("http://test-server/v1/episodes/ep1/postmortem").mock(
            return_value=Response(200, json=report)
        )

    @respx.mock
    def test_an_incomplete_export_is_not_reported_as_clean(self, capsys):
        self._mock_report(events_lost=6, gaps=[{"seq": 1, "dropped": 6, "reason": "boom"}])
        code = export_cmd.run(["ep1"])
        assert code == 5
        assert "EPISODE INCOMPLETE" in capsys.readouterr().err

    @respx.mock
    def test_a_complete_export_still_exits_0(self, capsys):
        self._mock_report()
        assert export_cmd.run(["ep1"]) == 0

    @respx.mock
    def test_a_broken_chain_still_exits_3(self, capsys):
        self._mock_report(chain_valid=False, events_lost=6)
        assert export_cmd.run(["ep1"]) == 3

    @respx.mock
    def test_an_older_server_without_the_field_is_treated_as_complete(self, capsys):
        # events_lost is absent from a server predating the gap record; it must not crash
        # or invent a warning it has no evidence for.
        report = {"episode_id": "ep1", "chain_valid": True,
                  "timeline": [{"seq": 0, "kind": "status", "summary": "a"}]}
        respx.get("http://test-server/v1/episodes/ep1/postmortem").mock(
            return_value=Response(200, json=report)
        )
        assert export_cmd.run(["ep1"]) == 0

    def test_the_exit_code_is_documented(self):
        assert "INCOMPLETE" in export_cmd.__doc__
