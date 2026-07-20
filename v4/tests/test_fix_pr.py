"""Misconfig fix-PR generator (v5 P3) — PR-ready diff + metadata."""
from __future__ import annotations

from app.tools.aci.fix_pr import FixPR, make_fix_pr, unified_diff

_ORIG = "apiVersion: apps/v1\nkind: Deployment\nspec:\n  securityContext:\n    runAsNonRoot: false\n"
_FIXED = "apiVersion: apps/v1\nkind: Deployment\nspec:\n  securityContext:\n    runAsNonRoot: true\n"


class TestUnifiedDiff:
    def test_shows_change(self):
        d = unified_diff("deploy.yaml", _ORIG, _FIXED)
        assert "a/deploy.yaml" in d and "b/deploy.yaml" in d
        assert "-    runAsNonRoot: false" in d and "+    runAsNonRoot: true" in d

    def test_identical_is_empty(self):
        assert unified_diff("x.yaml", _ORIG, _ORIG) == ""


class TestMakeFixPr:
    def test_builds_pr(self):
        pr = make_fix_pr("deploy.yaml", _ORIG, _FIXED,
                         title="fix: enforce runAsNonRoot",
                         rationale="CIS 5.2.6 — containers must not run as root")
        assert isinstance(pr, FixPR)
        assert pr.is_noop is False
        assert pr.rollback_class == "declarative-revert"
        assert pr.added_lines == 1 and pr.removed_lines == 1
        assert "runAsNonRoot" in pr.title and "CIS" in pr.rationale

    def test_noop_change(self):
        pr = make_fix_pr("deploy.yaml", _ORIG, _ORIG, title="", rationale="")
        assert pr.is_noop is True
        assert pr.title == "fix: deploy.yaml"      # default title when blank

    def test_default_title_when_blank(self):
        pr = make_fix_pr("a/b.yaml", _ORIG, _FIXED, title="   ", rationale="r")
        assert pr.title == "fix: a/b.yaml"
