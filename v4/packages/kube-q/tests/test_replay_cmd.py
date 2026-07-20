"""Tests for `kq replay <episode_id>` (flight-recorder replay)."""
from __future__ import annotations

import json
import os

import pytest
import respx
from httpx import Response

from kube_q.cli import replay_cmd


@pytest.fixture(autouse=True)
def _clean_kube_q_env(monkeypatch):
    """Earlier test modules can leak KUBE_Q_* values into os.environ via the
    config loader's dotenv pass — wipe them so load_config() sees only ours."""
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)


def _sse(*events: dict) -> str:
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    return body + "data: [DONE]\n\n"


def _meta(valid: bool, records: int) -> dict:
    return {"type": "replay_meta", "episode_id": "ep1", "records": records, "chain_valid": valid}


@respx.mock
def test_replay_renders_events_and_intact_chain(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.get("http://test-server/v1/episodes/ep1/replay").mock(
        return_value=Response(
            200,
            text=_sse(
                _meta(True, 2),
                {"type": "status", "phase": "analyzing", "message": "looking"},
                {"type": "tool_call", "tool": "run_kubectl", "command": "get pods"},
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    exit_code = replay_cmd.run(["ep1"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "chain intact" in out
    assert "tool_call" in out


@respx.mock
def test_replay_broken_chain_exits_3(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.get("http://test-server/v1/episodes/ep1/replay").mock(
        return_value=Response(
            200,
            text=_sse(_meta(False, 1), {"type": "status", "message": "x"}),
            headers={"content-type": "text/event-stream"},
        )
    )
    exit_code = replay_cmd.run(["ep1"])
    assert exit_code == 3
    assert "CHAIN BROKEN" in capsys.readouterr().out


@respx.mock
def test_replay_unknown_episode_exits_1(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.get("http://test-server/v1/episodes/nope/replay").mock(
        return_value=Response(404, json={"detail": "no recorded episode 'nope'"})
    )
    exit_code = replay_cmd.run(["nope"])
    assert exit_code == 1
    assert "No recorded episode" in capsys.readouterr().out


def test_replay_usage_without_args():
    assert replay_cmd.run([]) == 2
    assert replay_cmd.run(["--help"]) == 0
