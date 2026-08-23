"""Whether a command may run is decided by "is this a write", not by a list of remembered writes.

The risk tables were a deny-list: `_HIGH_RISK | _MEDIUM_RISK` named 13 verbs, and every kubectl
verb they did not name was treated as a harmless read. kubectl has more verbs than that.
Measured 2026-08-20 through the real tool with a **read-only** API key, all of these executed
with no approval prompt:

    kubectl label / annotate …          mutate live objects
    kubectl rollout restart|undo …      restart every pod / roll back a release
    kubectl cp prod/api-1:/etc/creds …  copy files out of a container, mounted Secrets included
    kubectl debug node/node-1 …         ephemeral privileged pod on the node
    kubectl expose / autoscale …        create Services and HPAs
    kubectl port-forward / attach …     reach an internal service, or a running container

The default is now inverted: `_READ_ONLY_VERBS` is an allowlist and anything absent is a write.
A kubectl release that adds a verb fails closed rather than arriving pre-approved.

Over-blocking is the opposite failure and is tested just as hard — a gate that stops ordinary
reads gets switched off, and `rollout status` must stay available to a read-only key even though
`rollout restart` must not.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.tools.aci.bounds import is_read_only
from app.tools.kubectl_tool import _classify_risk, _extract_verb, _is_write_verb, run_kubectl

_READONLY = {"configurable": {"user_role": "readonly", "thread_id": "t"}}
_ADMIN = {"configurable": {"user_role": "admin", "thread_id": "t"}}

_WRITES = [
    "kubectl label pod api-1 env=prod --overwrite -n prod",
    "kubectl annotate deployment api note=x --overwrite -n prod",
    "kubectl rollout restart deployment/api -n prod",
    "kubectl rollout undo deployment/api -n prod",
    "kubectl rollout pause deployment/api -n prod",
    "kubectl cp prod/api-1:/etc/creds /tmp/creds",
    "kubectl debug node/node-1 -it --image=busybox",
    "kubectl expose deployment api --port=80 -n prod",
    "kubectl autoscale deployment api --max=10 -n prod",
    "kubectl port-forward svc/db 5432:5432 -n prod",
    "kubectl attach api-1 -it -n prod",
]

_READS = [
    "kubectl get pods -n prod",
    "kubectl describe deployment api -n prod",
    "kubectl logs api-1 -n prod",
    "kubectl top pods -n prod",
    "kubectl events -n prod",
    "kubectl rollout status deployment/api -n prod",
    "kubectl rollout history deployment/api -n prod",
    "kubectl auth can-i get pods",
    "kubectl api-resources",
    "kubectl explain pod",
    "kubectl version",
    "kubectl cluster-info",
]


def _invoke(command: str, config: dict) -> tuple[str, bool, bool]:
    with patch("subprocess.run") as run:
        proc = MagicMock()
        proc.stdout, proc.stderr, proc.returncode = "<DONE>", "", 0
        run.return_value = proc
        try:
            out, hitl = str(run_kubectl.invoke({"command": command, "stdin": None},
                                               config=config)), False
        except KeyError as exc:
            if "__pregel_scratchpad" not in str(exc):
                raise
            out, hitl = "", True
        return out, run.called, hitl


class TestAReadOnlyKeyCannotWriteAtAll:
    @pytest.mark.parametrize("command", _WRITES)
    def test_the_write_is_refused(self, command):
        out, ran, hitl = _invoke(command, _READONLY)
        assert "[Permission Denied]" in out, f"{command!r} was permitted: {out[:150]}"
        assert not ran and not hitl


class TestReadsAreNotCollateralDamage:
    @pytest.mark.parametrize("command", _READS)
    def test_the_read_still_runs_for_a_readonly_key(self, command):
        out, ran, hitl = _invoke(command, _READONLY)
        assert ran, f"{command!r} was blocked: {out[:150]}"
        assert not hitl and "[Permission Denied]" not in out


class TestWritesStillReachTheApprovalGateForAnAdmin:
    @pytest.mark.parametrize("command", _WRITES)
    def test_an_admin_is_asked_first(self, command):
        out, ran, hitl = _invoke(command, _ADMIN)
        assert hitl, f"{command!r} executed without an approval prompt: {out[:150]}"
        assert not ran


class TestTheDefaultIsWrite:
    def test_an_unknown_verb_is_treated_as_a_write(self):
        """A verb from a future kubectl release must not arrive pre-approved."""
        tokens = "kubectl frobnicate thing -n prod".split()
        assert _is_write_verb(_extract_verb(tokens), tokens)
        assert _classify_risk("frobnicate", tokens) == "medium"

    def test_an_unknown_verb_is_not_read_only_to_the_aci_guard(self):
        assert not is_read_only("kubectl frobnicate thing")

    @pytest.mark.parametrize("command,is_write", [
        ("kubectl rollout status deploy/web", False),
        ("kubectl rollout history deploy/web", False),
        ("kubectl rollout restart deploy/web", True),
        ("kubectl -n prod rollout restart deploy/web", True),   # flag-order, from pass 51
        ("kubectl rollout resume deploy/web", True),            # unlisted subcommand → write
    ])
    def test_subcommands_are_judged_separately(self, command, is_write):
        tokens = command.split()
        assert _is_write_verb(_extract_verb(tokens), tokens) is is_write
        assert is_read_only(command) is (not is_write)

    def test_the_empty_command_is_not_a_write(self):
        assert not _is_write_verb("", [])
