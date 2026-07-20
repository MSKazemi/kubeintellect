"""GitOps PR opener (v5 P3) — push branch + open PR, graceful degradation."""
from __future__ import annotations

from app.tools.aci.fix_pr import make_fix_pr
from app.tools.aci.gitops import open_pr

_ORIG = "kind: Deployment\nspec:\n  runAsNonRoot: false\n"
_FIXED = "kind: Deployment\nspec:\n  runAsNonRoot: true\n"


def _fix():
    return make_fix_pr("deploy.yaml", _ORIG, _FIXED, title="fix: runAsNonRoot", rationale="CIS 5.2.6")


class _Runner:
    """Records argv; returns preset (rc, out) per command head."""
    def __init__(self, results):
        self.results = results        # {"push": (0, "..."), "gh": (0, "url")}
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        head = "gh" if argv[0] == "gh" else "push"
        return self.results.get(head, (0, ""))


class TestOpenPr:
    def test_push_and_open_pr(self):
        r = _Runner({"push": (0, "ok"), "gh": (0, "https://github.com/x/y/pull/1")})
        res = open_pr(_fix(), repo_dir="/tmp/repo", branch="ki/fix", runner=r)
        assert res.pushed and res.pr_opened
        assert "pull/1" in res.detail
        assert any(a[0] == "git" and "push" in a for a in r.calls)
        assert any(a[0] == "gh" for a in r.calls)

    def test_push_failure_stops(self):
        r = _Runner({"push": (1, "permission denied")})
        res = open_pr(_fix(), repo_dir="/tmp/repo", branch="ki/fix", runner=r)
        assert res.pushed is False and res.pr_opened is False and "push failed" in res.detail
        assert not any(a[0] == "gh" for a in r.calls)   # never tried gh after a failed push

    def test_gh_unavailable_still_pushes(self):
        r = _Runner({"push": (0, "ok"), "gh": (127, "gh: command not found")})
        res = open_pr(_fix(), repo_dir="/tmp/repo", branch="ki/fix", runner=r)
        assert res.pushed is True and res.pr_opened is False
        assert "manually" in res.detail and "branch pushed" in res.detail

    def test_noop_fix_opens_nothing(self):
        noop = make_fix_pr("deploy.yaml", _ORIG, _ORIG, title="", rationale="")
        r = _Runner({})
        res = open_pr(noop, repo_dir="/tmp/repo", branch="ki/fix", runner=r)
        assert res.pushed is False and res.pr_opened is False and r.calls == []
