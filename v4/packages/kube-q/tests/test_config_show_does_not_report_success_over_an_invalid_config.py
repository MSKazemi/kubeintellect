"""`kq config show` must not report success while printing "⚠ Invalid values detected".

Two defects in `config_cmd`, both measured 2026-08-24.

1. `cmd_show` printed the invalid-values banner and returned **0**. The human-readable answer
   and the machine-readable answer disagreed, and only the exit code is read by
   `kq config show || …` in an install script or a CI pre-flight. Its sibling `cmd_set` already
   returned 2 for a bad key, and `load_config(strict=True)` already exits 2 on exactly these
   errors, so the odd one out was `show`.

2. Both call sites rendered `err.splitlines()[0]`. `validate_config` builds each message in
   three parts on purpose — its docstring promises "the offending value, the matching env var
   (so the user knows what to edit), and what a valid value looks like" — and the last two are
   on lines 2 and 3. The user saw the complaint and never the remedy:

       Invalid URL: 'not-a-url' — must start with http:// or https:// …
       [dropped]  Example: KUBE_Q_URL=https://api.kubeintellect.com
       [dropped]  Fix: set KUBE_Q_URL in ~/.kube-q/.env or pass a valid value.
"""
from __future__ import annotations

import inspect

import pytest

from kube_q.cli import config_cmd
from kube_q.core.config import Config, validate_config

_BAD = dict(url="not-a-url", timeout=-5, output="yaml")


@pytest.fixture
def show(monkeypatch):
    """Run `cmd_show` against a chosen Config, returning (exit_code, stdout)."""
    def _run(capsys, **overrides):
        cfg = Config(**overrides)
        monkeypatch.setattr(config_cmd, "load_config", lambda strict=False: cfg)
        rc = config_cmd.cmd_show()
        return rc, capsys.readouterr().out
    return _run


class TestTheExitCodeAgreesWithWhatWasPrinted:
    def test_an_invalid_config_exits_non_zero(self, show, capsys):
        rc, out = show(capsys, **_BAD)
        assert "Invalid values detected" in out, "the banner is the premise of this test"
        assert rc != 0, (
            "`kq config show` printed the invalid-values banner and reported success; "
            "`kq config show || exit 1` in an install script never fires")

    def test_a_valid_config_still_exits_zero(self, show, capsys):
        rc, out = show(capsys)
        assert rc == 0, "a healthy config now fails, which breaks every honest caller"
        assert "Invalid values detected" not in out

    def test_the_code_is_the_one_the_project_already_uses_for_bad_config(self, show, capsys):
        """`load_config(strict=True)` exits 2 on these same errors — don't invent a second code."""
        import kube_q.core.config as core_config
        rc, _ = show(capsys, **_BAD)
        assert rc == 2, f"expected the established configuration-error code 2, got {rc}"
        assert "sys.exit(2)" in inspect.getsource(core_config.load_config), (
            "`load_config(strict=True)` no longer exits 2 on a config error, so 2 is no longer "
            "'the code the project already uses' — recheck this test's premise")

    def test_the_table_is_still_printed_before_the_errors(self, show, capsys):
        """It is still a `show` command — failing must not cost the output."""
        rc, out = show(capsys, **_BAD)
        assert "KUBE_Q_URL" in out and out.index("KUBE_Q_URL") < out.index("Invalid values")


class TestTheRemedyIsNotDropped:
    def test_the_fix_line_survives(self, show, capsys):
        _, out = show(capsys, **_BAD)
        assert "KUBE_Q_URL in ~/.kube-q/.env" in out.replace("\n", " "), (
            "the message naming what to edit was truncated away")

    def test_the_example_line_survives(self, show, capsys):
        _, out = show(capsys, **_BAD)
        assert "Example:" in out, "the 'what a valid value looks like' half was dropped"

    def test_every_error_that_has_a_hint_shows_it(self, show, capsys):
        _, out = show(capsys, **_BAD)
        flat = " ".join(out.split())
        for field in ("KUBE_Q_URL", "KUBE_Q_TIMEOUT", "KUBE_Q_OUTPUT"):
            assert field in flat, f"{field}'s remedy never reached the user"

    def test_the_premise_holds_the_messages_really_are_multi_line(self):
        """Non-vacuity: if `validate_config` went single-line, these tests prove nothing."""
        errs = validate_config(Config(**_BAD))
        assert errs and any(len(e.splitlines()) > 1 for e in errs), (
            "validate_config no longer builds multi-line messages; this file's premise is stale")

    def test_a_single_line_error_does_not_gain_a_blank_continuation(self, capsys):
        config_cmd._print_errors(["just one line"])
        out = capsys.readouterr().out
        assert out.strip() == "• just one line", f"padding was invented: {out!r}"

    def test_blank_continuation_lines_are_skipped(self, capsys):
        """Count EVERY emitted line, not just the non-blank ones — a whitespace-only line is
        exactly what the skip exists to prevent, and filtering blanks in the assertion made the
        test unable to see it (caught by mutation, 2026-08-24)."""
        config_cmd._print_errors(["head\n\n  tail"])
        out = capsys.readouterr().out
        assert out == "  • head\n    tail\n", (
            f"a blank hint line was emitted as padding: {out!r}")


class TestTheWriteGuardKeepsItsRemedyToo:
    """`cmd_set` refuses a bad value; the refusal must say how to fix it."""

    @pytest.fixture(autouse=True)
    def _no_writes(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config_cmd, "ENV_FILE", tmp_path / ".env")

    def test_a_refused_write_shows_the_remedy(self, capsys):
        rc = config_cmd.cmd_set("url=not-a-url")
        out = capsys.readouterr().out
        assert rc == 2
        assert "Refusing to write" in out
        assert "KUBE_Q_URL" in out.replace("\n", " ")

    def test_a_refused_write_still_returns_two(self, capsys):
        assert config_cmd.cmd_set("output=yaml") == 2

    def test_a_valid_write_is_unaffected(self, capsys):
        rc = config_cmd.cmd_set("url=https://example.invalid")
        assert rc == 0 and "✓" in capsys.readouterr().out


class TestNoCallSiteStillTruncates:
    def test_the_first_line_only_rendering_is_gone(self):
        """Structural guard — the bug's exact shape must not come back anywhere in this module."""
        import ast
        tree = ast.parse(inspect.getsource(config_cmd))
        offenders = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "splitlines"
            and isinstance(node.slice, ast.Constant) and node.slice.value == 0
        ]
        assert not offenders, (
            f"a call site renders only the first line of a config error again, at line(s) "
            f"{offenders}")
