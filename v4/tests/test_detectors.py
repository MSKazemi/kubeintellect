"""Detector engine, predicates, sensorium normalisation (P2, ADR-006)."""
from __future__ import annotations

import time

from app.detectors.engine import DetectorEngine, load_detectors
from app.detectors.models import parse_detect_block
from app.sensorium.k8s_watcher import _JsonStream
from app.sensorium.observations import Observation, pod_display_status


def _obs(kind="pod_status", ns="default", name="web-1", ts=None, **fields):
    return Observation(
        kind=kind, cluster_id="test", namespace=ns, name=name,
        fields=fields, ts=ts if ts is not None else time.time(),
    )


def _engine(detect_raw: dict, playbook="TestPB", mocker=None) -> DetectorEngine:
    block = parse_detect_block(playbook, detect_raw)
    assert block is not None
    return DetectorEngine(detectors=(block,), cluster_id="test")


CRASHLOOP = {
    "watch_predicates": [
        {"kind": "Pod", "status_regex": "^CrashLoopBackOff$"},
        {
            "kind": "Event",
            "reason_regex": "^BackOff$",
            "message_regex": "Back-off restarting failed container",
            "involved_kind": "Pod",
        },
    ],
    "promql": ['kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"} == 1'],
    "debounce_seconds": 60,
}


class TestPredicates:
    def test_pod_status_match(self):
        block = parse_detect_block("x", CRASHLOOP)
        assert block.watch_predicates[0].matches(_obs(status="CrashLoopBackOff"))
        assert not block.watch_predicates[0].matches(_obs(status="Init:CrashLoopBackOff"))
        assert not block.watch_predicates[0].matches(_obs(status="Running"))

    def test_event_reason_and_message_co_condition(self):
        pred = parse_detect_block("x", CRASHLOOP).watch_predicates[1]
        good = _obs(
            kind="event", reason="BackOff", event_type="Warning",
            message="Back-off restarting failed container web", involved_kind="Pod",
        )
        assert pred.matches(good)
        wrong_message = _obs(
            kind="event", reason="BackOff", event_type="Warning",
            message="Back-off pulling image", involved_kind="Pod",
        )
        assert not pred.matches(wrong_message)

    def test_event_involved_kind_narrowing(self):
        pred = parse_detect_block("x", CRASHLOOP).watch_predicates[1]
        node_event = _obs(
            kind="event", reason="BackOff", event_type="Warning",
            message="Back-off restarting failed container", involved_kind="Node",
        )
        assert not pred.matches(node_event)

    def test_normal_events_ignored(self):
        pred = parse_detect_block("x", CRASHLOOP).watch_predicates[1]
        normal = _obs(
            kind="event", reason="BackOff", event_type="Normal",
            message="Back-off restarting failed container", involved_kind="Pod",
        )
        assert not pred.matches(normal)

    def test_null_detect_block(self):
        assert parse_detect_block("x", None) is None
        assert parse_detect_block("x", {}) is None


class TestEngineStateMachine:
    def test_debounce_zero_fires_immediately(self, mocker):
        mocker.patch("app.detectors.engine.flight_recorder.record")
        engine = _engine({**CRASHLOOP, "debounce_seconds": 0})
        fired = engine.process(_obs(status="CrashLoopBackOff"))
        assert len(fired) == 1
        assert fired[0].playbook == "TestPB"
        assert fired[0].namespace == "default"

    def test_debounce_holds_then_fires_on_tick(self, mocker):
        mocker.patch("app.detectors.engine.flight_recorder.record")
        engine = _engine(CRASHLOOP)  # debounce 60
        t0 = 1000.0
        assert engine.process(_obs(status="CrashLoopBackOff", ts=t0)) == []
        assert engine.tick(now=t0 + 30) == []          # still within debounce
        fired = engine.tick(now=t0 + 61)
        assert len(fired) == 1

    def test_no_refire_while_condition_persists(self, mocker):
        mocker.patch("app.detectors.engine.flight_recorder.record")
        engine = _engine({**CRASHLOOP, "debounce_seconds": 0})
        assert len(engine.process(_obs(status="CrashLoopBackOff"))) == 1
        assert engine.process(_obs(status="CrashLoopBackOff")) == []
        assert engine.tick() == []

    def test_recovery_clears_and_allows_refire(self, mocker):
        mocker.patch("app.detectors.engine.flight_recorder.record")
        engine = _engine({**CRASHLOOP, "debounce_seconds": 0})
        assert len(engine.process(_obs(status="CrashLoopBackOff"))) == 1
        engine.process(_obs(status="Running"))           # transition clears
        assert len(engine.process(_obs(status="CrashLoopBackOff"))) == 1

    def test_condition_clearing_within_debounce_prevents_fire(self, mocker):
        mocker.patch("app.detectors.engine.flight_recorder.record")
        engine = _engine(CRASHLOOP)
        t0 = 1000.0
        engine.process(_obs(status="CrashLoopBackOff", ts=t0))
        engine.process(_obs(status="Running", ts=t0 + 10))   # recovered
        assert engine.tick(now=t0 + 120) == []

    def test_distinct_objects_tracked_independently(self, mocker):
        mocker.patch("app.detectors.engine.flight_recorder.record")
        engine = _engine({**CRASHLOOP, "debounce_seconds": 0})
        assert len(engine.process(_obs(status="CrashLoopBackOff", name="a"))) == 1
        assert len(engine.process(_obs(status="CrashLoopBackOff", name="b"))) == 1

    def test_finding_recorded_to_flight_recorder(self, mocker):
        record = mocker.patch("app.detectors.engine.flight_recorder.record")
        engine = _engine({**CRASHLOOP, "debounce_seconds": 0})
        engine.process(_obs(status="CrashLoopBackOff"))
        record.assert_called_once()
        episode_id, kind, payload = record.call_args[0]
        assert episode_id == "findings:test"
        assert kind == "finding"
        assert payload["playbook"] == "TestPB"


TERMINATING = {
    "watch_predicates": [{"kind": "Pod", "status_regex": "^Terminating$"}],
    "debounce_seconds": 600,
}


class TestTerminatingStuckReliability:
    """Regression tests for the churn false-positive (E3a).

    A normal termination arms the Terminating key, then the pod is removed; its
    *final* watch document is a DELETED event whose object still carries
    deletionTimestamp, so status still computes 'Terminating' and still matches.
    Before the fix the armed key lingered and tick() fired it at debounce → 66
    false firings under 20 min of churn. A DELETED event must disarm the key.
    """

    def test_deleted_pod_clears_terminating_arm(self, mocker):
        mocker.patch("app.detectors.engine.flight_recorder.record")
        engine = _engine(TERMINATING)
        t0 = 1000.0
        assert engine.process(_obs(status="Terminating", ts=t0, watch_type="MODIFIED")) == []
        # pod finishes terminating and is removed (final event still Terminating)
        assert engine.process(
            _obs(status="Terminating", ts=t0 + 30, watch_type="DELETED")
        ) == []
        # long past the 600s debounce: must NOT fire — the object is gone
        assert engine.tick(now=t0 + 601) == []

    def test_genuinely_stuck_terminating_still_fires(self, mocker):
        mocker.patch("app.detectors.engine.flight_recorder.record")
        engine = _engine(TERMINATING)
        t0 = 1000.0
        # stuck pod: Terminating persists via MODIFIED heartbeats, never DELETED
        assert engine.process(_obs(status="Terminating", ts=t0, watch_type="MODIFIED")) == []
        assert engine.process(
            _obs(status="Terminating", ts=t0 + 300, watch_type="MODIFIED")
        ) == []
        fired = engine.tick(now=t0 + 601)
        assert len(fired) == 1

    def test_churn_many_normal_terminations_no_false_fire(self, mocker):
        mocker.patch("app.detectors.engine.flight_recorder.record")
        engine = _engine(TERMINATING)
        t0 = 1000.0
        # 66 pods each churn Terminating -> DELETED within grace (mirrors the
        # 20-min churn workload that produced 66 false firings)
        for i in range(66):
            name = f"job-pod-{i}"
            engine.process(_obs(status="Terminating", name=name, ts=t0 + i, watch_type="ADDED"))
            engine.process(
                _obs(status="Terminating", name=name, ts=t0 + i + 20, watch_type="DELETED")
            )
        # well past debounce for every armed key
        assert engine.tick(now=t0 + 66 + 700) == []


class TestPlaybookDetectorLoading:
    def test_eighteen_compiled_three_llm_only(self):
        detectors = load_detectors()
        assert len(detectors) == 18
        names = {d.playbook for d in detectors}
        assert "CommandHardcodedFailure" not in names   # LLM-only by design
        assert "ServiceUnreachable" not in names
        # A NetworkPolicy denial is dropped in the CNI datapath, so no signal
        # ever reaches the API server — there is nothing to compile.
        assert "NetworkPolicyBlocking" not in names
        assert "CrashLoopBackOff" in names
        assert "DeploymentRolloutStuck" in names

    def test_all_compiled_blocks_have_predicates(self):
        for det in load_detectors():
            assert det.watch_predicates, f"{det.playbook} has no watch predicates"
            assert det.debounce_seconds >= 0


class TestPodDisplayStatus:
    def test_waiting_reason_wins(self):
        pod = {
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {"state": {"waiting": {"reason": "CrashLoopBackOff"}}}
                ],
            }
        }
        assert pod_display_status(pod) == "CrashLoopBackOff"

    def test_init_container_prefix(self):
        pod = {
            "status": {
                "phase": "Pending",
                "initContainerStatuses": [
                    {"state": {"waiting": {"reason": "CrashLoopBackOff"}}}
                ],
            }
        }
        assert pod_display_status(pod) == "Init:CrashLoopBackOff"

    def test_evicted_via_status_reason(self):
        pod = {"status": {"phase": "Failed", "reason": "Evicted"}}
        assert pod_display_status(pod) == "Evicted"

    def test_terminating_via_deletion_timestamp(self):
        pod = {
            "metadata": {"deletionTimestamp": "2026-06-12T00:00:00Z"},
            "status": {"phase": "Running"},
        }
        assert pod_display_status(pod) == "Terminating"

    def test_plain_phase_fallback(self):
        assert pod_display_status({"status": {"phase": "Running"}}) == "Running"


class TestJsonStream:
    def test_incremental_parse(self):
        stream = _JsonStream()
        docs = stream.feed('{"type": "ADDED", "object": {"kind": "Pod"}}{"type": "MODIF')
        assert docs == [{"type": "ADDED", "object": {"kind": "Pod"}}]
        docs = stream.feed('IED", "object": {"kind": "Pod"}}')
        assert docs == [{"type": "MODIFIED", "object": {"kind": "Pod"}}]

    def test_whitespace_between_documents(self):
        stream = _JsonStream()
        assert stream.feed('{"a": 1}\n\n{"b": 2}\n') == [{"a": 1}, {"b": 2}]


class TestFindingsEndpoint:
    async def test_findings_disabled(self, mocker):
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        mocker.patch("app.api.v1.endpoints.findings.get_engine", return_value=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get("/v1/findings")
        assert response.status_code == 200
        assert response.json()["sensorium"] == "disabled"

    async def test_findings_active(self, mocker):
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        mocker.patch("app.detectors.engine.flight_recorder.record")
        engine = _engine({**CRASHLOOP, "debounce_seconds": 0})
        engine.process(_obs(status="CrashLoopBackOff"))
        mocker.patch("app.api.v1.endpoints.findings.get_engine", return_value=engine)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get("/v1/findings")
        body = response.json()
        assert body["sensorium"] == "active"
        assert len(body["findings"]) == 1
        assert body["findings"][0]["playbook"] == "TestPB"
