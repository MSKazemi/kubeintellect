"""The mode gate reported what it had skipped only when it had nothing else to say.

`check-file-modes.sh` counts tracked paths it cannot examine — symlinks, gitlinks, entries
absent from this checkout — and says so, because "a sparse or partial checkout used to make
this gate pass having examined nothing". That sentence is already in the script, and
`test_a_skipped_path_is_counted_out_loud_not_dropped` already drives it.

It only drove the clean path. The note lived *inside* the OK branch, so the other two exits
never carried it. Measured 2026-08-24 on a throwaway index holding one violation and one
tracked-but-absent path:

    $ check-file-modes.sh
    checked 3 file(s) from the default git index.
    ERROR: 1 tracked file(s) have a shebang but are not executable (ruff EXE001).
    …
    exit=1

    $ check-file-modes.sh --fix
    fixed: removed +x from 0 file(s), added +x to 1 file(s)
    The mode changes are STAGED (index + working tree). Review with:
    exit=0

The second is the one that matters: the word *fixed*, exit 0, and no hint that a tracked
path was never examined and may still be wrong. A gap in the denominator does not become
less true when the numerator is bad news.

The fix is structural rather than three copies of one echo — the note is hoisted above every
branch, so a path added later cannot forget it. That is what the last class of test below
pins: not the wording, but that no exit can be reached without it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-file-modes.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not _SCRIPT.exists(),
    reason="needs git and the repo-root mode gate",
)

NOTE = "tracked path(s) were skipped"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, env=dict(os.environ),
                          capture_output=True, text=True, check=True)


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(_SCRIPT), *args], cwd=repo,
                          capture_output=True, text=True)


def _write(repo: Path, rel: str, body: str, *, executable: bool) -> None:
    path = repo / rel
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """One clean file, one violation, one tracked path absent from the checkout.

    Only the index is populated — `git ls-files -s` is what the script reads, and a commit
    would trip this machine's author-identity hook for no benefit.
    """
    root = tmp_path / "tree"
    root.mkdir()
    _git(root, "init", "-q", ".")
    _write(root, "run.sh", "#!/bin/sh\necho hi\n", executable=True)
    _write(root, "broken.sh", "#!/bin/sh\n", executable=False)   # shebang, no +x
    _write(root, "vanishing.txt", "gone\n", executable=False)
    _git(root, "add", "-A")
    (root / "vanishing.txt").unlink()                            # tracked, now absent
    return root


class TestEveryExitCarriesTheNote:
    def test_the_failing_run_states_what_it_skipped(self, repo: Path):
        result = _run(repo)
        assert result.returncode == 1, result.stdout
        assert "EXE001" in result.stdout, "the fixture stopped producing a violation"
        assert NOTE in result.stdout, (
            "the run reported a violation and never mentioned that a tracked path was "
            "never examined — the reader cannot tell whether more are hiding there")

    def test_the_fix_run_does_not_claim_a_complete_repair(self, repo: Path):
        result = _run(repo, "--fix")
        assert result.returncode == 0, result.stdout
        assert "fixed:" in result.stdout, "the fixture stopped exercising --fix"
        assert NOTE in result.stdout, (
            "`--fix` said 'fixed' and exited 0 while a tracked path was never examined")

    def test_the_clean_run_keeps_the_note_it_already_had(self, repo: Path):
        """The decided behaviour this file must not regress."""
        _git(repo, "update-index", "--chmod=+x", "broken.sh")
        result = _run(repo)
        assert result.returncode == 0, result.stdout
        assert "file modes OK" in result.stdout
        assert NOTE in result.stdout

    def test_the_note_precedes_the_verdict_it_qualifies(self, repo: Path):
        out = _run(repo).stdout
        assert out.index(NOTE) < out.index("from the default git index."), (
            "a caveat printed after the verdict is a caveat read after the decision")

    def test_the_count_is_the_number_skipped(self, repo: Path):
        note = [ln for ln in _run(repo).stdout.splitlines() if NOTE in ln]
        assert len(note) == 1, "the note must appear exactly once per run"
        assert "1 tracked" in note[0], (
            "the note named no size; 1 skipped path and 300 are not the same situation, and "
            "the run's other counts appear elsewhere in the output")


class TestNothingIsSaidWhenNothingWasSkipped:
    def test_a_complete_run_grows_no_caveat(self, repo: Path):
        """Hoisting the note must not make it unconditional."""
        _git(repo, "update-index", "--chmod=+x", "broken.sh")
        _git(repo, "rm", "-q", "--cached", "vanishing.txt")
        result = _run(repo)
        assert result.returncode == 0, result.stdout
        assert NOTE not in result.stdout, "a run that skipped nothing claimed it skipped"

    def test_a_complete_failing_run_grows_no_caveat(self, repo: Path):
        _git(repo, "rm", "-q", "--cached", "vanishing.txt")
        result = _run(repo)
        assert result.returncode == 1
        assert NOTE not in result.stdout


class TestNoExitPathCanForgetIt:
    """The invariant, asserted structurally: the note is hoisted, not copied per branch.

    Three exits print a verdict — OK, violations, `--fix`. A per-branch echo would satisfy
    the behavioural tests above and still leave the fourth branch, whenever it is written,
    silent by default. Asserting there is exactly ONE emitting line, above all of them, is
    what makes the property hold for branches that do not exist yet.
    """

    def test_the_note_is_emitted_from_exactly_one_place(self):
        body = _SCRIPT.read_text(encoding="utf-8")
        emitters = [n for n, ln in enumerate(body.splitlines(), 1)
                    if NOTE in ln and ln.lstrip().startswith("echo")]
        assert len(emitters) == 1, (
            f"the note is echoed from {len(emitters)} places; one per branch means the next "
            "branch starts silent")

    def test_it_is_emitted_before_every_verdict(self):
        lines = _SCRIPT.read_text(encoding="utf-8").splitlines()
        note_at = next(n for n, ln in enumerate(lines)
                       if NOTE in ln and ln.lstrip().startswith("echo"))
        verdicts = [n for n, ln in enumerate(lines)
                    if ln.lstrip().startswith(("echo \"file modes OK", "echo \"checked ",
                                               "echo \"fixed:"))]
        assert len(verdicts) == 3, f"expected three verdict lines, found {len(verdicts)}"
        assert all(note_at < v for v in verdicts), (
            "a verdict is printed before the caveat that qualifies it")
