"""`kubeintellect --version` must print a version, not an argparse usage error.

The subcommand parser is created with ``required=True``, so before ``--version``
existed the documented pre-flight check (`kubeintellect --version`) exited 2 with
"the following arguments are required: command". Guard that regression.
"""
from __future__ import annotations

import re

import pytest
from app.cli import __version__, main


def test_version_flag_prints_version_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("kubeintellect ")
    # Either a real dist version, or "unknown" from a bare source tree.
    assert re.fullmatch(r"kubeintellect (\d+\.\d+\.\d+.*|unknown)\n", out), out


def test_version_matches_module_attribute(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])

    assert capsys.readouterr().out.strip() == f"kubeintellect {__version__}"


def test_bare_invocation_still_requires_a_command(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])

    assert exc.value.code == 2
    assert "required" in capsys.readouterr().err
