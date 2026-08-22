"""Gate: every text-mode read/write names its encoding.

Exercises scripts/check-text-encoding.py, the guard added for the #136/#156
class — a bare read_text()/write_text()/open() decodes with the PLATFORM
DEFAULT, which is not UTF-8 on Windows or under the C locale.

Two things are asserted, and the second matters as much as the first: a checker
that can never fail is worth nothing, so there are explicit red cases, plus the
skips that keep it from firing where there is nothing to fix.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check-text-encoding.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_text_encoding", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_tracked_tree_is_clean():
    # Explicit root: the suite runs from v4/, and `git ls-files` is CWD-relative,
    # so an implicit call here would scan only the v4 subtree.
    checker = _load_checker()
    paths = checker.tracked_python_files(str(_REPO_ROOT))
    # A vacuous scan would pass for free, which is the failure mode this rules out.
    assert len(paths) > 50, f"suspiciously few files scanned: {len(paths)}"

    checked, failures = checker.check_paths(paths, str(_REPO_ROOT))
    assert checked > 50
    assert failures == [], "text-mode call(s) without an encoding:\n" + "\n".join(
        f"{path}:{lineno} {name}()" for path, lineno, name in failures
    )


def test_frozen_generations_are_out_of_scope():
    """v1-v3 are closed under ADR-001/002 and are not built by CI."""
    checker = _load_checker()
    paths = checker.tracked_python_files(str(_REPO_ROOT))
    assert not [p for p in paths if p.startswith(("v1/", "v2/", "v3/"))]
    assert any(p.startswith("v4/") for p in paths)


def test_bare_text_calls_are_caught(tmp_path: Path):
    """The #156 shapes, verbatim: the three ways this repo reads and writes text."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from pathlib import Path\n"
        'Path("a").read_text()\n'
        'Path("b").write_text("x")\n'
        'open("c", "w")\n',
        encoding="utf-8",
    )

    checker = _load_checker()
    checked, failures = checker.check_paths([str(bad)])

    assert checked == 1
    assert [name for _path, _lineno, name in failures] == ["read_text", "write_text", "open"]


def test_binary_mode_is_not_flagged(tmp_path: Path):
    """There is no encoding on a binary handle; flagging it would demand a nonsense fix."""
    binary = tmp_path / "binary.py"
    binary.write_text(
        'open("a", "rb")\nopen("b", "wb")\nopen("c", mode="rb")\n',
        encoding="utf-8",
    )

    checker = _load_checker()
    assert checker.check_paths([str(binary)]) == (1, [])


def test_explicit_encoding_and_forwarded_kwargs_pass(tmp_path: Path):
    """An encoding already named, or one that may arrive through **kwargs."""
    good = tmp_path / "good.py"
    good.write_text(
        "from pathlib import Path\n"
        'Path("a").read_text(encoding="utf-8")\n'
        'open("b", "w", encoding="latin-1")\n'
        'def f(**kw):\n    return open("c", **kw)\n',
        encoding="utf-8",
    )

    checker = _load_checker()
    assert checker.check_paths([str(good)]) == (1, [])


def test_main_exit_codes(tmp_path: Path, capsys):
    """The exit code is the whole contract for a CI gate."""
    checker = _load_checker()

    good = tmp_path / "ok.py"
    good.write_text('open("a", encoding="utf-8")\n', encoding="utf-8")
    assert checker.main([str(good)]) == 0

    bad = tmp_path / "nope.py"
    bad.write_text('open("a")\n', encoding="utf-8")
    assert checker.main([str(bad)]) == 1

    # The failure has to name the file, or a red CI run is a scavenger hunt.
    assert "nope.py" in capsys.readouterr().out
