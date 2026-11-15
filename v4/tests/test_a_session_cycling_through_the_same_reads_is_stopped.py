"""A session that keeps cycling through the same reads, learning nothing new, is stopped — whatever order it uses.

Pilot run 4 (2026-11-15, Nova Pro, `b3-c1`): the session created the missing ConfigMap with the wrong key, then issued
the same four reads (ConfigMap data, ConfigMap name, the Deployment's `env` and `envFrom`) about 33 times each, with the
same replies, in parallel batches whose order changed from round to round — 155 calls, until the step budget ended
it. The stuck detector knew two shapes only: one call repeated 4 times in a row, and an exact A-B alternation 3 times.
A permuted 4-call cycle is neither.

The rule now also asks whether the session is still learning anything: if the last 12 calls are at most 6 distinct
call/reply pairs, each already seen at least 4 times in the session and at least once before those 12, nothing new is
coming back. Replayed over every session of pilot runs 3 and 4 (six model runs), it stops this one, at call 41, and no
other — none of the sessions that went on to fix their fault.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from app.tools import kubectl_tool as kt

# The recorded run-4 session, one letter per call, in the order the tool received them.
_SEQUENCE = (
    "abcdefghijhkklmcnoohchpqrrnstnrstnrstrnstnrtsrntsrntsrntsrnstrntsnrstrntsrnstrnstrnstnrtsnrstrnstrntsnrstrnts"
    "rnstnrtsnrtsrnstrntsnrstnrstnrstrntsrntsrntsnr"
)
_COMMANDS = {
    "a": "kubectl get events -n b3-c1 --sort-by=.lastTimestamp --field-selector=type=Warning",
    "b": "kubectl get pods -n b3-c1",
    "c": "kubectl describe pod faulty-app-f9f9b8867-5n8nl -n b3-c1",
    "d": "kubectl get configmap app-config -n b3-c1",
    "e": "kubectl get configmap app-config -o yaml -n b3-c1",
    "f": "kubectl get pod faulty-app-f9f9b8867-5n8nl -n b3-c1",
    "g": "kubectl create configmap app-config --from-literal=key1=value1 --namespace=b3-c1",
    "h": "kubectl get pod -l app=faulty-app -n b3-c1",
    "i": "kubectl get deployment -n b3-c1",
    "j": "kubectl get replicaset -n b3-c1",
    "k": "kubectl get deployment faulty-app -n b3-c1 -o yaml",
    "l": "kubectl scale deployment faulty-app --replicas=1 -n b3-c1",
    "m": "kubectl scale deployment faulty-app --replicas=0 -n b3-c1",
    "n": "kubectl get configmap app-config -n b3-c1 -o jsonpath={.data}",
    "o": "kubectl get pod faulty-app-f9f9b8867-5n8nl -n b3-c1 -o jsonpath={.spec.containers[*].env}",
    "p": "kubectl get deployment faulty-app -n b3-c1",
    "q": "kubectl describe deployment faulty-app -n b3-c1",
    "r": "kubectl get deployment faulty-app -n b3-c1 -o jsonpath={.spec.template.spec.containers[*].env}",
    "s": "kubectl get deployment faulty-app -n b3-c1 -o jsonpath={.spec.template.spec.containers[*].envFrom}",
    "t": "kubectl get configmap app-config -n b3-c1 -o jsonpath={.metadata.name}",
}


def _replay(
    sequence: str, session: str, reply=lambda command, i: f"reply to {command}\n"
):
    """Send each call through run_kubectl as an auto-approved bound session; return (calls executed, outputs)."""
    executed: list[str] = []
    outputs: list[str] = []
    cfg = {
        "configurable": {
            "user_role": "operator",
            "hitl_bypass": True,
            "thread_id": session,
            "bound_namespace": "b3-c1",
        }
    }
    for i, letter in enumerate(sequence):
        command = _COMMANDS[letter]

        def fake_run(argv, *a, _c=command, _i=i, **k):
            executed.append(_c)
            return SimpleNamespace(returncode=0, stdout=reply(_c, _i), stderr="")

        with (
            mock.patch.object(kt.subprocess, "run", fake_run),
            mock.patch.object(kt, "_capture_rollback_point", lambda *a, **k: None),
            mock.patch.object(kt, "interrupt", lambda p: True),
            mock.patch.object(kt.settings, "AUTO_APPROVE_ENABLED", True),
            mock.patch.object(kt.settings, "PREFLIGHT_DIFF_ENABLED", False),
            mock.patch.object(kt.settings, "MANIFEST_LINT_ENABLED", False),
        ):
            outputs.append(kt.run_kubectl.invoke({"command": command}, config=cfg))
    return executed, outputs


def test_the_run_4_loop_is_stopped_long_before_the_step_budget():
    kt.reset_session_guard()
    executed, outputs = _replay(_SEQUENCE, "auto-run4-nova-b3-c1")
    assert kt.session_is_stuck("auto-run4-nova-b3-c1")
    assert len(executed) <= 45, f"{len(executed)} of {len(_SEQUENCE)} calls ran"
    assert any("repeating" in o.lower() or "stop" in o.lower() for o in outputs[30:50])


def test_the_same_reads_with_changing_replies_are_progress_not_a_loop():
    """Polling a rollout: the same three reads, but the state they report moves."""
    kt.reset_session_guard()
    executed, _ = _replay(
        "nrs" * 20,
        "auto-polling",
        reply=lambda command, i: f"{command} observed at step {i}\n",
    )
    assert not kt.session_is_stuck("auto-polling") and len(executed) == 60


def test_a_long_session_reading_many_different_things_is_not_stopped():
    kt.reset_session_guard()
    executed, _ = _replay("abcdefhijkpq" * 2 + "nrst", "auto-broad")
    assert not kt.session_is_stuck("auto-broad") and len(executed) == 28
