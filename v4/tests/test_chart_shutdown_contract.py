"""The chart's shutdown contract — the one part of a rolling update no test can observe.

Draining is the rare setting where the *documented* mechanism and the *real* one came apart
without anything going red. The chart, the deployment template and two module docstrings all
described the same sequence: SIGTERM flips ``/readyz`` to 503, Kubernetes notices, traffic
stops, then the pools close. Sending a real SIGTERM to a real server and probing it (pass 48,
2026-08-19) showed the transition is 200 → ECONNREFUSED: uvicorn closes its listening socket
first and runs the application's shutdown hook last, so the 503 never becomes visible and a
request arriving on a stale kube-proxy route is refused rather than served.

What actually holds the socket open is the ``preStop`` sleep, because Kubernetes runs preStop
*before* SIGTERM. These tests pin that mechanism in place, and pin the arithmetic that makes
it work — because the failure mode is silent in both directions: delete the hook and every
rolling update drops a few requests with no error anywhere, or set a grace period at or below
the sleep and the SIGKILL lands mid-drain, reintroducing exactly what the hook prevents. A
cluster is the only other place either mistake shows up, and only under load.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "kubeintellect"
_DEPLOYMENT = _CHART / "templates" / "deployment.yaml"


def _values(path: Path) -> dict:
    """Load a values file. These are plain YAML — only templates carry Go syntax."""
    return yaml.safe_load(path.read_text()) or {}


def _values_files() -> list[Path]:
    """Only files that actually set a shutdown knob. A file that sets neither inherits both
    defaults, which values.yaml already proves — parametrizing over it yields a skip that
    reads like coverage without being any."""
    keys = ("drainSeconds", "terminationGracePeriodSeconds")
    return sorted(
        f for f in _CHART.glob("values*.yaml*")
        if f.is_file() and any(k in (_values(f) or {}) for k in keys)
    )


class TestTheDrainMechanismIsPresent:
    def test_the_deployment_declares_a_prestop_hook(self):
        text = _DEPLOYMENT.read_text()
        assert "preStop:" in text, (
            "The deployment has no preStop hook, so nothing holds the listening socket open "
            "while the Endpoints removal propagates. Readiness cannot substitute: uvicorn "
            "closes the socket at SIGTERM and runs the shutdown hook afterwards."
        )

    def test_the_hook_sleeps_for_the_configured_drain(self):
        text = _DEPLOYMENT.read_text()
        assert re.search(r"command:\s*\[\"/bin/sleep\",\s*\"\{\{\s*\.Values\.drainSeconds", text), (
            "The preStop hook must sleep for .Values.drainSeconds. A hardcoded duration "
            "cannot be tuned per environment, and the arithmetic test below stops applying."
        )

    def test_drain_is_on_by_default(self):
        assert int(_values(_CHART / "values.yaml")["drainSeconds"]) > 0, (
            "drainSeconds defaults to 0, which disables the hook — a default install would "
            "drop requests on every rolling update."
        )


class TestTheShutdownBudgetAddsUp:
    """grace > drain, in every values file that sets either. The killer is the silent case."""

    @pytest.mark.parametrize("path", _values_files(), ids=lambda p: p.name)
    def test_the_grace_period_outlasts_the_drain(self, path: Path):
        values = _values(path)
        drain = values.get("drainSeconds")
        grace = values.get("terminationGracePeriodSeconds")
        # An override that moves one without the other inherits the default for the other.
        defaults = _values(_CHART / "values.yaml")
        drain = int(defaults["drainSeconds"] if drain is None else drain)
        grace = int(defaults["terminationGracePeriodSeconds"] if grace is None else grace)
        assert grace > drain, (
            f"{path.name}: terminationGracePeriodSeconds={grace} does not exceed "
            f"drainSeconds={drain}. The preStop sleep is spent inside the grace budget, so "
            f"Kubernetes would SIGKILL the pod while it is still draining."
        )

    @pytest.mark.parametrize("path", _values_files(), ids=lambda p: p.name)
    def test_in_flight_requests_still_have_room_after_the_drain(self, path: Path):
        """Draining is not the only thing that has to fit — requests and pools follow it."""
        values = _values(path)
        defaults = _values(_CHART / "values.yaml")
        drain = int(values.get("drainSeconds", defaults["drainSeconds"]))
        grace = int(values.get("terminationGracePeriodSeconds",
                               defaults["terminationGracePeriodSeconds"]))
        assert grace - drain >= 15, (
            f"{path.name}: only {grace - drain}s left after the {drain}s drain for in-flight "
            f"requests and closing the Postgres pools. That is not a drain, it is a kill."
        )


class TestTheProbesAnswerTheirOwnQuestion:
    """Liveness and readiness must not point at the same endpoint — see app/core/readiness.py."""

    def test_liveness_and_readiness_use_different_paths(self):
        # Strip comments: the surrounding prose discusses both endpoints, and matching it
        # would let a genuinely mispointed probe hide behind a paragraph that names the
        # right path.
        text = "\n".join(
            line for line in _DEPLOYMENT.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        liveness = text.split("livenessProbe:", 1)[1].split("readinessProbe:", 1)[0]
        readiness = text.split("readinessProbe:", 1)[1]
        assert "/healthz" in liveness, "liveness must probe /healthz (checks nothing by design)"
        assert "/readyz" in readiness, "readiness must probe /readyz"
        assert "/readyz" not in liveness, (
            "Liveness is pointed at /readyz. Once draining returns 503 the kubelet would "
            "restart the pod mid-shutdown, and any dependency check reachable from readiness "
            "would turn one database blip into a cluster-wide restart loop."
        )
