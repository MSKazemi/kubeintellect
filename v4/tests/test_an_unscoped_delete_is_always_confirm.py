"""`kubectl delete --all` / `-A` is always-confirm, even on an auto-approve session.

Only deletes whose TARGET is a namespace, pv, crd or a drain were always-confirm. Measured 2026-11-12 on a
real cluster through `run_kubectl` with `hitl_bypass=True`: `kubectl delete pods --all -n <ns>` ran with no
prompt and replaced every pod in the namespace, while `kubectl delete namespace <ns>` stopped and asked. Same
blast radius to the workload, one flag apart.

Scope of this rule (a decision, recorded so it is not mistaken for an oversight):
* `--all` and `-A` / `--all-namespaces` on a delete: always-confirm.
* A delete that NAMES an object, or narrows by a label selector (`-l`), still honours auto-approve. A
  selector-scoped delete is how many routine remediations ("restart the pods of this deployment") are
  written, and an unattended A3 turn cannot answer a prompt, so confirming those would turn A3 off for them.
  Whether selector deletes should also be confirmed, capped by object count, or refused is left to the owner.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.tools.kubectl_tool import _is_unscoped_delete, _requires_always_confirm, run_kubectl


def _invoke(command, hitl_bypass=True, user_role="admin"):
    captured = {}

    def fake_interrupt(value):
        captured.update(value)
        return True

    proc = MagicMock()
    proc.stdout, proc.stderr, proc.returncode = "ok", "", 0
    cfg = {"configurable": {"user_role": user_role, "hitl_bypass": hitl_bypass}}
    with patch("app.tools.kubectl_tool.interrupt", side_effect=fake_interrupt) as mock_intr:
        with patch("subprocess.run", return_value=proc) as mock_run:
            run_kubectl.invoke({"command": command}, config=cfg)
    return captured, mock_intr, mock_run


@pytest.mark.parametrize("cmd", [
    "kubectl delete pods --all -n default",
    "kubectl delete pods --all=true -n default",
    "kubectl delete pods -n default --all",
    "kubectl delete all --all -n default",
])
def test_an_unscoped_delete_overrides_auto_approve(cmd):
    captured, mock_intr, _ = _invoke(cmd, hitl_bypass=True)
    mock_intr.assert_called_once()
    assert captured.get("always_confirm") is True
    assert captured.get("risk_level") == "high"


@pytest.mark.parametrize("cmd", [
    "kubectl delete deployments -A",
    "kubectl delete pods --all-namespaces",
])
def test_a_cluster_wide_delete_is_refused_before_it_can_reach_auto_approve(cmd):
    # Already true before this change: the protected-namespace guard refuses a write that spans every
    # namespace. Pinned here so a future refactor of that guard cannot quietly reopen the cluster-wide form.
    _, mock_intr, mock_run = _invoke(cmd, hitl_bypass=True)
    mock_intr.assert_not_called()
    assert not any("delete" in str(c.args[0]) for c in mock_run.call_args_list if c.args)


@pytest.mark.parametrize("cmd", [
    "kubectl delete pod foo -n default",
    "kubectl delete pods foo bar -n default",
    "kubectl delete pods -l app=web -n default",
    "kubectl delete pods --selector=app=web -n default",
])
def test_a_named_or_selector_scoped_delete_still_honours_auto_approve(cmd):
    _, mock_intr, _ = _invoke(cmd, hitl_bypass=True)
    mock_intr.assert_not_called()


def test_an_explicit_false_is_not_an_unscoped_delete():
    assert _is_unscoped_delete(["kubectl", "delete", "pods", "--all=false", "-n", "x"]) is False
    assert _requires_always_confirm("delete", ["kubectl", "delete", "pods", "--all=false", "-n", "x"]) is False


def test_other_verbs_are_not_affected_by_a_flag_named_all():
    # `kubectl get --all-namespaces` is a read; `kubectl logs --all-containers` is not `--all`.
    assert _requires_always_confirm("get", ["kubectl", "get", "pods", "-A"]) is False
    assert _requires_always_confirm("logs", ["kubectl", "logs", "p", "--all-containers"]) is False
