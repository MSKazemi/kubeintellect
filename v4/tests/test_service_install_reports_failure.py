"""`kubeintellect service install` must not report a service systemd refused.

Both systemctl calls ran with `check=False, capture_output=True` — return code discarded, stderr
swallowed — and the caller then printed unconditionally:

    ✓  Service installed — server will start automatically on login.

The common failure is not exotic. Over SSH with no login session `systemctl --user` answers
`Failed to connect to bus: No medium found` and enables nothing; the same happens in a container
and wherever the user manager is not running. Reproduced 2026-08-24 with a systemctl that exits
1: the ✓ printed, the error text was thrown away, and the command exited 0.

The wizard path was worse. `cmd_init` printed the same ✓, called `_open_kq()` and returned —
skipping the fallback immediately below it that starts a server for the session. So the user got
a `kq` prompt with nothing behind it, on a first run, having answered "yes" to the only question
that was asked.

Two things are pinned here: the report matches what systemd did, and a missing config file
cannot produce a unit that will not start.
"""

from __future__ import annotations

import subprocess

import pytest

from app import cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated HOME, so the unit is written somewhere harmless."""
    monkeypatch.setattr(cli, "_SERVICE_DIR", tmp_path / ".config" / "systemd" / "user")
    monkeypatch.setattr(cli, "_SERVICE_FILE", tmp_path / ".config" / "systemd" / "user"
                        / f"{cli._SERVICE_NAME}.service")
    monkeypatch.setattr(cli, "_CONFIG_FILE", tmp_path / ".kubeintellect" / ".env")
    return tmp_path


def _systemctl(returncode: int, stderr: str = ""):
    """Replace subprocess.run so only systemctl is faked — `which` must still work."""
    real = subprocess.run

    def _run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "systemctl":
            return subprocess.CompletedProcess(cmd, returncode, "", stderr)
        return real(cmd, *args, **kwargs)

    return _run


class TestTheInstallerReportsWhatSystemdDid:
    def test_a_refused_enable_is_not_a_success(self, home, monkeypatch):
        monkeypatch.setattr(cli.subprocess, "run",
                            _systemctl(1, "Failed to connect to bus: No medium found"))
        result = cli._install_service()
        assert result.ok is False
        assert "No medium found" in result.detail

    def test_a_working_systemd_is_still_a_success(self, home, monkeypatch):
        monkeypatch.setattr(cli.subprocess, "run", _systemctl(0))
        assert cli._install_service().ok is True

    def test_a_missing_systemctl_is_reported_not_raised(self, home, monkeypatch):
        real = subprocess.run

        def _run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "systemctl":
                raise FileNotFoundError("no systemctl here")
            return real(cmd, *args, **kwargs)

        monkeypatch.setattr(cli.subprocess, "run", _run)
        result = cli._install_service()
        assert result.ok is False and "systemctl" in result.detail

    def test_the_command_exits_non_zero_so_a_script_can_tell(self, home, monkeypatch, capsys):
        import types
        monkeypatch.setattr(cli.subprocess, "run", _systemctl(1, "Failed to connect to bus"))
        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_service(types.SimpleNamespace(action="install"))
        assert exit_info.value.code == 1
        out = capsys.readouterr().out
        assert "Failed to connect to bus" in out
        assert "start automatically on login" not in out, "it claimed success anyway"

    def test_the_failure_names_something_the_user_can_do(self, home, monkeypatch, capsys):
        monkeypatch.setattr(cli.subprocess, "run", _systemctl(1, "Failed to connect to bus"))
        cli._print_service_failure(cli._install_service())
        out = capsys.readouterr().out
        assert "enable-linger" in out
        assert "kubeintellect serve" in out


class TestTheUnitCanStart:
    def test_a_missing_config_file_does_not_make_the_unit_unstartable(self, home, monkeypatch):
        """`service install` before `init` is a legal order. Without the dash systemd fails it."""
        monkeypatch.setattr(cli.subprocess, "run", _systemctl(0))
        cli._install_service()
        unit = cli._SERVICE_FILE.read_text(encoding="utf-8")
        assert not cli._CONFIG_FILE.exists()
        assert f"EnvironmentFile=-{cli._CONFIG_FILE}" in unit, unit

    def test_the_unit_still_points_at_a_real_command(self, home, monkeypatch):
        """Vacuity guard: an empty ExecStart would satisfy every assertion above."""
        monkeypatch.setattr(cli.subprocess, "run", _systemctl(0))
        cli._install_service()
        exec_line = [line for line in cli._SERVICE_FILE.read_text(encoding="utf-8").splitlines()
                     if line.startswith("ExecStart=")]
        assert exec_line and exec_line[0].endswith("kubeintellect serve")
        assert len(exec_line[0]) > len("ExecStart= serve")
