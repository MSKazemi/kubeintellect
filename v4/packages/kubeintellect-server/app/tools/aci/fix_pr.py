"""Misconfig fix-PR generator (v5 P3 first write class, A-CH-04-02).

The lowest-blast-radius write class: a misconfig fix emitted as a GitOps **pull request** behind the
chokepoint — declarative-revert (revert = don't merge / revert the commit), never a live mutation.
This module is the deterministic PR-packaging core: given the original manifest and a corrected one
(the repair itself — LLM auto-repair — is pluggable and out of scope here), it produces a unified
diff + PR metadata ready to open. Actually pushing the PR needs a target repo and is gated.

Pure/deterministic (stdlib difflib) — fully unit-testable, no repo, no LLM.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass(frozen=True)
class FixPR:
    path: str
    diff: str                 # unified diff, '' if no change
    title: str
    rationale: str
    rollback_class: str = "declarative-revert"

    @property
    def is_noop(self) -> bool:
        return not self.diff.strip()

    @property
    def added_lines(self) -> int:
        return sum(1 for ln in self.diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))

    @property
    def removed_lines(self) -> int:
        return sum(1 for ln in self.diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))


def unified_diff(path: str, original: str, fixed: str) -> str:
    """A git-style unified diff between two manifest texts."""
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        fixed.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    )
    return "".join(diff)


def make_fix_pr(path: str, original: str, fixed: str, *, title: str, rationale: str) -> FixPR:
    """Package a misconfig fix as a PR-ready diff + metadata. A no-op change yields is_noop=True."""
    return FixPR(
        path=path,
        diff=unified_diff(path, original, fixed),
        title=title.strip() or f"fix: {path}",
        rationale=rationale.strip(),
    )
