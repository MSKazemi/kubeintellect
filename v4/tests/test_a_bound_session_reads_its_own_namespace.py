"""A read in a bound session looks in the session's namespace when the command names none.

Measured 2026-11-14 (`papers/v4-operator-r1/A3-LOOP-INVESTIGATION.md` §2b): an autonomous session bound to `h3-2`
ran `kubectl get job report -o yaml` without `-n`, so kubectl looked in its default namespace and answered NotFound
for a Job that existed in `h3-2`. The model concluded the Job was gone and spent 73 cycles "recreating" it. The
namespace binding constrained only writes, so nothing put the read where the investigation was.

Writes are deliberately NOT given a namespace: a write that names none (or a cluster-scoped object) must still fail
the binding check and stop at the approval prompt, which an injected `-n` would hide.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from app.tools import kubectl_tool as kt


def _run(
    command: str,
    *,
    bound: str | None = "h3-2",
    bypass: bool = True,
    stdin: str | None = None,
):
    calls: list[list[str]] = []
    prompts: list[dict] = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    cfg: dict = {
        "configurable": {
            "user_role": "operator",
            "hitl_bypass": bypass,
            "thread_id": "auto-t",
        }
    }
    if bound:
        cfg["configurable"]["bound_namespace"] = bound
    with (
        mock.patch.object(kt.subprocess, "run", fake_run),
        mock.patch.object(kt, "interrupt", lambda p: prompts.append(p) or False),
        mock.patch.object(kt, "_capture_rollback_point", lambda *a, **k: None),
    ):
        out = kt.run_kubectl.invoke({"command": command, "stdin": stdin}, config=cfg)
    return calls, prompts, out


class TestAReadWithoutANamespaceLooksInTheBoundOne:
    def test_the_e3_read_runs_in_the_bound_namespace(self):
        calls, _, out = _run("kubectl get job report -o yaml")
        argv = calls[-1]
        assert argv[argv.index("-n") + 1] == "h3-2"
        assert (
            "h3-2" in out and "named none" in out
        )  # the model is told where it looked

    def test_describe_and_logs_too(self):
        for cmd in (
            "kubectl describe job report",
            "kubectl logs -l job-name=report --tail=5",
        ):
            calls, _, _ = _run(cmd)
            assert calls[-1][calls[-1].index("-n") + 1] == "h3-2", cmd


class TestWhatIsLeftAlone:
    def test_an_explicit_namespace_is_kept(self):
        calls, _, out = _run("kubectl get job report -n other")
        argv = calls[-1]
        assert argv.count("-n") == 1 and argv[argv.index("-n") + 1] == "other"
        assert "named none" not in out

    def test_attached_namespace_spelling_is_kept(self):
        calls, _, _ = _run("kubectl get pods -nother")
        assert "-n" not in calls[-1] and "-nother" in calls[-1]

    def test_all_namespaces_is_kept(self):
        calls, _, _ = _run("kubectl get pods -A")
        assert "-n" not in calls[-1]

    def test_an_unbound_chat_session_is_unchanged(self):
        calls, _, out = _run("kubectl get job report -o yaml", bound=None, bypass=False)
        assert "-n" not in calls[-1] and "named none" not in out

    def test_a_write_without_a_namespace_is_not_given_one_and_still_needs_a_human(self):
        # The binding must keep failing closed for writes that name no namespace.
        calls, prompts, out = _run("kubectl label node worker-1 role=x")
        assert prompts, (
            "a write that names no namespace must stop at the approval prompt"
        )
        assert not calls, "and must not run"
        assert out == "Action cancelled by user."

    def test_a_manifest_write_without_a_namespace_still_needs_a_human(self):
        manifest = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app-config\ndata:\n  k: v\n"
        calls, prompts, _ = _run("kubectl apply -f -", stdin=manifest)
        assert prompts and not calls
