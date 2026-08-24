"""Gate: no tracked Python file carries a SyntaxWarning.

Exercises scripts/check-syntax-warnings.py, the guard added for the #63 class
(an invalid escape sequence in a non-raw string, which also corrupted the
jsonpath examples in the coordinator prompt).

Two things are asserted, and the second matters as much as the first: a checker
that can never fail is worth nothing, so there is an explicit red case built
from the exact defect #63 reported.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check-syntax-warnings.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_syntax_warnings", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_tracked_tree_is_clean():
    # Note the explicit root: the suite runs from v4/, and `git ls-files` is
    # CWD-relative, so an implicit call here would scan only the v4 subtree.
    checker = _load_checker()
    paths = checker.tracked_python_files(str(_REPO_ROOT))
    # If the scan ever returns nothing the gate would pass vacuously — which is
    # precisely the failure mode this file exists to rule out.
    assert len(paths) > 50, f"suspiciously few files scanned: {len(paths)}"

    checked, failures, _skipped = checker.check_paths(paths, str(_REPO_ROOT))
    assert checked > 50
    assert failures == [], "SyntaxWarning(s) in tracked source:\n" + "\n".join(
        f"{p}: {detail}" for p, detail in failures
    )


def test_frozen_generations_are_out_of_scope():
    """v1-v3 are closed under ADR-001/002 and are not built by CI."""
    checker = _load_checker()
    paths = checker.tracked_python_files(str(_REPO_ROOT))
    assert not [p for p in paths if p.startswith(("v1/", "v2/", "v3/"))]
    # …and the frozen trees really do contain Python, so the filter is load-bearing.
    assert any(p.startswith("v4/") for p in paths)


def test_invalid_escape_sequence_is_caught(tmp_path: Path):
    """The #63 defect, verbatim: a regex in a non-raw string."""
    bad = tmp_path / "bad.py"
    bad.write_text('import re\n_RE = re.compile("^[ \\t]*(?:[-*]|\\d+\\.?)\\s+(.+)$")\n')

    checker = _load_checker()
    checked, failures, _skipped = checker.check_paths([str(bad)])

    assert checked == 1
    assert len(failures) == 1
    assert "\\d" in failures[0][1]


def test_clean_file_passes(tmp_path: Path):
    good = tmp_path / "good.py"
    good.write_text('import re\n_RE = re.compile(r"^[ \\t]*(?:[-*]|\\d+\\.?)\\s+(.+)$")\n')

    checker = _load_checker()
    checked, failures, _skipped = checker.check_paths([str(good)])

    assert (checked, failures) == (1, [])


def test_main_exit_codes(tmp_path: Path, capsys):
    """The exit code is the whole contract for a CI gate."""
    checker = _load_checker()

    good = tmp_path / "ok.py"
    good.write_text("x = 1\n")
    assert checker.main([str(good)]) == 0

    bad = tmp_path / "nope.py"
    bad.write_text('x = "\\d+"\n')
    assert checker.main([str(bad)]) == 1

    # The failure has to name the file, or a red CI run is a scavenger hunt.
    assert "nope.py" in capsys.readouterr().err


def test_a_scan_that_compiled_nothing_is_not_a_pass(tmp_path: Path, capsys, monkeypatch):
    """Vacuity guard — the sibling of the same defect found in check-file-modes.sh.

    The tracked-tree form used to print `syntax OK — 0 tracked Python files …` and
    exit 0. The count was visible, but a zero beside the word OK still reads as a
    pass, and a sparse or partial checkout reaches it.
    """
    checker = _load_checker()
    monkeypatch.setattr(checker, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(checker, "tracked_python_files", lambda root=None: [])

    assert checker.main([]) == 1
    assert "nothing was compiled" in capsys.readouterr().err


def test_an_explicit_file_list_is_still_the_callers_business(tmp_path: Path):
    """`--` explicit paths keep the old contract: the caller chose the scope.

    A hook that passes a list including a deleted path must not be turned red by
    the guard above; only the tracked-tree form claims to have covered the tree.
    """
    checker = _load_checker()
    missing = tmp_path / "gone.py"

    assert checker.main([str(missing)]) == 0
