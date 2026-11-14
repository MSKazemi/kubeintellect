"""A kubectl error tells the model what is wrong and whether resending the same command can help.

Measured 2026-11-14 (`papers/v4-operator-r1/A3-LOOP-INVESTIGATION.md` §2): an unattended session resent the same
rejected `kubectl apply` 73 times. The rejection ("The request is invalid: … unrecognized type: string") matched no
hint, so the model was never told that its manifest had a field of the wrong type, nor that a 4xx error does not go
away on retry (AWS Builders' Library: client errors are not worth retrying). The strings below are the replies the
cluster actually gave in those runs (`data/loop-2026-11-14/`).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from app.tools import kubectl_errors as ke
from app.tools import kubectl_tool as kt

# Recorded replies (E3 replay; re-runs r1, r3, r4), abbreviated only where noted.
E3_APPLY = (
    'The request is invalid: patch: Invalid value: "map[metadata:map[annotations:map[kubectl.kubernetes.io/'
    'last-applied-configuration:{\\"apiVersion\\":\\"batch/v1\\"}]] spec:map[backoffLimit:5]]": unrecognized type: string'
)
SSA_TYPE = (
    "Error from server: failed to create typed patch object (h3-2/report; batch/v1, Kind=Job): "
    '.spec.template.spec.containers[name="c"].command[2]: expected string, got &value.valueUnstructured{}'
)
CREATE_TYPE = (
    'Error from server (BadRequest): error when creating "STDIN": Job in version "v1" cannot be handled as a Job: '
    "json: cannot unmarshal object into Go struct field Container.spec.template.spec.containers.command of type string"
)
IMMUTABLE = 'The Job "report" is invalid: spec.template: Invalid value: {"labels":{}}: field is immutable'
EXISTS = 'error: failed to create job: jobs.batch "report" already exists'
UNKNOWN_FLAG = (
    "error: unknown flag: --command\nSee 'kubectl create job --help' for usage."
)


class TestTheRecordedRejectionsAreExplained:
    @pytest.mark.parametrize("reply", [E3_APPLY, SSA_TYPE, CREATE_TYPE])
    def test_a_wrong_type_in_a_manifest_gets_the_quoting_hint(self, reply):
        name, hint = ke.interpret(reply)
        assert name == "manifest_type_error"
        assert "quoted" in hint and "wrong type" in hint

    def test_already_exists_says_change_it_in_place_not_a_substitute(self):
        name, hint = ke.interpret(EXISTS)
        assert name == "already_exists"
        assert "substitute" in hint

    def test_an_unknown_flag_is_named(self):
        assert ke.interpret(UNKNOWN_FLAG)[0] == "unknown_flag"

    def test_immutable_no_longer_recommends_recreating_as_a_retry(self):
        # The old hint ("use kubectl replace or recreate the resource") pointed the faked-fix session at
        # `replace` and then at a substitute Job under a new name.
        name, hint = ke.interpret(IMMUTABLE)
        assert name == "immutable_field"
        assert "recreate the resource" not in hint and "human" in hint


class TestEachErrorSaysWhetherResendingCanHelp:
    @pytest.mark.parametrize(
        "reply", [E3_APPLY, SSA_TYPE, CREATE_TYPE, IMMUTABLE, EXISTS, UNKNOWN_FLAG]
    )
    def test_permanent(self, reply):
        assert ke.error_class(ke.interpret(reply)[0]) == "permanent"

    def test_a_conflict_is_transient(self):
        name, _ = ke.interpret(
            'Operation cannot be fulfilled on jobs.batch "report": the object has been modified'
        )
        assert ke.error_class(name) == "transient"

    def test_an_unmatched_error_has_no_class(self):
        assert ke.error_class(ke.interpret("something nobody has seen")[0]) is None


def test_the_tool_reply_carries_the_class():
    fake = SimpleNamespace(returncode=1, stdout="", stderr=E3_APPLY)
    cfg = {
        "configurable": {
            "user_role": "operator",
            "hitl_bypass": True,
            "thread_id": "auto-x",
            "bound_namespace": "h3-2",
        }
    }
    manifest = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: h3-2\ndata:\n  k: v\n"
    with (
        mock.patch.object(kt.subprocess, "run", lambda *a, **k: fake),
        mock.patch.object(kt, "_capture_rollback_point", lambda *a, **k: None),
    ):
        out = kt.run_kubectl.invoke(
            {"command": "kubectl apply -f - -n h3-2", "stdin": manifest}, config=cfg
        )
    assert out.startswith("[kubectl exited 1]")
    assert "[permanent error: resending this exact command cannot succeed" in out
