"""tests/test_zero_shadow_firings_is_not_a_precision_measurement.py

`GET /v1/detectors/{name}/shadow-findings` answers the question a reviewer promotes or rejects
a candidate detector on. Until 2026-08-24 three different states gave the same answer:

    the sensorium is not running at all                 200  {"sensorium":"disabled","findings":[]}
    engine up, detector not loaded (DB unreachable)     200  {"name":…,"findings":[]}
    engine up, detector loaded, genuinely quiet         200  {"name":…,"findings":[]}

and `kq detector shadow <name>` rendered all three as `<name>: 0 shadow firing(s)` — a reviewer
reading "quiet, no false positives, safe to promote" off a detector that was never evaluated.

`list_detectors`, two functions above in the same file, already draws the line this endpoint was
missing: "'I cannot answer' and 'the answer is nothing' are different, and for a detector
inventory the difference is whether the operator believes their cluster is unmonitored or merely
unqueryable." The same sentence applies to a candidate's firing count.

`watching: false` deliberately says "not loaded in this process", never "no such detector":
`load_db_detectors` documents that an unreachable DB silently disarms every stored detector, so
absence from the engine is not evidence about the detector's existence.
"""

from collections import deque
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoints import detectors as ep
from app.core.config import settings


class FakeDetector:
    """Shaped like the real `DetectBlock`, which always carries both predicate tuples.

    It did not, and the omission mattered: `watching` now asks *which kind* of predicate a
    loaded detector has, because a trend-only detector on a server with predictive detection
    off is loaded and evaluated by nothing. A fake missing a field the real object always has
    cannot exercise that.
    """

    def __init__(self, playbook, *, watch=("pod-status",), trend=()):
        self.playbook = playbook
        self.watch_predicates = tuple(watch)
        self.trend_predicates = tuple(trend)


class FakeFinding:
    def __init__(self, playbook, obj="pod-1"):
        self.playbook = playbook
        self._obj = obj

    def to_dict(self):
        return {"playbook": self.playbook, "namespace": "default", "object": self._obj,
                "evidence": "restarts climbing"}


class FakeEngine:
    def __init__(self, shadow=(), findings=(), capacity=500):
        self.shadow_detectors = tuple(shadow)
        self.shadow_findings = deque(findings, maxlen=capacity)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ep.router, prefix="/v1")
    return TestClient(app)


@pytest.fixture(autouse=True)
def enabled():
    old = settings.NL_DETECTOR_AUTHORING_ENABLED
    settings.NL_DETECTOR_AUTHORING_ENABLED = True
    yield
    settings.NL_DETECTOR_AUTHORING_ENABLED = old


def ask(client, engine, name="nl:disk-filling"):
    with patch.object(ep, "get_engine", lambda: engine):
        return client.get(f"/v1/detectors/{name}/shadow-findings")


LOADED = FakeEngine(shadow=[FakeDetector("nl:disk-filling")])
NOT_LOADED = FakeEngine()


class TestTheThreeStatesAreNowDistinguishable:
    def test_no_engine_is_not_an_empty_result(self, client):
        assert ask(client, None).status_code == 503

    def test_a_loaded_quiet_detector_answers_normally(self, client):
        r = ask(client, LOADED)
        assert r.status_code == 200
        assert r.json()["watching"] is True
        assert r.json()["findings"] == []

    def test_a_detector_this_process_never_loaded_says_so(self, client):
        r = ask(client, NOT_LOADED)
        assert r.status_code == 200
        assert r.json()["watching"] is False

    def test_the_two_two_hundreds_differ_in_the_field_that_matters(self, client):
        assert ask(client, LOADED).json()["watching"] != ask(client, NOT_LOADED).json()["watching"]


class TestThe503DoesNotClaimAnythingAboutTheDetector:
    def detail(self, client):
        return ask(client, None).json()["detail"]

    def test_it_denies_the_inference_a_reviewer_would_make(self, client):
        assert "NOT the same as" in self.detail(client)
        assert "having fired nothing" in self.detail(client)

    def test_it_says_a_decision_must_not_be_taken_on_it(self, client):
        assert "promoting or rejecting" in self.detail(client)

    def test_it_names_the_detector_it_is_refusing_to_answer_about(self, client):
        assert "nl:disk-filling" in self.detail(client)


class TestTheBufferAdmitsItIsALowerBound:
    def test_a_saturated_ring_says_so(self, client):
        eng = FakeEngine(shadow=[FakeDetector("x")],
                         findings=[FakeFinding("x", f"p{i}") for i in range(4)], capacity=4)
        assert ask(client, eng, "x").json()["buffer"] == {
            "held": 4, "capacity": 4, "saturated": True}

    def test_an_unsaturated_ring_says_so(self, client):
        eng = FakeEngine(shadow=[FakeDetector("x")],
                         findings=[FakeFinding("x")], capacity=4)
        assert ask(client, eng, "x").json()["buffer"]["saturated"] is False

    def test_the_stream_never_claims_to_be_durable(self, client):
        assert ask(client, LOADED).json()["durable"] is False

    def test_findings_for_other_detectors_are_still_excluded(self, client):
        eng = FakeEngine(shadow=[FakeDetector("x"), FakeDetector("y")],
                         findings=[FakeFinding("x"), FakeFinding("y"), FakeFinding("y")])
        assert len(ask(client, eng, "y").json()["findings"]) == 2

    def test_the_held_count_is_the_whole_ring_not_just_this_detector(self, client):
        """`held` is what the ring can still show at all — for `saturated` to mean anything."""
        eng = FakeEngine(shadow=[FakeDetector("x")],
                         findings=[FakeFinding("x"), FakeFinding("z")])
        body = ask(client, eng, "x").json()
        assert body["buffer"]["held"] == 2 and len(body["findings"]) == 1


class TestTheSiblingRuleIsWrittenDownHere:
    def test_list_detectors_still_refuses_an_empty_two_hundred(self, client):
        """The precedent this fix followed. If it goes, this endpoint's 503 loses its reason."""
        with patch.object(ep.review, "list_detectors",
                          side_effect=ep.review.DetectorStoreUnavailable("db down")):
            assert client.get("/v1/detectors").status_code == 503


class TestALoadedDetectorIsNotAlwaysAnEvaluatedOne:
    """`watching` used to mean "loaded", which is a weaker claim than it reads as.

    A detector whose only predicate is a trend predicate is evaluated by `evaluate_trends`, and
    `evaluate_trends` runs only when `PREDICTIVE_DETECTION_ENABLED` is true. On the F3 soak
    cluster two such detectors were loaded, watched-per-this-field, and evaluated by nothing —
    the same silence the whole soak was void for, one layer down.
    """

    TREND_ONLY = FakeEngine(shadow=[FakeDetector("nl:disk-filling", watch=(), trend=("cpu",))])

    @staticmethod
    @contextmanager
    def _predictive(on: bool):
        before = settings.PREDICTIVE_DETECTION_ENABLED
        settings.PREDICTIVE_DETECTION_ENABLED = on
        try:
            yield
        finally:
            settings.PREDICTIVE_DETECTION_ENABLED = before

    def test_a_trend_only_detector_is_not_watching_when_prediction_is_off(self, client):
        with self._predictive(False):
            body = ask(client, self.TREND_ONLY).json()
        assert body["watching"] is False
        assert "PREDICTIVE_DETECTION_ENABLED" in body["watching_reason"]
        assert "not evidence" in body["watching_reason"]

    def test_the_same_detector_is_watching_once_prediction_is_on(self, client):
        with self._predictive(True):
            body = ask(client, self.TREND_ONLY).json()
        assert body["watching"] is True
        assert "trend predicates" in body["watching_reason"]

    def test_a_watch_predicate_detector_does_not_depend_on_that_flag(self, client):
        with self._predictive(False):
            body = ask(client, LOADED).json()
        assert body["watching"] is True
        assert "watch predicates" in body["watching_reason"]

    def test_an_unloaded_detector_says_so_without_blaming_the_predicate(self, client):
        body = ask(client, NOT_LOADED).json()
        assert body["watching"] is False
        assert "not loaded" in body["watching_reason"]
        assert "not a statement that no such detector exists" in body["watching_reason"]

    def test_a_detector_with_no_predicates_at_all_is_not_watching(self, client):
        engine = FakeEngine(shadow=[FakeDetector("nl:disk-filling", watch=(), trend=())])
        body = ask(client, engine).json()
        assert body["watching"] is False
        assert "no evaluable predicate" in body["watching_reason"]
