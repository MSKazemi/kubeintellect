"""A gate may choose its scope. It may not describe its own effort as coverage.

`scripts/check-syntax-warnings.py` compiles a list of paths and skips any it cannot open —
a tracked path deleted in the working tree, a submodule, a sparse checkout. Skipping is
correct and *decided*: `test_an_explicit_file_list_is_still_the_callers_business` fixed the
exit code at 0 on purpose, because a pre-commit hook that passes a just-deleted path must
not be turned red for doing the right thing. This file does not touch that.

What it fixes is the sentence. Measured 2026-08-24, before the change:

    $ check-syntax-warnings.py present.py gone1.py gone2.py
    syntax OK — 1 file(s) compile with no SyntaxWarning on Python 3.12.3
    exit=0

    $ check-syntax-warnings.py gone1.py gone2.py
    syntax OK — 0 file(s) compile with no SyntaxWarning on Python 3.12.3
    exit=0

Two files named on the command line were never opened, and nothing said so. The second case
is worse: the word OK over a scan that compiled nothing. The tracked-tree form already
guards that (`test_a_scan_that_compiled_nothing_is_not_a_pass`) — but that guard reads
`if not argv and checked == 0`, so it was switched off for exactly the form a CI hook uses.

The general shape, and the reason this belongs to the same family as the rest of this
directory: a count is only a coverage claim if the denominator is stated. `1 file(s)` is
true of a run that was asked about three, and *the number itself never lies* — which is
what makes it so easy to read as a pass.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_GATE = Path(__file__).resolve().parents[2] / "scripts" / "check-syntax-warnings.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("syntax_gate", _GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gate():
    return _load_checker()


def _files(tmp_path: Path, present: int, missing: int) -> list[str]:
    paths = []
    for i in range(present):
        f = tmp_path / f"ok{i}.py"
        f.write_text("x = 1\n", encoding="utf-8")
        paths.append(str(f))
    paths += [str(tmp_path / f"gone{i}.py") for i in range(missing)]
    return paths


class TestTheSkipIsReported:
    def test_check_paths_returns_what_it_skipped(self, gate, tmp_path):
        """The datum has to exist before any surface can print it."""
        checked, failures, skipped = gate.check_paths(_files(tmp_path, 1, 2))
        assert checked == 1 and not failures
        assert len(skipped) == 2, "the unreadable paths were swallowed, not returned"
        assert all(p.endswith(".py") for p in skipped)

    def test_a_partial_scan_states_the_shortfall(self, gate, tmp_path, capsys):
        rc = gate.main(_files(tmp_path, 1, 2))
        out = capsys.readouterr().out
        assert rc == 0, "the decided contract is exit 0; only the wording was wrong"
        assert "2 of 3 skipped" in out, (
            "the OK line reported 1 file compiled and never mentioned the other two")

    def test_the_skipped_paths_are_named_on_stderr(self, gate, tmp_path, capsys):
        paths = _files(tmp_path, 1, 2)
        gate.main(paths)
        err = capsys.readouterr().err
        note = [ln for ln in err.splitlines() if "could not be read" in ln]
        assert len(note) == 1, "the skip note is the one line that has to exist"
        assert "2 of 3" in note[0], (
            "the note reported a skip without its size; the same figure appears in the stdout "
            "OK line, so asserting over the whole capture would not have proved this line")
        for p in paths[1:]:
            assert p in err, "a skipped path was counted but not named, so it cannot be fixed"

    def test_stdout_stays_parseable(self, gate, tmp_path, capsys):
        """The note goes to stderr so `make check-syntax` output stays one line on stdout."""
        gate.main(_files(tmp_path, 1, 2))
        assert len(capsys.readouterr().out.strip().splitlines()) == 1


class TestNothingCompiledIsNotOK:
    def test_the_word_ok_is_not_printed_over_an_empty_scan(self, gate, tmp_path, capsys):
        """The vacuity guard existed but was gated on `not argv`, i.e. off for CI hooks."""
        rc = gate.main(_files(tmp_path, 0, 2))
        cap = capsys.readouterr()
        assert rc == 0, "still the caller's business — the exit code is a settled contract"
        assert "OK" not in cap.out, (
            "a scan that opened no files at all reported OK; that is the defect the "
            "tracked-tree form already fixed, reached through the other input mode")
        assert "covers no code" in cap.err

    def test_a_clean_full_scan_is_untouched(self, gate, tmp_path, capsys):
        rc = gate.main(_files(tmp_path, 2, 0))
        cap = capsys.readouterr()
        assert rc == 0
        assert "syntax OK — 2 file(s)" in cap.out
        assert "skipped" not in cap.out, "a complete scan must not grow a caveat it has not earned"
        assert cap.err == "", "nothing to note, so nothing on stderr"

    def test_a_real_failure_still_fails(self, gate, tmp_path, capsys):
        """Reporting skips must not outrank reporting defects."""
        bad = tmp_path / "bad.py"
        bad.write_text('import re\nre.compile("\\d+")\n', encoding="utf-8")
        rc = gate.main([str(bad), str(tmp_path / "gone.py")])
        cap = capsys.readouterr()
        assert rc == 1, "a SyntaxWarning was demoted by the presence of a skipped path"
        # Reported as SyntaxError, not SyntaxWarning: `simplefilter("error", SyntaxWarning)`
        # makes the compiler raise it that way, which the gate documents at its `except`.
        # Assert on the message, which is the part that survives that detail.
        assert "invalid escape sequence" in cap.err
        assert "could not be read" in cap.err, "both the defect and the gap must be reported"


class TestTheTrackedFormKeepsItsOwnContract:
    def test_zero_tracked_files_still_fails(self, gate, tmp_path, capsys, monkeypatch):
        """The stronger guard on the tracked form is not weakened by the new wording."""
        monkeypatch.setattr(gate, "repo_root", lambda: str(tmp_path))
        monkeypatch.setattr(gate, "tracked_python_files", lambda root=None: [])
        assert gate.main([]) == 1
        assert "nothing was compiled" in capsys.readouterr().err

    def test_a_sparse_checkout_says_how_much_it_missed(self, gate, tmp_path, capsys, monkeypatch):
        """The tracked form claims to have covered the tree, so a shortfall must be visible."""
        (tmp_path / "here.py").write_text("x = 1\n", encoding="utf-8")
        monkeypatch.setattr(gate, "repo_root", lambda: str(tmp_path))
        monkeypatch.setattr(gate, "tracked_python_files",
                            lambda root=None: ["here.py", "gone_a.py", "gone_b.py"])
        rc = gate.main([])
        cap = capsys.readouterr()
        assert rc == 0
        assert "2 of 3 skipped" in cap.out, (
            "the gate said it covered the tracked tree while a third of it was missing")
        assert "gone_a.py" in cap.err
