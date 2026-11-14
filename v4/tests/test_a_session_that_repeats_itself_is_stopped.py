"""An auto-approved session that keeps repeating the same steps with the same results is stopped.

Measured 2026-11-14 (`papers/v4-operator-r1/A3-LOOP-INVESTIGATION.md` §2b): the 73-attempt loop was an A-B
ping-pong — `get job report -o yaml` (NotFound) then `apply -f -` (rejected) — with nothing changing between rounds.
The repeat guard refuses the identical write, but the cycle itself (read, refused write, read, refused write…) would
still run to the step limit. OpenHands' StuckDetector flags exactly these shapes: the same action with the same
observation several times, and two action/observation pairs alternating. Here: the same call with the same reply 4
times in a row, or an A-B alternation 3 times, ends the session's tool use with an instruction to report.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from app.tools import kubectl_tool as kt

NOT_FOUND = 'Error from server (NotFound): jobs.batch "report" not found'
REJECTED = 'The request is invalid: patch: Invalid value: "map[...]": unrecognized type: string'
MANIFEST = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: h3-2\ndata:\n  k: v\n"


def _call(
    command: str,
    stdin: str | None = None,
    *,
    session: str = "auto-st",
    bypass: bool = True,
):
    calls: list[list[str]] = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        err = REJECTED if "apply" in argv else NOT_FOUND
        return SimpleNamespace(returncode=1, stdout="", stderr=err)

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
        mock.patch.object(kt, "interrupt", lambda p: True),
    ):
        out = kt.run_kubectl.invoke({"command": command, "stdin": stdin}, config=cfg)
    return calls, out


def _e3_round(session: str = "auto-st"):
    a = _call("kubectl get job report -o yaml", session=session)
    b = _call("kubectl apply -f - -n h3-2", MANIFEST, session=session)
    return a, b


def test_the_e3_ping_pong_is_stopped_within_four_rounds():
    outputs = []
    for _ in range(4):
        (_, a), (_, b) = _e3_round()
        outputs += [a, b]
    assert any("[stuck:" in o for o in outputs), "the A-B cycle must be named as stuck"
    assert kt.session_is_stuck("auto-st")
    calls, out = _call("kubectl get pods -n h3-2")
    assert not calls, "once stuck, no further command runs"
    assert out.startswith("[Stopped]") and "report" in out


def test_the_same_read_with_the_same_reply_four_times_is_stuck():
    for _ in range(4):
        _, out = _call("kubectl get job report -n h3-2")
    assert "[stuck:" in out


def test_three_different_reads_are_not_stuck():
    for cmd in (
        "kubectl get pods -n h3-2",
        "kubectl describe job report -n h3-2",
        "kubectl logs -l app=x -n h3-2",
    ):
        _, out = _call(cmd)
    assert "[stuck:" not in out and not kt.session_is_stuck("auto-st")


def test_a_chat_session_is_never_stopped():
    for _ in range(5):
        _call("kubectl get job report -n h3-2", session="chat-1", bypass=False)
    assert not kt.session_is_stuck("chat-1")
