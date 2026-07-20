"""Tests for the `kq <command>` registry and main() dispatch/discovery."""

from __future__ import annotations

import os

import pytest

from kube_q.cli import main as cli_main
from kube_q.cli import subcommands


@pytest.fixture(autouse=True)
def _clean_kube_q_env(monkeypatch):
    # Keep KUBE_Q_* config env vars from leaking between tests — the argparse
    # `--help` path calls load_config(), which validates them.
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)


# ── Registry ──────────────────────────────────────────────────────────────────

EXPECTED = {
    "config",
    "findings",
    "digest",
    "replay",
    "postmortem",
    "detector",
    "preference",
    "v5-status",
    "completion",
}


def test_registry_lists_every_subcommand():
    assert set(subcommands.names()) == EXPECTED


def test_every_command_resolves_to_a_callable_runner():
    for name in subcommands.names():
        runner = subcommands.get_runner(name)
        assert callable(runner), f"{name} has no run() callable"


def test_get_runner_unknown_returns_none():
    assert subcommands.get_runner("nope") is None


def test_describe_and_help_block_cover_all_commands():
    described = dict(subcommands.describe())
    assert set(described) == EXPECTED
    assert all(desc for desc in described.values()), "every command needs a description"
    block = subcommands.help_block()
    for name in EXPECTED:
        assert name in block


@pytest.mark.parametrize(
    "typo,expected",
    [
        ("fndings", "findings"),
        ("detctor", "detector"),
        ("postmortum", "postmortem"),
        ("digset", "digest"),
    ],
)
def test_suggest_matches_close_typos(typo, expected):
    assert expected in subcommands.suggest(typo)


def test_suggest_gibberish_is_empty():
    assert subcommands.suggest("zzzzzz") == []


def test_help_aliases():
    assert subcommands.is_help_alias("help")
    assert subcommands.is_help_alias("commands")
    assert not subcommands.is_help_alias("findings")


# ── main() dispatch ───────────────────────────────────────────────────────────


def test_known_subcommand_dispatches_and_returns_exit_code(monkeypatch):
    recorded = {}

    def fake_run(argv):
        recorded["argv"] = argv
        return 7

    # get_runner imports the module and reads .run at call time, so patching the
    # module attribute is enough.
    import kube_q.cli.findings_cmd as findings_cmd

    monkeypatch.setattr(findings_cmd, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["kq", "findings", "--limit", "5"])

    with pytest.raises(SystemExit) as exc:
        cli_main.main()

    assert exc.value.code == 7
    assert recorded["argv"] == ["--limit", "5"]


def test_help_alias_lists_commands(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["kq", "help"])
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in EXPECTED:
        assert name in out


def test_unknown_command_suggests_and_exits_2(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["kq", "fndings"])
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "Unknown command" in out
    assert "findings" in out  # suggestion


def test_help_block_appears_in_argparse_help(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["kq", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Commands" in out
    assert "findings" in out
    assert "v5-status" in out
