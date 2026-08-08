"""Tests for `kq export`.

The point of this command is that it exports *recorded* data and refuses to
invent any, so the interesting cases are the refusals: an episode with no
recorded events, and an episode whose audit chain does not verify.
"""
from __future__ import annotations

import json
import os

import pytest
import respx
import yaml
from httpx import Response

from kube_q.cli import export_cmd

_REPORT = {
    "episode_id": "ep-1",
    "chain_valid": True,
    "summary": "3 recorded events · 1 detector firing(s).",
    "root_cause": "OOMKilled — container limit too low",
    "timeline": [
        {"seq": 1, "at": 1_700_000_000.0, "kind": "finding", "summary": "detector oom fired"},
        {"seq": 2, "at": 1_700_000_001.0, "kind": "tool_call", "summary": "tool kubectl: get pods"},
        {"seq": 3, "at": 1_700_000_002.0, "kind": "final", "summary": "investigation concluded"},
    ],
    "what_fired": [
        {"seq": 1, "playbook": "oom", "namespace": "prod", "object": "api", "severity": "critical"}
    ],
}


@pytest.fixture(autouse=True)
def _clean_kube_q_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")


def _mock(report: dict, episode: str = "ep-1"):
    return respx.get(f"http://test-server/v1/episodes/{episode}/postmortem").mock(
        return_value=Response(200, json=report)
    )


@respx.mock
def test_export_json_to_stdout(capsys):
    route = _mock(_REPORT)
    assert export_cmd.run(["ep-1"]) == 0
    assert route.calls[0].request.url.params["format"] == "json"

    data = json.loads(capsys.readouterr().out)
    # The real recorded content must survive the round trip verbatim.
    assert data["episode_id"] == "ep-1"
    assert data["root_cause"] == "OOMKilled — container limit too low"
    assert [e["seq"] for e in data["timeline"]] == [1, 2, 3]


@respx.mock
def test_export_yaml_to_stdout(capsys):
    _mock(_REPORT)
    assert export_cmd.run(["ep-1", "--format", "yaml"]) == 0

    data = yaml.safe_load(capsys.readouterr().out)
    assert data["episode_id"] == "ep-1"
    assert data["what_fired"][0]["playbook"] == "oom"


@respx.mock
def test_export_to_file(tmp_path, capsys):
    _mock(_REPORT)
    out = tmp_path / "nested" / "report.json"
    assert export_cmd.run(["ep-1", "-o", str(out)]) == 0

    assert json.loads(out.read_text(encoding="utf-8"))["episode_id"] == "ep-1"
    assert "Exported 3 recorded event(s)" in capsys.readouterr().out


@respx.mock
def test_empty_episode_exports_nothing(tmp_path, capsys):
    """A missing episode must not produce a report file — the defect this replaces."""
    _mock({"episode_id": "ghost", "chain_valid": True, "timeline": []}, episode="ghost")
    out = tmp_path / "ghost.json"

    assert export_cmd.run(["ghost", "-o", str(out)]) == 4
    assert not out.exists()
    assert "No recorded events" in capsys.readouterr().err


@respx.mock
def test_broken_chain_exports_but_signals(capsys):
    _mock({**_REPORT, "chain_valid": False})
    assert export_cmd.run(["ep-1"]) == 3

    captured = capsys.readouterr()
    assert json.loads(captured.out)["episode_id"] == "ep-1"   # still exported
    assert "AUDIT CHAIN BROKEN" in captured.err


@respx.mock
def test_server_error_is_reported(capsys):
    respx.get("http://test-server/v1/episodes/ep-1/postmortem").mock(
        return_value=Response(500, text="boom")
    )
    assert export_cmd.run(["ep-1"]) == 1
    assert "Export failed" in capsys.readouterr().err


@respx.mock
def test_unwritable_output_path_fails(capsys, tmp_path):
    _mock(_REPORT)
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory")
    assert export_cmd.run(["ep-1", "-o", str(blocker / "report.json")]) == 1
    assert "Could not write" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        [],                                # no episode id
        ["ep-1", "--format", "xml"],       # unsupported format
        ["ep-1", "--format"],              # flag without a value
        ["ep-1", "--output"],              # flag without a value
        ["ep-1", "extra"],                 # stray second positional
        ["ep-1", "--nope"],                # unknown flag
    ],
)
def test_usage_errors(argv):
    assert export_cmd.run(argv) == 2


def test_help_is_zero():
    assert export_cmd.run(["--help"]) == 0


def test_registered_in_the_subcommand_registry():
    from kube_q.cli import subcommands

    assert "export" in subcommands.names()
    assert subcommands.get_runner("export") is export_cmd.run
    assert "--format" in subcommands.completion_hints("export")["flags"]
