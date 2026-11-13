"""Auto-approve granted to an autonomous investigation applies in ITS namespace only.

The autonomy ladder decides, per namespace, whether a detector firing may mutate without a human (A3 plus
the allowlist). That decision became a boolean (`hitl_bypass`) in the run config, which the tool layer read
without any notion of WHERE the command lands. Measured 2026-11-12 with a real model on a real cluster: a
session opened for namespace `a3-1` (auto-approve granted) searched the cluster, found a crash-looping
deployment in `a2-1`, which the ladder pins to A2 (propose only), and patched it. The submitted paper said
an investigation "cannot widen its own authority"; at the tool layer it could.

`run_kubectl` now reads `bound_namespace` from the run config (set by the watchtower, injected by the graph,
never writable by a model). Outside that namespace the bypass does not apply and the command goes to the
approval prompt, which an unattended turn cannot answer, so nothing runs. Sessions with no bound namespace
(chat) are unchanged.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.tools.kubectl_tool import _stays_within_namespace, run_kubectl

MUTATING_VERBS = ("patch", "scale", "apply", "set", "rollout", "delete", "create")


def _run(command, *, bound="a3-1", bypass=True, stdin=None, role="admin", approve=True):
    ran: list[list[str]] = []

    def fake_run(argv, *a, **k):
        ran.append([str(x) for x in argv])
        proc = MagicMock()
        proc.stdout, proc.stderr, proc.returncode = "ok", "", 0
        return proc

    interrupt = MagicMock(return_value=approve)
    cfg = {"configurable": {"user_role": role, "hitl_bypass": bypass}}
    if bound:
        cfg["configurable"]["bound_namespace"] = bound
    with patch("app.tools.kubectl_tool.interrupt", interrupt), patch("subprocess.run", side_effect=fake_run):
        out = run_kubectl.invoke({"command": command, "stdin": stdin}, config=cfg)
    mutated = any(len(a) > 1 and any(v in a[1:4] for v in MUTATING_VERBS) and "get" not in a[1:3] for a in ran
                  if "-o" not in a[1:3])
    return interrupt, mutated, out


def _manifest(ns):
    return f"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: x\n  namespace: {ns}\nspec:\n  replicas: 1\n"


class TestInsideTheBoundNamespace:
    @pytest.mark.parametrize("cmd", [
        "kubectl patch deployment x -n a3-1 -p {}",
        "kubectl patch deployment x -na3-1 -p {}",
        "kubectl patch deployment x --namespace=a3-1 -p {}",
        "kubectl scale deployment x --replicas=2 -n A3-1",
        "kubectl rollout restart deployment/x -n a3-1",
    ])
    def test_auto_approve_still_applies(self, cmd):
        interrupt, mutated, _ = _run(cmd)
        interrupt.assert_not_called()
        assert mutated

    def test_a_manifest_that_declares_only_the_bound_namespace_is_inside(self):
        interrupt, mutated, _ = _run("kubectl apply -f -", stdin=_manifest("a3-1"))
        interrupt.assert_not_called()
        assert mutated


class TestOutsideTheBoundNamespace:
    @pytest.mark.parametrize("cmd", [
        "kubectl patch deployment x -n a2-1 -p {}",
        "kubectl scale deployment x --replicas=0 -n decoy-1",
        "kubectl scale deployment x --replicas=0",          # names no namespace: kubectl's default
        "kubectl rollout restart deployment/x",
    ])
    def test_the_bypass_does_not_apply_and_the_approval_prompt_appears(self, cmd):
        interrupt, mutated, _ = _run(cmd, approve=False)
        interrupt.assert_called_once()
        assert not mutated

    @pytest.mark.parametrize("cmd", [
        "kubectl scale deployment x --replicas=0 -A",
        "kubectl scale deployment x --replicas=0 --all-namespaces",
    ])
    def test_a_write_across_every_namespace_never_runs(self, cmd):
        # Refused outright by the existing protected-namespace guard (stricter than a prompt); the bound
        # namespace rule must not loosen that.
        _, mutated, _ = _run(cmd, approve=True)
        assert not mutated

    def test_a_manifest_for_another_namespace_is_outside(self):
        interrupt, mutated, _ = _run("kubectl apply -f -", stdin=_manifest("a2-1"), approve=False)
        interrupt.assert_called_once()
        assert not mutated

    def test_a_manifest_naming_no_namespace_is_outside(self):
        no_ns = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: x\nspec:\n  replicas: 1\n"
        interrupt, mutated, _ = _run("kubectl apply -f -", stdin=no_ns, approve=False)
        interrupt.assert_called_once()
        assert not mutated

    def test_a_human_who_approves_can_still_run_it(self):
        interrupt, mutated, _ = _run("kubectl patch deployment x -n a2-1 -p {}", approve=True)
        interrupt.assert_called_once()
        assert mutated

    def test_a_human_who_declines_stops_it(self):
        interrupt, mutated, out = _run("kubectl patch deployment x -n a2-1 -p {}", approve=False)
        interrupt.assert_called_once()
        assert not mutated and "cancelled" in out.lower()


class TestOtherSessionsAreUnchanged:
    def test_an_unbound_session_keeps_its_auto_approve(self):
        interrupt, mutated, _ = _run("kubectl patch deployment x -n a2-1 -p {}", bound=None)
        interrupt.assert_not_called()
        assert mutated

    def test_a_gated_session_is_still_gated_inside_its_namespace(self):
        interrupt, _, _ = _run("kubectl patch deployment x -n a3-1 -p {}", bypass=False, approve=False)
        interrupt.assert_called_once()

    def test_reads_outside_the_namespace_are_not_gated(self):
        interrupt, _, _ = _run("kubectl get pods -n a2-1")
        interrupt.assert_not_called()


class TestTheBoundaryItself:
    @pytest.mark.parametrize("verb,args,stdin,expected", [
        ("patch", ["kubectl", "patch", "deployment", "x", "-n", "a3-1"], None, True),
        ("patch", ["kubectl", "patch", "deployment", "x", "-n", "a2-1"], None, False),
        ("patch", ["kubectl", "patch", "deployment", "x"], None, False),
        ("patch", ["kubectl", "patch", "deployment", "x", "-A"], None, False),
        ("apply", ["kubectl", "apply", "-f", "-"], _manifest("a3-1"), True),
        ("apply", ["kubectl", "apply", "-f", "-"], _manifest("a2-1"), False),
        ("apply", ["kubectl", "apply", "-f", "-", "-n", "a3-1"], _manifest("a2-1"), False),
        ("delete", ["kubectl", "delete", "node", "n1"], None, False),
    ])
    def test_the_rule(self, verb, args, stdin, expected):
        assert _stays_within_namespace(verb, args, stdin, "a3-1") is expected
