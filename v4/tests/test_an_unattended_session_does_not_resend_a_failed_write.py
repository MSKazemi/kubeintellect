"""An unattended session does not resend a write that already failed permanently, and its writes are budgeted.

Measured 2026-11-14 (`papers/v4-operator-r1/A3-LOOP-INVESTIGATION.md` §2b): an auto-approved session sent the same
`kubectl apply -f - -n h3-2`, with the same manifest, 73 times; every one was rejected the same way and nothing in
the tool noticed. OpenHands' StuckDetector stops "the same action producing the same error" after three; AWS's
retry guidance does not retry client errors at all. Here the second identical write after a permanent failure is
refused before it reaches the cluster, and an auto-approved session may run at most AUTO_APPROVE_MAX_WRITES writes
before each further one needs a human.

Only auto-approved sessions are affected: in a chat a human approves every write and may retry after fixing
something outside the session.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from app.core.config import settings
from app.tools import kubectl_tool as kt

E3_REJECTION = 'The request is invalid: patch: Invalid value: "map[...]": unrecognized type: string'
CONFLICT = 'Operation cannot be fulfilled on configmaps "c": the object has been modified; please apply your changes'


def _cm(value: str) -> str:
    return f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: h3-2\ndata:\n  k: '{value}'\n"


@pytest.fixture(autouse=True)
def _fresh():
    kt.reset_session_guard()
    yield
    kt.reset_session_guard()


def _send(
    stdin: str,
    *,
    rc: int = 1,
    err: str = E3_REJECTION,
    session: str = "auto-a",
    bypass: bool = True,
):
    calls: list[list[str]] = []
    prompts: list[dict] = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        return SimpleNamespace(
            returncode=rc,
            stdout="" if rc else "configmap/c configured\n",
            stderr=err if rc else "",
        )

    cfg = {
        "configurable": {
            "user_role": "operator",
            "hitl_bypass": bypass,
            "thread_id": session,
            "bound_namespace": "h3-2",
        }
    }
    with (
        mock.patch.object(kt.subprocess, "run", fake_run),
        mock.patch.object(kt, "_capture_rollback_point", lambda *a, **k: None),
        mock.patch.object(kt, "interrupt", lambda p: prompts.append(p) or (not bypass)),
    ):
        out = kt.run_kubectl.invoke(
            {"command": "kubectl apply -f - -n h3-2", "stdin": stdin}, config=cfg
        )
    return calls, prompts, out


class TestARepeatedFailedWrite:
    def test_the_e3_shape_stops_at_the_second_attempt(self):
        calls, _, first = _send(_cm("x"))
        assert calls and "[permanent error:" in first
        calls, _, second = _send(_cm("x"))
        assert not calls, "the identical write must not reach the cluster a second time"
        assert second.startswith("[Repeat refused]")
        assert "unrecognized type" in second and "change the approach" in second

    def test_a_changed_manifest_is_a_new_attempt(self):
        _send(_cm("x"))
        calls, _, _ = _send(_cm("y"))
        assert calls

    def test_another_session_is_not_affected(self):
        _send(_cm("x"), session="auto-a")
        calls, _, _ = _send(_cm("x"), session="auto-b")
        assert calls

    def test_a_transient_failure_may_be_retried(self):
        _send(_cm("x"), err=CONFLICT)
        calls, _, _ = _send(_cm("x"), err=CONFLICT)
        assert calls

    def test_a_chat_session_with_a_human_is_not_affected(self):
        _send(_cm("x"), bypass=False)
        calls, _, _ = _send(_cm("x"), bypass=False)
        assert calls, (
            "a human approved it; they may retry after fixing something outside the session"
        )


class TestTheWriteBudget:
    def test_writes_beyond_the_budget_need_a_human(self):
        limit = settings.AUTO_APPROVE_MAX_WRITES
        for i in range(limit):
            calls, prompts, _ = _send(_cm(f"v{i}"), rc=0)
            assert calls and not prompts, f"write {i + 1} of {limit} runs unattended"
        calls, prompts, out = _send(_cm("one-too-many"), rc=0)
        assert prompts, "the write after the budget must stop at the approval prompt"
        assert not calls and out == "Action cancelled by user."

    def test_rejected_writes_count_too(self):
        limit = settings.AUTO_APPROVE_MAX_WRITES
        for i in range(limit):
            _send(_cm(f"bad{i}"))
        _, prompts, _ = _send(_cm("next"), rc=0)
        assert prompts
