"""Gate: the suite can be run against exactly what a public checkout carries.

This repository is dual-git — `.git` tracks the published subset and a second
index tracks a superset that also carries the private research materials the root
`.gitignore` names. Every local gate runs against the WORKING TREE, which is the
superset. So a test can read a private-tier file, be green on this machine, and be
red on `main` — which has now happened twice (`74ac4cf`, and again three tests
later).

`scripts/check-public-checkout.sh` removes the guesswork: it exports HEAD into a
throwaway directory and runs the gates there, so they see what GitHub sees.

The one non-obvious requirement, and the reason this file asserts it: the export
must be a real git checkout. Twelve tests in this suite decide what to scan from
`git ls-files`, and a bare `tar -x` of `git archive` has no index at all — measured,
running the suite in one reports **twelve failures that do not exist on `main`**.
The export is committed for the same reason: `actions/checkout` gives CI a real
HEAD, so an export without one is less faithful than the clone it stands in for.
An instrument that manufactures its own findings is worse than no instrument.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check-public-checkout.sh"

_PRIVATE_INDEX = _REPO_ROOT / ".git-private"


def _private_only_paths() -> list[str]:
    """Paths the superset tracks and the published index does not.

    Derived, never enumerated. A hardcoded list drifts the moment a directory is
    added, and writing the private directory names into a published file would
    disclose the very thing this test exists to keep out of the export.
    """
    priv = subprocess.run(
        ["git", f"--git-dir={_PRIVATE_INDEX}", f"--work-tree={_REPO_ROOT}",
         "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    private = {line for line in priv.stdout.splitlines() if line}
    return sorted(private - set(_tracked_at_head()))


def _tracked_at_head() -> list[str]:
    # ls-tree, not ls-files: the export is of HEAD, and ls-files would report the
    # index — which differs the moment anything is staged.
    out = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(line for line in out.stdout.splitlines() if line)


@pytest.fixture(scope="module")
def private_only():
    """Paths the superset tracks and the published index does not."""
    if not _PRIVATE_INDEX.exists():
        pytest.skip("the private index is not in this checkout; nothing to compare")
    paths = _private_only_paths()
    # Vacuity guard: an empty difference would make every assertion below free.
    assert len(paths) > 100, f"suspiciously small private-only set: {len(paths)}"
    return paths


@pytest.fixture(scope="module")
def export_dir():
    """One export, shared — `git archive` of the whole tree is not free."""
    if not _SCRIPT.exists():
        pytest.fail(f"{_SCRIPT.relative_to(_REPO_ROOT)} does not exist")
    with tempfile.TemporaryDirectory(prefix="ki-public-checkout-") as tmp:
        target = Path(tmp) / "tree"
        proc = subprocess.run(
            ["bash", str(_SCRIPT), "--export-only", str(target)],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (
            f"--export-only failed ({proc.returncode})\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
        yield target


class TestTheScriptIsRunnableAtAll:
    def test_it_exists(self):
        assert _SCRIPT.exists(), f"missing {_SCRIPT}"

    def test_it_is_executable(self):
        # The file-mode gate requires shebang <-> +x agreement; a non-executable
        # checker is one nobody runs.
        assert _SCRIPT.stat().st_mode & 0o111, f"{_SCRIPT.name} is not executable"

    def test_it_parses(self):
        proc = subprocess.run(["bash", "-n", str(_SCRIPT)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


class TestItRunsWhereGitHasNoConfiguredIdentity:
    """The runner has no `user.name`, and `git commit` refuses without one.

    This is the same defect the file is named for, pointed at the instrument
    itself: the export step reads the source repository's identity so its commit
    passes the guard hook, and on a maintainer's machine that always resolves. On
    a CI runner `git var GIT_AUTHOR_IDENT` exits 128, and the first version of
    this test was green locally and red on `main` for exactly that reason.
    """

    def test_the_export_still_builds(self, tmp_path):
        env = dict(os.environ)
        # Scrub every layer git would read an identity from — this is what a fresh
        # runner looks like, and nothing else reproduces it.
        env.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "HOME": str(tmp_path / "home"),
            }
        )
        # EMPTY, not absent. Scrubbing config alone is not enough — git falls back to
        # the passwd GECOS name, which a developer machine has and a runner does not,
        # and that difference is what made the first version of this test pass here
        # and fail on `main`. An empty GIT_AUTHOR_NAME reproduces the runner's exact
        # "fatal: empty ident name" and wins over any config the script sets.
        for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            env[key] = ""
        (tmp_path / "home").mkdir()

        proc = subprocess.run(
            ["bash", str(_SCRIPT), "--export-only", str(tmp_path / "tree")],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, (
            f"the export needs a configured git identity, which CI does not have "
            f"({proc.returncode})\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path / "tree",
            capture_output=True,
            text=True,
            env=env,
        )
        assert head.returncode == 0, head.stderr


class TestTheExportIsAFaithfulCheckout:
    def test_it_is_not_empty(self, export_dir):
        # Vacuity guard: every assertion below would pass for free on an empty tree.
        assert (export_dir / "README.md").is_file()
        assert (export_dir / "v4" / "tests").is_dir()

    def test_it_is_a_real_git_checkout(self, export_dir):
        # Without this, twelve `git ls-files`-driven tests fail for a reason that
        # exists only in the instrument. See the module docstring.
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=export_dir,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "true"

    def test_it_resolves_head(self, export_dir):
        # `actions/checkout` gives CI a real HEAD. An export with only an index is
        # LESS faithful than the clone it stands in for, and anything that resolves
        # HEAD — this file's own export step among them — fails there and nowhere else.
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=export_dir,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"the export has no HEAD:\n{proc.stderr}"
        assert len(proc.stdout.strip()) == 40

    def test_its_index_is_the_one_head_carries(self, export_dir):
        expected = _tracked_at_head()
        assert len(expected) > 1000, f"suspiciously small index at HEAD: {len(expected)}"

        out = subprocess.run(
            ["git", "ls-files"],
            cwd=export_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        got = sorted(line for line in out.stdout.splitlines() if line)

        missing = sorted(set(expected) - set(got))
        extra = sorted(set(got) - set(expected))
        assert not missing, f"{len(missing)} tracked path(s) absent from the export index: {missing[:10]}"
        assert not extra, f"{len(extra)} path(s) in the export index that HEAD does not track: {extra[:10]}"


class TestNoPrivateTierPathReachesTheExport:
    """Skipped in a public checkout — there is no superset there to compare against.

    That is the same honest skip the rest of the suite uses for private-tier
    assertions, and it is why this file's other two classes carry the checks that
    a public CI run can actually make.
    """

    def test_no_private_only_path_exists_in_the_export(self, export_dir, private_only):
        leaked = [p for p in private_only if (export_dir / p).exists()]
        assert not leaked, (
            f"{len(leaked)} private-tier path(s) reached a public checkout: {leaked[:10]}"
        )

    def test_no_private_only_path_is_in_the_export_index(self, export_dir, private_only):
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=export_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        indexed = {line for line in out.stdout.splitlines() if line}
        leaked = sorted(indexed & set(private_only))
        assert not leaked, f"private-tier paths in the export index: {leaked[:10]}"
