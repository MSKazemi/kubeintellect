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


class TestDefaultRunnerTimeout:
    """`_default_runner` must bound its own subprocesses.

    Both commands this module shells out to can block forever rather than fail — `git push`
    waits on a credential prompt that will never be answered (there is no tty), on a half-open
    connection, or on a stale `index.lock`; `gh pr create` waits on auth. Unbounded, that hangs
    the calling request with no upper limit, which is the worst failure shape for an
    incident-response tool: it fails exactly when someone needs an answer.

    These assert the bound exists and that a timeout degrades into an ordinary non-zero result,
    so `open_pr`'s existing graceful-degradation paths keep working.
    """

    def test_runner_passes_a_timeout(self, monkeypatch):
        import subprocess

        seen = {}

        def _fake_run(argv, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        from app.tools.aci import gitops

        rc, out = gitops._default_runner(["git", "push"])
        assert rc == 0 and out == "ok"
        assert seen.get("timeout"), (
            "_default_runner ran a subprocess with no timeout — `git push` can block "
            "indefinitely on a credential prompt or a stale lock"
        )

    def test_timeout_becomes_a_nonzero_result_not_an_exception(self, monkeypatch):
        import subprocess

        def _boom(argv, **kw):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout", 1))

        monkeypatch.setattr(subprocess, "run", _boom)
        from app.tools.aci import gitops

        rc, out = gitops._default_runner(["git", "-C", "/repo", "push"])
        assert rc != 0, "a timeout must surface as a failure, not propagate as an exception"
        assert "timed out" in out.lower()

    def test_push_timeout_reports_push_failure(self, monkeypatch):
        """End-to-end through open_pr: a hung push must not claim the branch was pushed."""
        import subprocess

        monkeypatch.setattr(
            subprocess, "run",
            lambda argv, **kw: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(cmd=argv, timeout=1)
            ),
        )
        res = open_pr(_fix(), repo_dir="/repo", branch="fix/x")
        assert res.pushed is False
        assert res.pr_opened is False
        assert "timed out" in res.detail.lower()
