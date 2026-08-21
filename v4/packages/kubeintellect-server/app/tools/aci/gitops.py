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

from app.tools.aci.fix_pr import FixPR

# runner(argv) -> (returncode, combined_output)
Runner = Callable[[list[str]], tuple[int, str]]


# Both commands this module runs can block forever rather than fail: `git push` waits on a
# credential prompt that will never be answered (no tty), stalls on a half-open connection, or
# blocks on a stale index.lock; `gh pr create` waits on auth. Unbounded, that hangs the calling
# request with no upper bound — the worst failure shape for an incident-response tool. The
# timeout converts "hang" into an ordinary non-zero result, which `open_pr` already degrades
# gracefully from.
_DEFAULT_TIMEOUT_S = 60


def _default_runner(argv: list[str]) -> tuple[int, str]:
    import subprocess
    try:
        r = subprocess.run(
            argv, capture_output=True, text=True, timeout=_DEFAULT_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        # Non-zero + a readable reason: push failure surfaces to the caller, and a `gh` timeout
        # falls through to the manual-PR pointer rather than losing the pushed branch.
        return 124, f"timed out after {_DEFAULT_TIMEOUT_S}s: {' '.join(argv[:3])}"
    return r.returncode, (r.stdout + r.stderr).strip()


@dataclass(frozen=True)
class PRResult:
    pushed: bool
    pr_opened: bool
    detail: str = ""


def open_pr(
    fix: FixPR, *, repo_dir: str, branch: str, base: str = "main", remote: str = "origin",
    runner: Runner | None = None,
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
