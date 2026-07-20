"""Tests for `kq completion [bash|zsh|fish]` and its drift guards."""

from __future__ import annotations

import os

import pytest

from kube_q.cli import completion_cmd, subcommands
from kube_q.cli import main as cli_main


@pytest.fixture(autouse=True)
def _clean_kube_q_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)


ALL_COMMANDS = set(subcommands.names())


# ── Script generation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_script_is_nonempty_and_names_every_command(shell):
    script = completion_cmd.script_for(shell)
    assert script.strip()
    for name in ALL_COMMANDS:
        assert name in script, f"{name} missing from {shell} completion"


def test_bash_defines_and_registers_the_function():
    script = completion_cmd.script_for("bash")
    assert "_kq_complete()" in script
    assert "complete -F _kq_complete kq" in script
    # per-command verbs must appear
    assert "show set reset profile" in script  # config
    assert "--limit" in script  # findings


def test_zsh_has_compdef_header_and_footer():
    script = completion_cmd.script_for("zsh")
    assert script.startswith("#compdef kq")
    assert "compdef _kq kq" in script


def test_fish_uses_complete_directives():
    script = completion_cmd.script_for("fish")
    assert "complete -c kq" in script
    assert "__fish_use_subcommand" in script
    assert "__fish_seen_subcommand_from findings" in script


# ── run() dispatch ────────────────────────────────────────────────────────────


def test_run_no_args_prints_usage():
    assert completion_cmd.run([]) == 0
    assert completion_cmd.run(["--help"]) == 0


def test_run_bash_prints_sourceable_script(capsys):
    rc = completion_cmd.run(["bash"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "complete -F _kq_complete kq" in out
    # No Rich markup leaked into the sourceable script.
    assert "[cyan]" not in out


def test_run_unknown_shell_errors():
    assert completion_cmd.run(["powershell"]) == 2


def test_completion_dispatches_through_main(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["kq", "completion", "fish"])
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 0
    assert "complete -c kq" in capsys.readouterr().out


def test_completion_listed_in_help(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["kq", "help"])
    with pytest.raises(SystemExit):
        cli_main.main()
    assert "completion" in capsys.readouterr().out


# ── Drift guard ───────────────────────────────────────────────────────────────


def test_every_global_flag_exists_in_kq_help(monkeypatch, capsys):
    """Completion must never offer a flag `kq --help` doesn't document."""
    monkeypatch.setattr("sys.argv", ["kq", "--help"])
    with pytest.raises(SystemExit):
        cli_main.main()
    help_text = capsys.readouterr().out
    missing = [f for f in completion_cmd.GLOBAL_FLAGS if f not in help_text]
    assert not missing, f"completion offers flags absent from --help: {missing}"


def test_completion_hints_cover_only_real_commands():
    """Every command with hints must be a real registered command."""
    for name in subcommands.names():
        hints = subcommands.completion_hints(name)
        assert set(hints) == {"verbs", "flags"}
