"""Gate: every `kq` subcommand documents exactly the exit codes it can return.

An exit table in `docs/cli-reference.md` is not prose. It is the machine-readable half
of the CLI's contract — the thing a script branches on with
``kq replay "$id" || case $? in 3) …`` — and until this guard existed nothing checked
it. `kq replay` documented five codes and returned six: the sixth, ``2``, hid inside
``return 0 if asked_for_help else 2``, one statement that yields two codes. Six more
commands had no table at all while returning ``1`` and ``2``.

A missing row is worse than a missing paragraph. A missing paragraph makes a reader
ask; a missing row makes a script take the wrong branch and report success.

So this file tests two things, and the second is the one that lasts:

1. the tree is currently consistent — every table matches its command; and
2. the *guard* actually detects each way it can drift, and says so out loud when it
   cannot read something, rather than resolving less and passing more.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_V4 = Path(__file__).resolve().parent.parent
_SCRIPT = _V4 / "scripts" / "check_doc_claims.py"
_CLI = _V4 / "packages" / "kube-q" / "kube_q" / "cli"


def _checker():
    spec = importlib.util.spec_from_file_location("check_doc_claims_exit", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _module_from(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}_cmd.py"
    path.write_text(body, encoding="utf-8")
    return path


# ── 1. the tree as it stands ─────────────────────────────────────────────────


def test_every_kq_command_documents_the_codes_it_returns():
    errors = _checker()._check_exit_codes()
    assert errors == [], "CLI exit-code drift:\n" + "\n".join(errors)


def test_the_guard_is_wired_into_the_gate():
    """`make docs-check` must run this, or it is a test that only ever runs by hand."""
    source = _SCRIPT.read_text(encoding="utf-8")
    run_checks = source.split("def run_checks(")[1].split("\ndef ")[0]
    assert "_check_exit_codes()" in run_checks


def test_the_guard_sees_every_shipped_subcommand():
    """A command with no module is unreachable; a module with no check is unguarded."""
    checker = _checker()
    modules = {p.stem[: -len("_cmd")].replace("_", "-") for p in _CLI.glob("*_cmd.py")}
    assert modules, "no kq subcommand modules found — the test would assert nothing"
    text = checker._read(checker._EXIT_DOC)
    for command in sorted(modules):
        assert checker._doc_section(text, command) is not None, command


def test_replay_documents_the_usage_code_it_actually_returns():
    """The regression this guard was written for, stated as its own assertion."""
    checker = _checker()
    codes, unresolved = checker._exit_codes_of(_CLI / "replay_cmd.py")
    assert unresolved == []
    assert 2 in codes
    section = checker._doc_section(checker._read(checker._EXIT_DOC), "replay")
    assert checker._exit_table(section) == codes == {0, 1, 2, 3, 4, 5}


@pytest.mark.parametrize(
    ("command", "argv"),
    [
        ("replay", []),
        ("replay", ["one", "two"]),
        ("completion", ["not-a-shell"]),
        ("digest", ["--hours", "not-a-number"]),
        ("findings", ["--limit", "not-a-number"]),
        ("v5_status", ["unexpected"]),
        ("preference", ["set"]),
        ("config", ["not-a-subcommand"]),
    ],
)
def test_the_documented_usage_code_is_the_one_the_command_really_returns(command, argv):
    """Read the table, then run the command — a table is a claim about behaviour.

    Every one of these paths returns ``2`` today. Reading it out of the AST proves the
    table matches the source; running it proves the source is the thing that runs.
    """
    checker = _checker()
    section = checker._doc_section(checker._read(checker._EXIT_DOC), command.replace("_", "-"))
    assert 2 in checker._exit_table(section)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; from kube_q.cli import {command}_cmd as m; "
            f"sys.exit(m.run({argv!r}))",
        ],
        cwd=_CLI.parents[2],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr


# ── 2. the guard's own failure modes ─────────────────────────────────────────


def test_one_return_statement_can_carry_two_codes():
    """`return 0 if help else 2` is the shape that hid `kq replay`'s undocumented 2."""
    checker = _checker()
    tree = ast.parse("def run(argv):\n    return 0 if argv else 2\n")
    codes, calls, opaque = checker._returns_of(tree.body[0])
    assert codes == {0, 2}
    assert calls == set() and opaque == []


def test_codes_are_followed_through_helpers(tmp_path):
    """Stopping at `run` would read `kq postmortem` as returning almost nothing."""
    checker = _checker()
    codes, unresolved = checker._exit_codes_of(_CLI / "postmortem_cmd.py")
    assert unresolved == []
    assert codes == {0, 1, 2, 3, 4, 5}
    # …and the helper really is where the interesting half lives.
    tree = ast.parse((_CLI / "postmortem_cmd.py").read_text(encoding="utf-8"))
    run = next(n for n in tree.body if getattr(n, "name", None) == "run")
    shallow, _, _ = checker._returns_of(run)
    assert {3, 4, 5} - shallow, "helper resolution would be untested if run() had them all"


def test_a_return_the_guard_cannot_read_is_reported_not_skipped(tmp_path):
    """A guard that quietly resolves less is a guard that quietly stops guarding."""
    checker = _checker()
    module = _module_from(tmp_path, "opaque", "def run(argv):\n    return CODES[argv[0]]\n")
    codes, unresolved = checker._exit_codes_of(module)
    assert codes == set()
    assert len(unresolved) == 1
    assert "cannot read" in unresolved[0]


def test_a_helper_from_another_module_is_reported_not_assumed(tmp_path):
    checker = _checker()
    module = _module_from(
        tmp_path, "imported", "from x import helper\n\ndef run(argv):\n    return helper(argv)\n"
    )
    codes, unresolved = checker._exit_codes_of(module)
    assert codes == set()
    assert "helper()" in unresolved[0] and "not defined in this module" in unresolved[0]


def test_a_module_without_run_is_an_error(tmp_path):
    checker = _checker()
    module = _module_from(tmp_path, "runless", "def other():\n    return 1\n")
    codes, unresolved = checker._exit_codes_of(module)
    assert codes == set()
    assert "no module-level run(argv)" in unresolved[0]


def test_returns_inside_a_nested_function_are_not_the_commands_codes(tmp_path):
    """A callback's `return 7` is the callback's, not the command's."""
    checker = _checker()
    module = _module_from(
        tmp_path,
        "nested",
        "def run(argv):\n"
        "    def _key(item):\n"
        "        return 7\n"
        "    sorted(argv, key=_key)\n"
        "    return 0\n",
    )
    codes, unresolved = checker._exit_codes_of(module)
    assert codes == {0}
    assert unresolved == []


def test_a_comment_in_a_code_fence_does_not_end_a_section():
    """`# bash — add to ~/.bashrc` inside a ```bash fence looks exactly like a heading."""
    checker = _checker()
    doc = (
        "### `kq demo`\n\n"
        "```bash\n"
        "# a comment that is not a heading\n"
        "kq demo\n"
        "```\n\n"
        "| Exit code | Meaning |\n"
        "|---|---|\n"
        "| `0` | fine |\n"
        "| `2` | usage |\n\n"
        "### `kq next`\n"
    )
    section = checker._doc_section(doc, "demo")
    assert checker._exit_table(section) == {0, 2}


def test_no_table_and_no_section_are_different_answers():
    checker = _checker()
    doc = "### `kq demo`\n\nProse only.\n\n### `kq next`\n"
    assert checker._doc_section(doc, "absent") is None
    assert checker._exit_table(checker._doc_section(doc, "demo")) is None


@pytest.mark.parametrize(
    ("body", "table", "expected"),
    [
        # returns 2, table omits it
        ("def run(argv):\n    return 0 if argv else 2\n", "| `0` | fine |\n", "can return [2] undocumented"),
        # table promises 3, command cannot produce it
        (
            "def run(argv):\n    return 0\n",
            "| `0` | fine |\n| `3` | never happens |\n",
            "documents [3] it cannot return",
        ),
    ],
)
def test_drift_in_either_direction_is_caught(tmp_path, monkeypatch, body, table, expected):
    checker = _checker()
    _module_from(tmp_path, "demo", body)
    doc = "### `kq demo`\n\n| Exit code | Meaning |\n|---|---|\n" + table
    monkeypatch.setattr(checker, "_CLI_DIR", tmp_path)
    monkeypatch.setattr(checker, "_read", lambda _doc: doc)
    errors = checker._check_exit_codes()
    assert len(errors) == 1
    assert expected in errors[0]


def test_a_missing_table_is_an_error_only_when_a_non_zero_exit_exists(tmp_path, monkeypatch):
    checker = _checker()
    doc = "### `kq demo`\n\nNo table here.\n"
    monkeypatch.setattr(checker, "_CLI_DIR", tmp_path)
    monkeypatch.setattr(checker, "_read", lambda _doc: doc)

    _module_from(tmp_path, "demo", "def run(argv):\n    return 0\n")
    assert checker._check_exit_codes() == []

    _module_from(tmp_path, "demo", "def run(argv):\n    return 1 if argv else 0\n")
    errors = checker._check_exit_codes()
    assert len(errors) == 1 and "[1]" in errors[0]


def test_a_command_with_no_doc_section_is_an_error(tmp_path, monkeypatch):
    checker = _checker()
    _module_from(tmp_path, "undocumented", "def run(argv):\n    return 0\n")
    monkeypatch.setattr(checker, "_CLI_DIR", tmp_path)
    monkeypatch.setattr(checker, "_read", lambda _doc: "### `kq other`\n")
    errors = checker._check_exit_codes()
    assert len(errors) == 1
    assert "no `kq undocumented` section" in errors[0]


def test_finding_no_modules_is_an_error_not_a_pass(tmp_path, monkeypatch):
    """Checking nothing is not the same as finding nothing wrong."""
    checker = _checker()
    monkeypatch.setattr(checker, "_CLI_DIR", tmp_path)
    monkeypatch.setattr(checker, "_read", lambda _doc: "")
    errors = checker._check_exit_codes()
    assert len(errors) == 1
    assert "checked nothing" in errors[0]


# ── 3. the other surface: the usage text printed on a usage error ────────────


@pytest.mark.parametrize("name", ["replay", "export", "detector"])
def test_the_printed_usage_text_agrees_with_the_command(name):
    """These commands `print(__doc__)` on a usage error, so it is a second exit table."""
    checker = _checker()
    module = _CLI / f"{name}_cmd.py"
    codes, unresolved = checker._exit_codes_of(module)
    assert unresolved == []
    assert checker._docstring_exit_codes(module) == codes


def test_the_usage_text_names_the_code_a_usage_error_returns():
    """`kq replay`'s block was printed *because* of a usage error and never named 2."""
    checker = _checker()
    assert 2 in checker._docstring_exit_codes(_CLI / "replay_cmd.py")


def test_a_module_with_no_exit_block_is_not_a_drift(tmp_path):
    checker = _checker()
    module = _module_from(tmp_path, "quiet", '"""Just a summary."""\n\ndef run(argv):\n    return 0\n')
    assert checker._docstring_exit_codes(module) is None
    module = _module_from(tmp_path, "bare", "def run(argv):\n    return 0\n")
    assert checker._docstring_exit_codes(module) is None


def test_a_wrapped_explanation_is_not_mistaken_for_a_code(tmp_path):
    """Continuation lines carry prose, and prose can start with a digit."""
    checker = _checker()
    module = _module_from(
        tmp_path,
        "wrapped",
        '"""Summary.\n\n'
        "Exit codes:\n"
        "  0  fine\n"
        "  2  usage error, which is not the same as\n"
        "     3 of the other outcomes below\n\n"
        "Trailing prose.\n"
        '"""\n\ndef run(argv):\n    return 0\n',
    )
    assert checker._docstring_exit_codes(module) == {0, 2}


def test_usage_text_drift_is_caught_in_both_directions(tmp_path, monkeypatch):
    checker = _checker()
    doc = "### `kq demo`\n\n| Exit code | Meaning |\n|---|---|\n| `0` | fine |\n| `2` | usage |\n"
    monkeypatch.setattr(checker, "_CLI_DIR", tmp_path)
    monkeypatch.setattr(checker, "_read", lambda _doc: doc)

    _module_from(
        tmp_path,
        "demo",
        '"""Summary.\n\nExit codes:\n  0  fine\n"""\n\ndef run(argv):\n    return 0 if argv else 2\n',
    )
    errors = checker._check_exit_codes()
    assert len(errors) == 1 and "omits [2]" in errors[0]

    _module_from(
        tmp_path,
        "demo",
        '"""Summary.\n\nExit codes:\n  0  fine\n  2  usage\n  4  imaginary\n"""\n\n'
        "def run(argv):\n    return 0 if argv else 2\n",
    )
    errors = checker._check_exit_codes()
    assert len(errors) == 1 and "lists [4] it cannot return" in errors[0]
