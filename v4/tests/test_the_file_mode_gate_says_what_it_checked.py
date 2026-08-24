"""Gate on the gate: `check-file-modes.sh` must not claim more than it examined.

`make check-modes` is one of the six required CI checks and, until this file, nothing
drove it. It printed

    file modes OK — every tracked file outside v1-v3 is executable iff it has a shebang

which is a claim about *the tree*. It is not what the script measures. The script reads
**one index** — `git ls-files -s` — and silently skips every tracked path that is not a
regular file in the current checkout. Two consequences, both reproduced before the fix:

* a checkout where nothing tracked is present (sparse, partial, or an index holding only
  the frozen v1–v3 generations) printed that same confident OK line having examined **zero**
  files, and exited 0;
* a working tree carrying a second index — this repository has one — was reported clean by
  the default run while the other index recorded 97 shebang'd files as non-executable,
  including the `ship` tool itself.

The fix is not a wider default scope: it is that the summary states the index and the count,
that examining nothing is a failure, that skipped paths are counted out loud, and that
`--git-dir` can point the same invariant at another index.
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


def _git(repo: Path, *args: str, git_dir: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if git_dir is not None:
        env["GIT_DIR"] = git_dir
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    )


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_SCRIPT), *args], cwd=repo, capture_output=True, text=True
    )


def _write(repo: Path, rel: str, body: str, *, executable: bool) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo. Only the index is ever populated — never a commit.

    The script reads `git ls-files -s`, so `git add` is enough, and committing here would
    trip this machine's author-identity hook for no benefit.
    """
    root = tmp_path / "tree"
    root.mkdir()
    _git(root, "init", "-q", ".")
    _write(root, "run.sh", "#!/bin/sh\necho hi\n", executable=True)
    _write(root, "data.txt", "x\n", executable=False)
    _git(root, "add", "-A")
    return root


class TestARunThatExaminedNothingIsNotAPass:
    """The vacuity case. This is the whole reason the count exists."""

    def test_an_index_with_only_frozen_paths_fails_instead_of_reporting_ok(self, repo: Path):
        (repo / "run.sh").unlink()
        (repo / "data.txt").unlink()
        _write(repo, "v1/app/main.py", "print('frozen')\n", executable=False)
        _git(repo, "add", "-A")

        result = _run(repo)

        assert result.returncode != 0, result.stdout
        assert "examined 0 files" in result.stdout
        assert "file modes OK" not in result.stdout

    def test_an_index_whose_files_are_all_absent_from_the_checkout_fails(self, repo: Path):
        (repo / "run.sh").unlink()
        (repo / "data.txt").unlink()

        result = _run(repo)

        assert result.returncode != 0
        assert "examined 0 files" in result.stdout


class TestTheSummarySaysWhatItChecked:
    def test_the_ok_line_carries_the_number_of_files_examined(self, repo: Path):
        result = _run(repo)

        assert result.returncode == 0, result.stdout
        assert "file modes OK" in result.stdout
        assert "2 file(s)" in result.stdout
        # the discarded claim: a statement about the tree rather than about this index
        assert "every tracked file" not in result.stdout

    def test_a_skipped_path_is_counted_out_loud_not_dropped(self, repo: Path):
        (repo / "data.txt").unlink()

        result = _run(repo)

        assert result.returncode == 0, result.stdout
        assert "1 file(s)" in result.stdout
        assert "1 tracked path(s) were skipped" in result.stdout

    def test_a_failing_run_also_states_the_scope(self, repo: Path):
        _write(repo, "broken.sh", "#!/bin/sh\n", executable=False)
        _git(repo, "add", "-A")

        result = _run(repo)

        assert result.returncode == 1
        assert "checked 3 file(s)" in result.stdout


class TestGitDirPointsTheInvariantAtAnotherIndex:
    """The live case: this working tree carries two indexes that disagree on 412 modes."""

    @staticmethod
    def _second_index(repo: Path) -> None:
        _git(repo, "init", "-q", ".", git_dir=".git2")
        _git(repo, "config", "core.bare", "false", git_dir=".git2")
        _git(repo, "add", "run.sh", "data.txt", git_dir=".git2")

    def test_the_default_run_cannot_see_a_violation_in_the_other_index(self, repo: Path):
        self._second_index(repo)
        _git(repo, "update-index", "--chmod=-x", "run.sh", git_dir=".git2")

        default = _run(repo)

        assert default.returncode == 0, default.stdout
        assert "file modes OK" in default.stdout

    def test_git_dir_reads_the_named_index_and_finds_it(self, repo: Path):
        self._second_index(repo)
        _git(repo, "update-index", "--chmod=-x", "run.sh", git_dir=".git2")

        result = _run(repo, "--git-dir", ".git2")

        assert result.returncode == 1, result.stdout
        assert "run.sh" in result.stdout
        assert "the index of .git2" in result.stdout

    def test_the_remedy_names_the_index_that_was_checked(self, repo: Path):
        """A bare `--fix` would rewrite the wrong index — the same class of wrong answer."""
        self._second_index(repo)
        _git(repo, "update-index", "--chmod=-x", "run.sh", git_dir=".git2")

        result = _run(repo, "--git-dir", ".git2")

        assert "--fix --git-dir .git2" in result.stdout

    def test_the_equals_form_is_accepted(self, repo: Path):
        self._second_index(repo)

        result = _run(repo, "--git-dir=.git2")

        assert result.returncode == 0, result.stdout
        assert "the index of .git2" in result.stdout

    def test_a_missing_directory_is_refused_rather_than_silently_ignored(self, repo: Path):
        result = _run(repo, "--git-dir", ".git-nope")

        assert result.returncode == 2
        assert "no such git directory" in result.stderr

    def test_git_dir_without_a_value_is_refused(self, repo: Path):
        result = _run(repo, "--git-dir")

        assert result.returncode == 2
        assert "needs a directory" in result.stderr


class TestTheOriginalInvariantStillHolds:
    """Everything above is new reporting. The rule it reports on must not have moved."""

    def test_executable_without_a_shebang_is_a_violation(self, repo: Path):
        _write(repo, "notes.md", "# hi\n", executable=True)
        _git(repo, "add", "-A")

        result = _run(repo)

        assert result.returncode == 1
        assert "EXE002" in result.stdout
        assert "notes.md" in result.stdout

    def test_a_shebang_without_the_bit_is_a_violation(self, repo: Path):
        _write(repo, "tool.py", "#!/usr/bin/env python3\n", executable=False)
        _git(repo, "add", "-A")

        result = _run(repo)

        assert result.returncode == 1
        assert "EXE001" in result.stdout
        assert "tool.py" in result.stdout

    def test_the_frozen_generations_are_still_excluded(self, repo: Path):
        _write(repo, "v2/legacy.md", "# frozen\n", executable=True)
        _git(repo, "add", "-A")

        result = _run(repo)

        assert result.returncode == 0, result.stdout

    def test_fix_repairs_both_directions_in_the_index(self, repo: Path):
        _write(repo, "notes.md", "# hi\n", executable=True)
        _write(repo, "tool.py", "#!/usr/bin/env python3\n", executable=False)
        _git(repo, "add", "-A")
        assert _run(repo).returncode == 1

        fixed = _run(repo, "--fix")

        assert fixed.returncode == 0, fixed.stdout
        modes = {
            line.split("\t")[1]: line.split()[0]
            for line in _git(repo, "ls-files", "-s").stdout.splitlines()
        }
        assert modes["notes.md"] == "100644"
        assert modes["tool.py"] == "100755"
        assert _run(repo).returncode == 0

    def test_fix_writes_back_to_the_index_it_was_pointed_at(self, repo: Path):
        _git(repo, "init", "-q", ".", git_dir=".git2")
        _git(repo, "config", "core.bare", "false", git_dir=".git2")
        _git(repo, "add", "run.sh", "data.txt", git_dir=".git2")
        _git(repo, "update-index", "--chmod=-x", "run.sh", git_dir=".git2")

        fixed = _run(repo, "--fix", "--git-dir", ".git2")

        assert fixed.returncode == 0, fixed.stdout
        second = _git(repo, "ls-files", "-s", git_dir=".git2").stdout
        assert "100755" in [line.split()[0] for line in second.splitlines() if "run.sh" in line]


class TestTheHelpTextStillRenders:
    def test_help_prints_the_usage_and_exits_zero(self, repo: Path):
        result = _run(repo, "--help")

        assert result.returncode == 0
        assert "check-file-modes.sh" in result.stdout
        assert "--git-dir" in result.stdout
        assert "WHAT \"OK\" MEANS, EXACTLY" in result.stdout

    def test_an_unknown_argument_is_refused(self, repo: Path):
        result = _run(repo, "--wat")

        assert result.returncode == 2
        assert "unknown argument" in result.stderr
