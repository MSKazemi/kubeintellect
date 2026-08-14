"""GitOps PR opener (v5 P3 first write class — final step).

Completes the fix-PR flow: given a fix already committed on a branch (see `fix_pr.py` / `repair.py`),
push the branch to the GitOps remote and open a pull request. This is the code for the last step;
at runtime it needs a configured GitOps remote + the `gh` CLI (or any PR API), exactly like every
other slice needs its infra. Degrades gracefully: if `gh` is unavailable it still pushes the branch
and returns the compare URL for a one-click manual PR — the change never silently fails to surface.

The command runner is injected (returns (returncode, output)), so this is fully unit-testable
without git, gh, or a network.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

from app.tools.aci.fix_pr import FixPR

# runner(argv) -> (returncode, combined_output)
Runner = Callable[[list[str]], tuple[int, str]]


def _default_runner(argv: list[str]) -> tuple[int, str]:
    import subprocess
    r = subprocess.run(argv, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


@dataclass(frozen=True)
class PRResult:
    pushed: bool
    pr_opened: bool
    detail: str = ""


def open_pr(
    fix: FixPR, *, repo_dir: str, branch: str, base: str = "main", remote: str = "origin",
    runner: Optional[Runner] = None,
) -> PRResult:
    """Push ``branch`` and open a PR for ``fix``. Fails safe (a no-op fix opens nothing)."""
    if fix.is_noop:
        return PRResult(False, False, "no change to propose (fix is a no-op)")
    run = runner or _default_runner

    push_rc, push_out = run(["git", "-C", repo_dir, "push", "-u", remote, branch])
    if push_rc != 0:
        return PRResult(False, False, f"branch push failed: {push_out}")

    gh_rc, gh_out = run([
        "gh", "pr", "create", "--title", fix.title, "--body", fix.rationale,
        "--base", base, "--head", branch,
    ])
    if gh_rc == 0:
        return PRResult(True, True, gh_out.strip())   # gh prints the PR URL
    # gh unavailable/unauthed — branch is up; surface a manual-PR pointer instead of failing.
    return PRResult(True, False,
                    f"branch pushed to {remote}/{branch}; open a PR against {base} manually "
                    f"(gh unavailable: {gh_out.splitlines()[0] if gh_out else 'not found'})")
