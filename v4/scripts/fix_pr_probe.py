"""End-to-end misconfig fix-PR probe (v5 P3 first write class) — LIVE Azure + real git.

Demonstrates the whole write class off-cluster: a misconfigured manifest → LLM auto-repair (live
Azure) → PR-ready diff → a REAL git branch + commit (the PR content) in a scratch repo. The only
piece not exercised is `gh pr create`, which just needs a GitHub remote.

Run on n1:  uv run python scripts/fix_pr_probe.py   (needs AZURE_* in .env)
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

from app.tools.aci.fix_pr import make_fix_pr
from app.tools.aci.repair import propose_fix

FAIL: list[str] = []
_ORIG = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  replicas: 2\n"
    "  template:\n    spec:\n      securityContext:\n        runAsNonRoot: false\n"
    "      containers:\n        - name: web\n          image: nginx:1.27\n"
)
_VIOLATION = "CIS 5.2.6 / Kyverno require-run-as-non-root: securityContext.runAsNonRoot must be true"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (r.stdout + r.stderr).strip()


async def main() -> int:
    # 1. LLM auto-repair (live Azure)
    fixed = await propose_fix(_ORIG, _VIOLATION)
    check("repair changed the manifest", fixed != _ORIG)
    check("repair resolved the violation (runAsNonRoot: true)", "runasnonroot: true" in fixed.lower())
    check("repair preserved the workload (image kept)", "nginx:1.27" in fixed)

    # 2. package as a PR-ready diff
    pr = make_fix_pr("deploy.yaml", _ORIG, fixed,
                     title="fix(security): enforce runAsNonRoot", rationale=_VIOLATION)
    check("fix-PR has a non-empty diff", not pr.is_noop, f"+{pr.added_lines}/-{pr.removed_lines}")
    check("fix-PR classified declarative-revert", pr.rollback_class == "declarative-revert")

    # 3. materialize the PR as a real git branch + commit
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "probe@local")
        _git(repo, "config", "user.name", "probe")
        (repo / "deploy.yaml").write_text(_ORIG, encoding="utf-8")
        _git(repo, "add", "deploy.yaml")
        _git(repo, "commit", "-q", "-m", "base manifest")
        _git(repo, "checkout", "-q", "-b", "ki/fix-runasnonroot")
        (repo / "deploy.yaml").write_text(fixed, encoding="utf-8")
        _git(repo, "commit", "-q", "-am", pr.title)
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        log = _git(repo, "log", "--oneline", "-1")
        gitdiff = _git(repo, "diff", "HEAD~1", "HEAD", "--", "deploy.yaml")   # fix commit vs its base
        check("PR branch created + fix committed", branch == "ki/fix-runasnonroot" and "runAsNonRoot" in pr.title, log)
        check("git diff on the branch shows the security fix", "runAsNonRoot: true" in gitdiff)

    print("\n================ FIXED MANIFEST (excerpt) ================")
    for ln in fixed.splitlines():
        if "runAsNonRoot" in ln or "image:" in ln:
            print("  " + ln.strip())
    print(f"\n==== {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)} ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
