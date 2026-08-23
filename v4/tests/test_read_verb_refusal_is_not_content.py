"""A refused read must not come back as cluster state — and must not become a health verdict.

`_run` is the shared tail of all four ACI read verbs. `run_kubectl` surfaces refusals and errors as
**text**, so the only thing separating "here is the cluster" from "here is why I could not look" is
how that string is read. It was read with a hand-kept marker list plus `lowered.startswith("error")`

    _GUARD_MARKERS = ("blocked protected", "requires confirmation", "HITL", "not permitted")

and measured 2026-08-20 against the real `run_kubectl`, two of the project's own refusals came back
as `ok=True` with the refusal text as the **body the model reads as cluster state**:

    [Error] kubectl is not installed or not found in PATH…      -> ok=True, health = FAILED
    [Unsupported] 'kubectl edit' requires an interactive
        terminal which is not available…                        -> ok=True, health = CURRENT

The first turned a tooling problem into a verdict that the workload is failing; the second turned a
refusal into a green light, because the phrase "is not available" contains "available".
`startswith("error")` never fired because the string starts with `[`. The three `[Protected]`
refusals were caught only because their wording happens to contain "not permitted" — a coincidence
of phrasing, not a check.

`_health_from` had the same substring defect on *real* output: it matched its keywords anywhere in
the body, so a Deployment named `error-budget-exporter` read as FAILED. Health words are now
matched as whole whitespace-separated fields.
"""
from __future__ import annotations

import pytest

from app.tools.aci import read_verbs as rv
from app.tools.aci.models import Health
from app.tools.kubectl_tool import run_kubectl

# (label, command, role) — each refused by a different real gate, none of them a cluster call.
REFUSED_BY_A_REAL_GATE = [
    ("protected resource: secrets", "get secrets -n prod", "readonly"),
    ("protected namespace", "get deployment web -n kube-system", "admin"),
    ("protected resource: one secret", "get secret db-creds -n prod", "admin"),
    ("unsupported verb", "edit deployment web -n prod", "admin"),
    ("no kubectl on PATH", "get deployment web -n prod", "admin"),
]

# Verbatim kubectl (bitnami/kubectl:latest v1.36.3, 2026-08-20).
UNREACHABLE = ("The connection to the server localhost:8080 was refused - "
               "did you specify the right host or port?")

# kubectl's documented `get pods` table shape.
HEALTHY_TABLE = (
    "NAME                             READY   STATUS    RESTARTS   AGE\n"
    "error-budget-exporter-5c8f9d76   1/1     Running   0          3h\n"
)
CRASHING_TABLE = (
    "NAME          READY   STATUS             RESTARTS   AGE\n"
    "api-7f4d8c9   0/1     CrashLoopBackOff   5          2m\n"
)


def _real_refusal(command: str, role: str, *, label: str = "") -> str:
    out = run_kubectl.invoke({"command": command}, config={"configurable": {"user_role": role}})
    assert out.startswith("["), f"expected a refusal marker, got {out[:80]!r}"
    return out


@pytest.fixture
def force_no_kubectl(monkeypatch):
    """Make kubectl genuinely unresolvable, instead of hoping the host lacks it.

    The "no kubectl on PATH" case used to depend on the machine not having kubectl installed.
    On a machine that HAS it — every developer laptop that ever touched a cluster, and any CI
    runner with a kubectl step — `run_kubectl` really executes it, returns cluster output or a
    connection error, and the `startswith("[")` assertion fails for a reason that has nothing
    to do with the behaviour under test. Forcing the FileNotFoundError exercises the exact
    branch kubectl_tool.py:1590 handles, on every machine.
    """
    import subprocess

    real_run = subprocess.run

    def no_kubectl(cmd, *a, **k):
        argv0 = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else cmd
        if isinstance(argv0, str) and argv0.endswith("kubectl"):
            raise FileNotFoundError(2, "No such file or directory", "kubectl")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", no_kubectl)
    yield


def _run_with(raw: str):
    original = rv._exec
    rv._exec = lambda command, _r=raw: _r
    try:
        return rv._run("inspect", "get pods -n prod", "pods in prod")
    finally:
        rv._exec = original


class TestARefusalIsAnErrorNotABody:
    @pytest.mark.parametrize("label,command,role", REFUSED_BY_A_REAL_GATE,
                             ids=[c[0] for c in REFUSED_BY_A_REAL_GATE])
    def test_the_verb_reports_failure_and_never_hands_it_over_as_content(
        self, label, command, role, request
    ):
        if label == "no kubectl on PATH":
            request.getfixturevalue("force_no_kubectl")
        raw = _real_refusal(command, role)
        result = _run_with(raw)
        assert result.ok is False, f"{label}: {raw.splitlines()[0]}"
        assert result.error and raw.strip()[:40] in result.error
        assert not result.body, "the refusal text reached the model as cluster state"
        assert result.empty is False

    def test_an_unreachable_cluster_is_not_content_either(self):
        result = _run_with(UNREACHABLE)
        assert result.ok is False and not result.body

    def test_a_plain_kubectl_error_is_not_content(self):
        result = _run_with('error: the path "/nope.yaml" does not exist')
        assert result.ok is False and not result.body


class TestARealReadStillWorks:
    def test_a_table_is_returned_as_a_body(self):
        result = _run_with(HEALTHY_TABLE)
        assert result.ok is True and result.empty is False
        assert "error-budget-exporter" in (result.body or "")

    def test_an_empty_result_is_still_empty_not_an_error(self):
        result = _run_with("No resources found in prod namespace.\n")
        assert result.ok is True and result.empty is True


class TestHealthComesFromAStatusWordNotASubstring:
    def test_a_workload_named_error_something_is_not_failing(self):
        assert rv._health_from(HEALTHY_TABLE) is Health.CURRENT

    def test_a_real_crashloop_is_still_failed(self):
        assert rv._health_from(CRASHING_TABLE) is Health.FAILED

    @pytest.mark.parametrize("name", [
        "error-budget-exporter", "crashloopbackoff-detector", "failed-login-collector",
        "imagepullbackoff-alerter",
    ])
    def test_a_name_that_contains_a_status_word_is_not_that_status(self, name):
        body = f"NAME   READY   STATUS    RESTARTS   AGE\n{name}   1/1     Running   0   3h\n"
        assert rv._health_from(body) is Health.CURRENT

    @pytest.mark.parametrize("phase,expected", [
        ("Running", Health.CURRENT),
        ("Pending", Health.IN_PROGRESS),
        ("Failed", Health.FAILED),
        ("Terminating", Health.TERMINATING),
    ])
    def test_a_normalized_yaml_phase_still_reads(self, phase, expected):
        assert rv._health_from(f"status:\n  phase: {phase}\n") is expected

    def test_nothing_recognisable_is_unknown(self):
        assert rv._health_from("apiVersion: v1\nkind: ConfigMap\n") is Health.UNKNOWN
