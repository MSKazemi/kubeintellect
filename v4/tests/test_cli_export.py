"""Tests for `kubeintellect export` CLI subcommand."""
import argparse
import json
import yaml
from pathlib import Path
import pytest

from app.cli import cmd_export


def test_export_json_stdout(capsys):
    args = argparse.Namespace(format="json", output=None, episode_id="ep-test-1")
    cmd_export(args)
    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert data["status"] == "ok"
    assert data["episode_id"] == "ep-test-1"
    assert "diagnosis" in data


def test_export_yaml_stdout(capsys):
    args = argparse.Namespace(format="yaml", output=None, episode_id="ep-test-2")
    cmd_export(args)
    captured = capsys.readouterr().out
    data = yaml.safe_load(captured)
    assert data["status"] == "ok"
    assert data["episode_id"] == "ep-test-2"
    assert "diagnosis" in data


def test_export_file(tmp_path: Path, capsys):
    out_file = tmp_path / "report.json"
    args = argparse.Namespace(format="json", output=str(out_file), episode_id="ep-test-3")
    cmd_export(args)
    assert out_file.exists()
    content = json.loads(out_file.read_text(encoding="utf-8"))
    assert content["episode_id"] == "ep-test-3"
