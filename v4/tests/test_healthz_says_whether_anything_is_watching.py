"""`/healthz` answered "ok" for a server whose perception had failed to start.

The response already carried four "is this subsystem actually alive" blocks — `leader`, `audit`,
`memory`, `recorder` — each added for the same stated reason: an empty table is indistinguishable
from a quiet cluster, so the outage has to be *askable*. Perception, the subsystem that produces
those tables and whose entire job is watching, was not among them.

Measured 2026-08-24, with the sensorium in `START_FAILED`:

    sensorium_absence() -> ('start_failed', 'kubectl: connection refused')
    /healthz status     -> ok
    /healthz keys       -> [arm, audit, degraded_experimental_flags, experimental_flags,
                            leader, memory, recorder, set_but_unwired_flags, status, version]
    anything about perception on /healthz? -> False

The cause was recorded and one function call away: `service.sensorium_absence()` distinguishes
six states precisely so this could be reported. It was reachable on `/v1/findings` and in the
digest — surfaces you consult once you already suspect something is wrong, which is the wrong
order for the question "is anything watching my cluster?".

Two things this file pins beyond the missing block:

* **`watching` is not `enabled`.** `app/detectors/perception.py` already draws this line —
  *"an engine exists regardless; what decides the word is whether any `kubectl --watch` stream is
  actually connected. The watch loop returns permanently on a missing kubectl, so 'not watching'
  can hold for the whole process lifetime."* Reporting engine presence alone would have moved the
  same wrong answer up one level.
* **`standby` is not an outage.** On a leader-election standby a peer holds the singleton lock and
  is watching; reporting that as a bare `false` would train an operator to ignore the field.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoints import health
from app.detectors import service as sensorium


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(health.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _restore_absence():
    """`_absence` is module state and every test here writes it."""
    before = (sensorium._absence, sensorium._absence_detail)
    yield
    sensorium._absence, sensorium._absence_detail = before


def _block(client) -> dict:
    return client.get("/healthz").json()["sensorium"]


# ── 1. the question is answerable at all ──────────────────────────────────────────────────────


class TestPerceptionIsOnTheResponse:
    def test_healthz_carries_a_sensorium_block(self, client):
        assert "sensorium" in client.get("/healthz").json()

    def test_a_failed_start_is_visible_with_its_cause(self, client):
        """The defect, in one test: this used to be absent from the response entirely."""
        sensorium.record_start_failure(RuntimeError("kubectl: connection refused"))
        block = _block(client)
        assert block["enabled"] is False
        assert block["watching"] is False
        assert block["state"] == sensorium.START_FAILED
        assert "connection refused" in block["reason"]

    def test_it_sits_beside_the_four_blocks_that_already_answered_this(self, client):
        body = client.get("/healthz").json()
        for sibling in ("leader", "audit", "memory", "recorder"):
            assert sibling in body, f"{sibling} disappeared — this file assumes the set of five"


# ── 2. the six absence states stay distinguishable ────────────────────────────────────────────


class TestEachAbsenceReasonSurvivesTheTrip:
    @pytest.mark.parametrize("state", [
        sensorium.NOT_STARTED,
        sensorium.DISABLED_BY_FLAG,
        sensorium.NO_DETECTORS,
        sensorium.START_FAILED,
        sensorium.STANDBY,
        sensorium.STOPPED,
    ])
    def test_the_state_constant_is_reported_verbatim(self, client, state):
        """A boolean would have collapsed all six. `standby` and `start_failed` are opposite
        situations — one is a peer doing the work, the other is nobody doing it."""
        sensorium._absence, sensorium._absence_detail = state, "why"
        block = _block(client)
        assert block["state"] == state
        assert block["enabled"] is False
        assert block["watching"] is False

    def test_standby_and_start_failed_are_not_the_same_answer(self, client):
        sensorium._absence, sensorium._absence_detail = sensorium.STANDBY, "a peer holds the lock"
        standby = _block(client)
        sensorium.record_start_failure(RuntimeError("boom"))
        failed = _block(client)
        assert standby["state"] != failed["state"]


# ── 3. running is not watching ────────────────────────────────────────────────────────────────


class TestAnEngineWithNoStreamIsNotWatching:
    def test_running_with_no_streams_reports_not_watching(self, client, mocker):
        mocker.patch("app.sensorium.k8s_watcher.stream_health", lambda: [])
        sensorium._absence, sensorium._absence_detail = sensorium.RUNNING, ""
        block = _block(client)
        assert block["enabled"] is True, "the engine really is up — that part is not a lie"
        assert block["watching"] is False
        assert "no kubectl watch stream" in block["reason"]

    def test_running_with_a_disconnected_stream_reports_not_watching(self, client, mocker):
        mocker.patch("app.sensorium.k8s_watcher.stream_health", lambda: [{"stopped": True}])
        mocker.patch("app.sensorium.k8s_watcher.any_stream_connected", lambda: False)
        sensorium._absence, sensorium._absence_detail = sensorium.RUNNING, ""
        assert _block(client)["watching"] is False

    def test_running_with_a_connected_stream_is_watching(self, client, mocker):
        """Vacuity guard: `watching` must be reachable, or every test above passes for free."""
        mocker.patch("app.sensorium.k8s_watcher.stream_health", lambda: [{"stopped": False}])
        mocker.patch("app.sensorium.k8s_watcher.any_stream_connected", lambda: True)
        sensorium._absence, sensorium._absence_detail = sensorium.RUNNING, ""
        block = _block(client)
        assert block["watching"] is True
        assert block["enabled"] is True
        assert block["reason"] == ""


# ── 4. a status read must never break the liveness probe ──────────────────────────────────────


class TestHealthzStillNeverFails:
    def test_a_raising_stream_health_does_not_500(self, client, mocker):
        """`/healthz` is a liveness probe: a 500 here restarts the pod. Reporting `watching:
        false` with the reason is strictly better than turning a status-read bug into a
        restart loop — the endpoint's own docstring says it checks nothing by design."""
        def boom():
            raise RuntimeError("stream registry exploded")

        mocker.patch("app.sensorium.k8s_watcher.stream_health", boom)
        sensorium._absence, sensorium._absence_detail = sensorium.RUNNING, ""
        response = client.get("/healthz")
        assert response.status_code == 200
        block = response.json()["sensorium"]
        assert block["watching"] is False
        assert "could not be read" in block["reason"]

    def test_the_overall_status_is_still_ok(self, client):
        """The block reports the subsystem; it must not flip the liveness verdict. A pod whose
        sensorium is down still serves the API and must not be killed for it."""
        sensorium.record_start_failure(RuntimeError("boom"))
        assert client.get("/healthz").json()["status"] == "ok"


# ── 5. the helper's own contract ──────────────────────────────────────────────────────────────


class TestTheHelperMatchesItsSiblings:
    def test_it_returns_the_same_keys_shape_as_the_other_blocks(self):
        block = sensorium.sensorium_status()
        assert {"enabled", "state", "reason", "watching"} == set(block)

    def test_enabled_tracks_the_engine_and_watching_tracks_the_streams(self, mocker):
        mocker.patch("app.sensorium.k8s_watcher.stream_health", lambda: [{"stopped": False}])
        mocker.patch("app.sensorium.k8s_watcher.any_stream_connected", lambda: True)
        sensorium._absence, sensorium._absence_detail = sensorium.RUNNING, ""
        assert sensorium.sensorium_status() == {
            "enabled": True, "state": sensorium.RUNNING, "watching": True, "reason": "",
        }
