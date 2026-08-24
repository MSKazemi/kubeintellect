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
    # Non-empty ⇒ the repair step produced nothing, so this PR is empty because the fix FAILED,
    # not because the manifest was already fine. `is_noop` cannot tell those apart on its own and
    # `open_pr` used to report both as "no change to propose".
    repair_failed_reason: str = ""

    @property
    def is_noop(self) -> bool:
        return not self.diff.strip()

    @property
    def added_lines(self) -> int:
        return sum(1 for ln in self.diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))

    @property
    def removed_lines(self) -> int:
        return sum(1 for ln in self.diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))


def _normalised(text: str) -> str:
    """Manifest text with exactly one trailing newline, or empty if there is no content.

    The repair step ends in `.strip()` (it has to, to remove ``` fences), so a model that
    **echoed the manifest back** — its way of saying nothing needs changing — came back one
    trailing newline shorter than it went in. Measured 2026-08-24 that produced a real diff:
    one line removed, the same line added, `is_noop` False, and `open_pr` pushed a branch and
    opened a pull request titled as a security fix whose entire content was a missing newline.
    A trailing newline is not a change to a manifest, and this is a PR opener.
    """
    return text.rstrip("\n") + "\n" if text.strip() else ""


def unified_diff(path: str, original: str, fixed: str) -> str:
    """A git-style unified diff between two manifest texts, ignoring trailing-newline drift."""
    diff = difflib.unified_diff(
        _normalised(original).splitlines(keepends=True),
        _normalised(fixed).splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    )
    return "".join(diff)


def make_fix_pr(
    path: str, original: str, fixed: str, *, title: str, rationale: str,
    repair_failed_reason: str = "",
) -> FixPR:
    """Package a misconfig fix as a PR-ready diff + metadata. A no-op change yields is_noop=True.

    Pass ``repair_failed_reason`` (from ``RepairProposal.reason``) when the repair step produced
    nothing, so the empty diff below can be told apart from a manifest that needed no change.
    """
    return FixPR(
        path=path,
        diff=unified_diff(path, original, fixed),
        title=title.strip() or f"fix: {path}",
        rationale=rationale.strip(),
        repair_failed_reason=repair_failed_reason.strip(),
    )
