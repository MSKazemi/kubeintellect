"""`kubeintellect init` died before its first question on a machine with no `sudo`.

Measured 2026-08-29 on a clean `python:3.12-slim` container against the published 2.4.1:

    $ kubeintellect init
      Failed to install 'kubectl': [Errno 2] No such file or directory: 'sudo'
    [exit 1]

Two faults, one symptom. `_install_kubectl` shelled out to `sudo` unconditionally, so on any
image without it the exec raised `FileNotFoundError` — and `_ensure_tool` turned every
installer failure into `sys.exit(1)`, so a *convenience* install ended the wizard. `init`
only writes configuration; `status` already reports a missing kubectl as a warning. The user
came to save settings and got nothing.

Same shape as `_docker_available()` raising because Docker was absent: a helper that fails in
exactly the situation it exists to handle.
"""
from __future__ import annotations

import subprocess

import pytest
from app import cli


def _boom() -> None:
    raise FileNotFoundError(2, "No such file or directory", "sudo")


class TestAConvenienceInstallDoesNotEndTheCaller:
    def test_an_optional_tool_that_cannot_be_installed_is_a_warning(self, capsys, monkeypatch):
        monkeypatch.setattr(
            cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1)
        )
        cli._ensure_tool("kubectl", _boom, required=False)  # must not raise SystemExit
        out = capsys.readouterr().out
        assert "Could not install 'kubectl'" in out
        assert "Continuing without it" in out

    def test_a_required_tool_that_cannot_be_installed_still_exits(self, monkeypatch):
        """The non-fatal path is opt-in; nothing else silently became best-effort."""
        monkeypatch.setattr(
            cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1)
        )
        with pytest.raises(SystemExit) as e:
            cli._ensure_tool("kind", _boom)
        assert e.value.code == 1

    def test_a_tool_that_is_present_is_not_installed_at_all(self, monkeypatch):
        monkeypatch.setattr(
            cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0)
        )
        cli._ensure_tool("kubectl", _boom, required=False)  # installer must never run

    def test_the_wizard_asks_for_kubectl_without_requiring_it(self):
        """The call site is the defect: `required=False` has to be at *this* one."""
        import inspect
        src = inspect.getsource(cli.cmd_init)
        assert '_ensure_tool("kubectl", _install_kubectl, required=False)' in src


class TestSudoIsUsedOnlyWhenItIsNeededAndPresent:
    def test_root_does_not_reach_for_sudo(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        )
        cli._privileged_mv("/tmp/kubectl", "/usr/local/bin/kubectl")
        assert calls == [["mv", "/tmp/kubectl", "/usr/local/bin/kubectl"]]

    def test_a_non_root_machine_without_sudo_gets_a_sentence_not_a_traceback(self, monkeypatch):
        monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(cli.shutil, "which", lambda _: None)
        with pytest.raises(RuntimeError) as e:
            cli._privileged_mv("/tmp/kubectl", "/usr/local/bin/kubectl")
        assert "'sudo' is not installed" in str(e.value)
        assert "mv /tmp/kubectl /usr/local/bin/kubectl" in str(e.value)

    def test_a_non_root_machine_with_sudo_still_elevates(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/sudo")
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        )
        cli._privileged_mv("/tmp/kind", "/usr/local/bin/kind")
        assert calls == [["sudo", "mv", "/tmp/kind", "/usr/local/bin/kind"]]

    def test_no_installer_shells_out_to_sudo_directly_any_more(self):
        import inspect
        for fn in (cli._install_kubectl, cli._install_kind):
            assert '"sudo"' not in inspect.getsource(fn), fn.__name__


class TestNoAvailabilityPredicateRaisesBecauseTheThingIsAbsent:
    """The same defect has now been found three times in one file — Docker, then sudo, then
    systemd — and each one crashed `kubeintellect init` *after* it had printed "Setup
    complete". This class states the rule once so the fourth is caught by a test rather than
    by someone on a clean machine.
    """

    @pytest.mark.parametrize("predicate", ["_docker_available", "_systemd_available"])
    def test_an_absent_binary_reads_as_unavailable(self, predicate, monkeypatch):
        def _missing(*a, **k):
            raise FileNotFoundError(2, "No such file or directory", "missing-binary")
        monkeypatch.setattr(cli.subprocess, "run", _missing)
        assert getattr(cli, predicate)() is False

    @pytest.mark.parametrize("predicate", ["_docker_available", "_systemd_available"])
    def test_a_hung_daemon_reads_as_unavailable(self, predicate, monkeypatch):
        def _hangs(*a, **k):
            raise subprocess.TimeoutExpired(cmd="x", timeout=5)
        monkeypatch.setattr(cli.subprocess, "run", _hangs)
        assert getattr(cli, predicate)() is False

    @pytest.mark.parametrize("predicate", ["_docker_available", "_systemd_available"])
    def test_every_such_predicate_bounds_its_wait(self, predicate):
        """Without a timeout an unreachable daemon hangs the wizard instead of crashing it,
        which is worse, not better."""
        import inspect
        assert "timeout=" in inspect.getsource(getattr(cli, predicate)), predicate
