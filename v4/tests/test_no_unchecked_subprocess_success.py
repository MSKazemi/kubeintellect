"""A command must not report success for a subprocess whose result it never looked at.

`subprocess.run(..., check=False)` with the result thrown away is silent by construction: the
command fails, Python continues to the next line, and the next line is usually a `✓`. Four of
these were live in `kubeintellect`: `kind-setup` printed a table of five RCA scenarios that
kubectl had refused to create, printed "Sample pods deployed" over an empty namespace,
`service uninstall` printed "Service removed" while the server was still running, and
`kubeintellect set` printed "service restarted to apply changes" for a restart systemd never
performed — the last one being the only signal a user gets that new configuration took effect.

Reviewing the four sites fixes those four. This file is what stops the fifth being written: a
discarded result is allowed only when it appears in `_REVIEWED`, next to the reason it is safe.
"""

from __future__ import annotations

import ast
import subprocess
import textwrap
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1] / "packages" / "kubeintellect-server" / "app"

# Discarded results that were read and judged honest. The key is (file, first three command
# words); the value is why no success claim rides on it.
_REVIEWED: dict[tuple[str, str], str] = {
    ("cli.py", "helm repo add"): (
        "fire-and-forget setup: the checked `helm install` a few lines below is what reports, "
        "and it fails loudly if the repo was never added"
    ),
    ("cli.py", "helm repo update"): "same as `helm repo add` — the checked install is the report",
    ("cli.py", "kubectl create namespace"): (
        "expected to fail when the namespace already exists; the checked `helm install "
        "--namespace` below is the real gate"
    ),
    ("cli.py", "kubectl patch svc"): (
        "exposes Loki on a NodePort. The `✓ Loki installed.` above it is gated on the helm "
        "install's returncode and stays true regardless; a failed patch surfaces as an "
        "unreachable Loki in `kubeintellect status`"
    ),
    ("cli.py", "systemctl --user start"): (
        "`service start|stop|status` pass systemctl's own stdout and stderr straight through to "
        "the terminal — no capture_output, and no ✓ printed afterwards"
    ),
    ("cli.py", "systemctl --user stop"): "as `service start`",
    ("cli.py", "systemctl --user status"): "as `service start`",
    ("cli.py", "journalctl --user -u"): "`service logs` streams journalctl to the terminal, tail -f style",
}

# Signatures that were the pass-192 defects. They must never come back.
_MUST_STAY_CHECKED = (
    "kubectl apply -f",
    "systemctl --user restart",
    "systemctl --user disable",
)


def _discarded_calls(tree: ast.AST) -> list[tuple[int, str]]:
    """Every `subprocess.run/call` whose result is dropped and which cannot raise on failure.

    `check_call`/`check_output` raise, and so does `run(..., check=True)`; those are safe and are
    not reported. An assigned or returned result is not an `ast.Expr` statement and is likewise
    out of scope — this looks only for the shape that continues silently.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        module = getattr(func.value, "id", "") if isinstance(func, ast.Attribute) else ""
        if (module, name) not in {("subprocess", "run"), ("subprocess", "call")}:
            continue
        if any(k.arg == "check" and getattr(k.value, "value", None) is True for k in call.keywords):
            continue
        words: list[str] = []
        if call.args and isinstance(call.args[0], ast.List):
            for element in call.args[0].elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    words.append(element.value)
                else:
                    break
        found.append((call.lineno, " ".join(words[:3])))
    return found


def _scan() -> tuple[list[tuple[str, int, str]], int]:
    """(findings, files scanned) across the server package."""
    findings, files = [], 0
    for path in sorted(_APP.rglob("*.py")):
        files += 1
        for lineno, signature in _discarded_calls(ast.parse(path.read_text())):
            findings.append((path.relative_to(_APP).as_posix(), lineno, signature))
    return findings, files


class TestNoUncheckedSubprocessSuccess:
    def test_every_discarded_result_is_on_the_reviewed_list(self):
        findings, _ = _scan()
        unreviewed = [f for f in findings if (f[0], f[2]) not in _REVIEWED]
        assert not unreviewed, (
            "These subprocess results are thrown away, so nothing printed after them can be "
            "trusted. Either check the returncode (see `_run_quietly`), or add the call to "
            f"_REVIEWED with the reason it is safe: {unreviewed}"
        )

    def test_the_scan_actually_walked_the_package(self):
        """Vacuity guard: an empty or misdirected walk would pass the test above trivially."""
        findings, files = _scan()
        assert files >= 100, f"only {files} module(s) scanned — is _APP pointing at the package?"
        assert findings, "no discarded calls found at all — the AST matcher has stopped matching"

    def test_the_reviewed_list_has_no_dead_entries(self):
        """A reason that no longer describes live code is a comment pretending to be a guard."""
        findings, _ = _scan()
        live = {(f[0], f[2]) for f in findings}
        stale = sorted(set(_REVIEWED) - live)
        assert not stale, f"_REVIEWED names calls that no longer exist — delete them: {stale}"

    @pytest.mark.parametrize("signature", _MUST_STAY_CHECKED)
    def test_the_repaired_commands_still_check_their_result(self, signature):
        findings, _ = _scan()
        offenders = [f for f in findings if f[2] == signature]
        assert not offenders, (
            f"`{signature}` is back to discarding its result; a success line is printed for work "
            f"that may not have happened: {offenders}"
        )

    def test_a_planted_unchecked_call_is_caught(self, tmp_path):
        """Red/green: the matcher must fail on the shape it exists to forbid."""
        module = textwrap.dedent("""
            import subprocess
            def deploy():
                subprocess.run(["kubectl", "apply", "-f", "x.yaml"], check=False)
                print("✓ deployed")
        """)
        found = _discarded_calls(ast.parse(module))
        assert [s for _, s in found] == ["kubectl apply -f"]

    def test_checked_and_consumed_results_are_not_flagged(self):
        """The matcher must not fire on the safe shapes, or it would be ignored within a week."""
        module = textwrap.dedent("""
            import subprocess
            def deploy():
                subprocess.run(["a", "b"], check=True)
                result = subprocess.run(["c", "d"], check=False)
                subprocess.check_call(["e", "f"])
                return result.returncode
        """)
        assert _discarded_calls(ast.parse(module)) == []


class TestTheCommandsThemselves:
    """The behaviour the guard is protecting, exercised through the CLI's own helpers."""

    def test_run_quietly_reports_a_failing_command(self):
        from app.cli import _run_quietly

        ok, detail = _run_quietly(["false"])
        assert ok is False
        ok, _ = _run_quietly(["true"])
        assert ok is True

    def test_run_quietly_survives_a_missing_binary(self):
        """A missing kubectl must be a False, not a traceback out of a best-effort installer."""
        from app.cli import _run_quietly

        ok, detail = _run_quietly(["kubeintellect-no-such-binary-2f9c"])
        assert ok is False
        assert "kubeintellect-no-such-binary-2f9c" in detail

    def test_run_quietly_returns_what_the_tool_said(self):
        from app.cli import _run_quietly

        ok, detail = _run_quietly(["sh", "-c", "echo boom >&2; exit 3"])
        assert ok is False and detail == "boom"

    def test_run_quietly_gives_up_rather_than_hanging(self):
        from app.cli import _run_quietly

        ok, detail = _run_quietly(["sleep", "5"], timeout=1)
        assert ok is False and "sleep" in detail

    def test_uninstall_reports_a_refused_disable(self, tmp_path, monkeypatch, capsys):
        """`service uninstall` must not print "Service removed" when systemd refused."""
        import argparse

        from app import cli

        unit = tmp_path / "kubeintellect.service"
        unit.write_text("[Unit]\n")
        monkeypatch.setattr(cli, "_SERVICE_FILE", unit)

        def fake_run(cmd, **kwargs):
            assert cmd[0] == "systemctl"
            return subprocess.CompletedProcess(cmd, 1, "", "Failed to connect to bus.")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_service(argparse.Namespace(action="uninstall"))
        assert exit_info.value.code == 1
        out = capsys.readouterr().out
        assert "Service removed" not in out
        assert "Failed to connect to bus." in out
        assert not unit.exists(), "the unit file should still be removed"

    def test_uninstall_reports_a_refused_disable_even_when_the_reload_works(
        self, tmp_path, monkeypatch, capsys
    ):
        """The `disable --now` returncode has to be read on its own — the daemon-reload that
        follows it succeeds happily on a box where the service is still running."""
        import argparse

        from app import cli

        unit = tmp_path / "kubeintellect.service"
        unit.write_text("[Unit]\n")
        monkeypatch.setattr(cli, "_SERVICE_FILE", unit)

        def fake_run(cmd, **kwargs):
            if "disable" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "Interactive authentication required.")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        with pytest.raises(SystemExit):
            cli.cmd_service(argparse.Namespace(action="uninstall"))
        out = capsys.readouterr().out
        assert "Service removed" not in out
        assert "Interactive authentication required." in out

    def test_set_does_not_claim_a_restart_that_did_not_happen(self, tmp_path, monkeypatch, capsys):
        """`kubeintellect set` is how configuration changes; this line is the only signal that
        the running server picked the change up."""
        import argparse

        from app import cli

        config = tmp_path / ".env"
        config.write_text("EXISTING=1\n")
        monkeypatch.setattr(cli, "_CONFIG_FILE", config)
        monkeypatch.setattr(cli, "_CONFIG_DIR", tmp_path)

        def fake_run(cmd, **kwargs):
            if "is-active" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "active\n", "")
            return subprocess.CompletedProcess(cmd, 1, "", "Job for kubeintellect.service failed.")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        cli.cmd_set(argparse.Namespace(assignments=["LOG_LEVEL=DEBUG"]))
        out = capsys.readouterr().out
        assert "service restarted to apply changes" not in out
        assert "previous configuration" in out
        assert "Job for kubeintellect.service failed." in out
        assert "LOG_LEVEL=DEBUG" in config.read_text()
