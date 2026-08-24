"""Gate: the CI lint step reads every tracked Python file, not a corner of the tree.

Until 2026-08-24 the `ruff check` step named exactly two paths — `packages/kubeintellect-server/app/`
and `packages/ki-protocol/`. Everything else was invisible to it: all 167 files under `tests/`, the
whole `kube-q` CLI, and every build script. A contributor adding a test file got a green lint job
that had not read their file, which is the same defect as a gate that examines nothing, scoped down
to *most* of the tree instead of all of it. Widening it cost 31 fixes — 25 semicolon statements,
5 long lines and one deliberate late import — which is roughly what "nobody has looked since the
beginning" is worth.

The check is coverage, not a fixed list: a new top-level package fails this test until the step is
widened, and the step may be reorganized freely as long as every tracked file stays inside it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

_V4 = Path(__file__).resolve().parents[1]
_CI = _V4.parent / ".github" / "workflows" / "ci.yml"


def _lint_paths() -> list[str]:
    """The paths the CI `ruff check` step actually passes to ruff."""
    workflow = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["lint"]["steps"]
    run = next(s["run"] for s in steps if s.get("name") == "ruff check")
    tokens = run.split()
    assert "check" in tokens, f"the lint step no longer runs `ruff check`: {run!r}"
    return [t for t in tokens[tokens.index("check") + 1:] if not t.startswith("-")]


def _tracked_python() -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--", "."], cwd=_V4, capture_output=True, text=True, timeout=60
    )
    return [name for name in proc.stdout.split("\0") if name.endswith(".py")]


class TestTheCheckItselfIsNotVacuous:
    def test_the_step_was_found_and_names_paths(self):
        paths = _lint_paths()
        assert paths, "no paths parsed out of the CI lint step — this gate would pass on anything"

    def test_the_repository_was_actually_listed(self):
        tracked = _tracked_python()
        assert len(tracked) > 300, f"only {len(tracked)} tracked .py files found — git listing failed"


class TestEveryTrackedFileIsLinted:
    def test_no_tracked_python_file_sits_outside_a_linted_root(self):
        roots = [p.rstrip("/") for p in _lint_paths()]
        uncovered = sorted(
            name for name in _tracked_python()
            if not any(name == r or name.startswith(r + "/") for r in roots)
        )
        assert not uncovered, (
            f"{len(uncovered)} tracked Python file(s) are never seen by the CI lint job — "
            f"widen the `ruff check` step in .github/workflows/ci.yml: {uncovered[:10]}"
        )

    @pytest.mark.parametrize("expected", ["tests/", "scripts/", "packages/kube-q/"])
    def test_the_roots_that_were_missing_are_named(self, expected):
        """Named explicitly so a "tidy-up" that drops one of them fails loudly."""
        assert expected in _lint_paths()


class TestMakeLintStillPredictsCI:
    """AGENTS.md tells contributors to use the `ruff check` command to predict CI. Keep it true."""

    def test_the_makefile_runs_the_same_paths(self):
        recipe = next(
            ln for ln in (_V4 / "Makefile").read_text(encoding="utf-8").splitlines()
            if "ruff check" in ln
        )
        missing = [p for p in _lint_paths() if p not in recipe]
        assert not missing, f"`make lint` does not cover what CI covers: {missing}"
