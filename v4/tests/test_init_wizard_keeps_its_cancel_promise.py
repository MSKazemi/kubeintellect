"""`kubeintellect init` must not answer an interrupted prompt with a traceback.

The wizard prints "Press Ctrl+C at any time to cancel without saving", and for
every release up to 2.4.0 nothing implemented that: the nine ``input()`` calls
in ``cmd_init`` had no handler, ``main()`` wrapped none of them, so both
KeyboardInterrupt and EOFError escaped as a raw stack trace.

EOF is the one users actually hit — piping, CI, or ``< /dev/null`` reaches
end-of-file on the first question. Measured on a clean Ubuntu 24.04 Azure VM on
2026-08-29: `kubeintellect init < /dev/null` printed
``EOFError: EOF when reading a line`` over a traceback ending in ``cli.py`` line
905, which reads as a crashed installer rather than a wizard that needs a
terminal.
"""
from __future__ import annotations

from unittest.mock import patch

from app.cli import main


def _run_init_raising(exc, capsys):
    with patch("app.cli.cmd_init", side_effect=exc):
        code = main(["init"])
    return code, capsys.readouterr().out


def test_eof_on_a_prompt_is_explained_not_traced(capsys):
    code, out = _run_init_raising(EOFError(), capsys)

    assert code == 1
    assert "end-of-file" in out
    assert "interactive" in out
    assert "Traceback" not in out


def test_ctrl_c_keeps_the_promise_the_banner_makes(capsys):
    code, out = _run_init_raising(KeyboardInterrupt(), capsys)

    # 130 is the conventional shell status for SIGINT.
    assert code == 130
    assert "Cancelled" in out
    assert "nothing was saved" in out.lower()


def test_the_banner_still_makes_that_promise():
    """If the wording goes, this guard should be revisited, not silently kept."""
    import inspect

    from app.cli import cmd_init

    assert "Ctrl+C at any time to cancel" in inspect.getsource(cmd_init)
