"""tests/test_nl_authoring_refuses_what_cannot_fire.py

ADR-012's natural-language detector authoring was measured by a pre-registered field campaign,
and it found two defects this file pins:

(a) **A compiled detector that could not fire was reported as a success.** One run produced a
    detector with zero predicates and `errors: []`; another kept a PromQL predicate that "is
    recorded but never evaluated, so it cannot fire". `POST /v1/detectors` answered both with a
    `200`, and the forgiving parser it validated with silently dropped or rewrote whatever it could
    not use — so the detector the engine loaded was not the one the author was shown.

(b) **Compilation was not repeatable.** The same 8 English descriptions compiled 47 minutes apart
    gave different predicate counts on 4 of them (1 vs 2, 0 vs 2, 3 vs 1, 1 vs 0).

Every model output below is a FAKE that reproduces one of those shapes. Each test fails on the
code before this change and passes after it:

    zero predicates            200 {"staged": false, ...}           → 422, nothing stored
    PromQL beside a live watch staged, the query silently inert     → 422 "never evaluated"
    a field the engine ignores staged, the condition silently gone  → 422 "unknown field"
    an entry the parser drops  staged with fewer predicates          → 422 "would drop it"
    a knob the parser replaces staged with the default instead      → 422 "would load"
    compiler outage            200 "no valid predicates" (a lie)    → 502, retryable
    temperature                whatever LLM_TEMPERATURE was          → bound to 0.0 per call
    identical prose again      recompiled, possibly differently      → stored compilation reused
    "staged" with no engine    staged: true                          → 202, staged: false + why
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1.endpoints import detectors as ep
from app.core.config import settings
from app.detectors import authoring
from app.detectors import service as det_service
from app.detectors.engine import DetectorEngine, load_db_detectors
from app.memory import service as mem_service

_LIVE = {"watch_predicates": [{"kind": "Pod", "status_regex": "^OOMKilled$"}]}

# ── The shapes the campaign saw, as the model would emit them ───────────────────────────────────

ZERO_PREDICATES = "{}"
ZERO_PREDICATES_WRAPPED = json.dumps({"detect": {}})
PROMQL_BESIDE_A_WATCH = json.dumps({
    "watch_predicates": [{"kind": "Pod", "status_regex": "^CrashLoopBackOff$"}],
    "promql": ['rate(kube_pod_container_status_restarts_total[5m]) > 0'],
})
PROMQL_ONLY = json.dumps({"promql": ['kube_pod_status_phase{phase="Pending"} > 0']})
IGNORED_FIELD = json.dumps({
    "watch_predicates": [{"kind": "Pod", "status_regex": "^OOMKilled$", "namespace": "payments"}],
})
DROPPED_ENTRY = json.dumps({
    "watch_predicates": [
        {"kind": "Event", "reason_regex": "^FailedMount$"},
        {"reason_regex": "^FailedAttachVolume$"},            # no kind: the parser skips it
    ],
})
REPLACED_KNOB = json.dumps({
    "trend_predicates": [{"metric": "kubelet_volume_stats_used_bytes", "threshold": 1e9,
                          "min_r2": 1.5}],                   # the parser loads 0.5 instead
})


class FakeCompiler:
    """A chat model that returns canned text and records how it was pinned."""

    model_name = "fake-compiler"

    def __init__(self, *outputs: str):
        self.outputs = list(outputs)
        self.calls = 0
        self.bound: list[dict] = []

    def bind(self, **kwargs):
        self.bound.append(kwargs)
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        text = self.outputs[min(self.calls, len(self.outputs)) - 1]
        return SimpleNamespace(content=text)


class FakeStore:
    """The `detectors` table, answering the four statements the authoring path issues."""

    def __init__(self, rows=()):
        self.rows = [dict(r) for r in rows]
        self.inserts = 0

    async def execute(self, _sql, cluster_id, name, predicate, created_from, author):
        if any(r["cluster_id"] == cluster_id and r["name"] == name for r in self.rows):
            return "INSERT 0 0"
        self.inserts += 1
        self.rows.append({"cluster_id": cluster_id, "name": name, "source": "nl",
                          "predicate": predicate, "status": "shadow",
                          "created_from": created_from, "reviewed_by": author})
        return "INSERT 0 1"

    async def fetchrow(self, _sql, cluster_id, digest, verbatim):
        for r in reversed(self.rows):
            comp = json.loads(r["predicate"]).get("compilation")
            if r["cluster_id"] != cluster_id or r["source"] != "nl":
                continue
            if (comp and comp.get("description_sha256") == digest) or (
                    comp is None and verbatim is not None and r["created_from"] == verbatim):
                return {"name": r["name"], "predicate": r["predicate"], "status": r["status"]}
        return None

    async def fetch(self, _sql, *_args):
        return [{"name": r["name"], "predicate": r["predicate"], "status": r["status"]}
                for r in self.rows if r["status"] in ("active", "shadow")]


@pytest.fixture
def store(monkeypatch):
    s = FakeStore()
    monkeypatch.setattr(mem_service, "_pool", s)
    return s


@pytest.fixture(autouse=True)
def _authoring_on(monkeypatch):
    monkeypatch.setattr(settings, "NL_DETECTOR_AUTHORING_ENABLED", True)
    monkeypatch.setattr(settings, "PREDICTIVE_DETECTION_ENABLED", False)
    monkeypatch.setattr(ep, "get_user_role", lambda _request: "operator")
    # No playbooks in the engine's set: only what this file stores.
    monkeypatch.setattr(det_service, "load_detectors", lambda: ())
    monkeypatch.setattr(det_service, "_last_db_counts", None)


@pytest.fixture
def engine(monkeypatch):
    eng = DetectorEngine(detectors=(), cluster_id="t", on_finding=lambda _f: None)
    monkeypatch.setattr(det_service, "_engine", eng)
    return eng


@pytest.fixture
def no_engine(monkeypatch):
    monkeypatch.setattr(det_service, "_engine", None)
    monkeypatch.setattr(det_service, "_absence", det_service.STANDBY)
    monkeypatch.setattr(det_service, "_absence_detail", "another replica holds the lock")


def _compiler(monkeypatch, *outputs: str) -> FakeCompiler:
    fake = FakeCompiler(*outputs)
    monkeypatch.setattr("app.cortex.models.get_specialist_llm", lambda: fake)
    return fake


async def _post(body: dict):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(ep.router, prefix="/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.post("/v1/detectors", json=body)


# ── (a) a detector that cannot fire is refused, never staged ────────────────────────────────────

class TestTheGateRefusesWhatCannotFire:
    @pytest.mark.parametrize("output", [ZERO_PREDICATES, ZERO_PREDICATES_WRAPPED])
    async def test_zero_predicates_is_a_422_and_nothing_is_stored(
            self, output, monkeypatch, store, engine):
        _compiler(monkeypatch, output)
        r = await _post({"description": "pods are unhappy"})
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["staged"] is False and body["stored"] is False
        assert body["errors"], "a refusal with errors: [] is the campaign's exact defect"
        assert store.inserts == 0

    async def test_promql_beside_a_live_watch_is_refused_not_staged_inert(
            self, monkeypatch, store, engine):
        """The watch half would load and fire; the PromQL half never would, while the author
        is shown it as part of the detector. Before: staged, 200."""
        _compiler(monkeypatch, PROMQL_BESIDE_A_WATCH)
        r = await _post({"description": "a container restarts in a loop"})
        assert r.status_code == 422, r.text
        assert any("never evaluated" in e for e in r.json()["errors"])
        assert store.inserts == 0

    async def test_promql_only_is_refused(self, monkeypatch, store, engine):
        _compiler(monkeypatch, PROMQL_ONLY)
        r = await _post({"description": "pods pending"})
        assert r.status_code == 422
        assert "never evaluated" in r.json()["errors"][0]

    @pytest.mark.parametrize(("output", "needle"), [
        (IGNORED_FIELD, "unknown field 'namespace'"),
        (DROPPED_ENTRY, "has no kind"),
        (REPLACED_KNOB, "would load 0.5"),
    ])
    def test_what_the_parser_would_drop_or_rewrite_is_refused_by_name(self, output, needle):
        block, errors = authoring.validate_detect_block(json.loads(output), "nl")
        assert block is None, f"accepted a detector the engine would load differently: {output}"
        assert any(needle in e for e in errors), errors

    def test_the_forged_provenance_key_is_refused(self):
        raw = {**_LIVE, "compilation": {"temperature": 0.0}}
        block, errors = authoring.validate_detect_block(raw, "nl")
        assert block is None and any("unknown key 'compilation'" in e for e in errors)

    async def test_trend_only_with_predictive_detection_off_is_refused(
            self, monkeypatch, store, engine):
        _compiler(monkeypatch, json.dumps({"trend_predicates": [
            {"metric": "kubelet_volume_stats_used_bytes", "threshold": 1e9}]}))
        r = await _post({"description": "a volume is filling up"})
        assert r.status_code == 422
        assert "PREDICTIVE_DETECTION_ENABLED" in r.json()["detail"]
        assert store.inserts == 0

    async def test_a_compiler_outage_is_a_502_not_an_empty_detector(
            self, monkeypatch, store, engine):
        def _boom():
            raise RuntimeError("connection refused")

        monkeypatch.setattr("app.cortex.models.get_specialist_llm", _boom)
        r = await _post({"description": "pods getting OOM killed"})
        assert r.status_code == 502
        assert "could not be called" in r.json()["detail"]
        assert store.inserts == 0

    async def test_an_unreadable_store_spends_no_compilation(self, monkeypatch, engine):
        monkeypatch.setattr(mem_service, "_pool", None)
        fake = _compiler(monkeypatch, json.dumps(_LIVE))
        r = await _post({"description": "pods getting OOM killed"})
        assert r.status_code == 503
        assert fake.calls == 0

    def test_whatever_the_gate_accepts_the_engine_loads_whole(self):
        """The gate and the loader read the same schema: an accepted block loads with every
        predicate it was written with, and nothing dropped."""
        accepted = [
            _LIVE,
            {"watch_predicates": [{"kind": "Event", "reason_regex": "^FailedMount$",
                                   "message_regex": "timed out", "involved_kind": "Pod"}],
             "debounce_seconds": 60},
            {"trend_predicates": [{"metric": 'kube_deployment_status_replicas{deployment="api"}',
                                   "threshold": 0, "direction": "falling", "min_r2": 0}]},
        ]
        for raw in accepted:
            block, errors = authoring.validate_detect_block(raw, "nl")
            assert block is not None and errors == [], (raw, errors)
            assert len(block.watch_predicates) == len(raw.get("watch_predicates", []))
            assert len(block.trend_predicates) == len(raw.get("trend_predicates", []))

    async def test_whatever_the_gate_accepts_load_db_detectors_does_not_trim(self, monkeypatch):
        raw = {"watch_predicates": [{"kind": "Pod", "status_regex": "^CrashLoopBackOff$"},
                                    {"kind": "Event", "reason_regex": "^BackOff$"}]}
        assert authoring.validate_detect_block(raw, "nl")[0] is not None
        store = FakeStore([{"cluster_id": "global", "name": "nl:x", "source": "nl",
                            "predicate": json.dumps(raw), "status": "shadow",
                            "created_from": "x"}])
        monkeypatch.setattr(mem_service, "_pool", store)
        _active, shadow = await load_db_detectors("global")
        assert len(shadow) == 1
        assert shadow[0].dropped_predicates == ()
        assert len(shadow[0].watch_predicates) == 2


# ── staging is read off the engine, not assumed from the INSERT ─────────────────────────────────

class TestStagedMeansLoaded:
    async def test_a_loaded_detector_is_staged(self, monkeypatch, store, engine):
        _compiler(monkeypatch, json.dumps(_LIVE))
        r = await _post({"description": "pods getting OOM killed", "name": "nl:oom"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["staged"] is True and body["stored"] is True
        assert any(d.playbook == "nl:oom" for d in engine.shadow_detectors), \
            "staged: true was answered for a detector the engine does not hold"

    async def test_no_engine_here_is_202_staged_false_with_the_reason(
            self, monkeypatch, store, no_engine):
        """Before: `staged: true` because the INSERT succeeded."""
        _compiler(monkeypatch, json.dumps(_LIVE))
        r = await _post({"description": "pods getting OOM killed", "name": "nl:oom"})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["staged"] is False and body["stored"] is True
        assert "standby" in body["staged_reason"]
        assert store.inserts == 1

    async def test_a_refresh_that_does_not_load_it_is_not_staged(
            self, monkeypatch, store, engine):
        async def _failing_load(_cluster_id):
            from app.detectors.review import DetectorStoreUnavailable
            raise DetectorStoreUnavailable("dns blip")

        monkeypatch.setattr(det_service, "load_db_detectors", _failing_load)
        _compiler(monkeypatch, json.dumps(_LIVE))
        r = await _post({"description": "pods getting OOM killed", "name": "nl:oom"})
        assert r.status_code == 202
        assert r.json()["staged"] is False
        assert "not in the engine's shadow set" in r.json()["staged_reason"]

    async def test_a_taken_name_is_a_409_not_a_silent_not_staged(
            self, monkeypatch, store, engine):
        _compiler(monkeypatch, json.dumps(_LIVE))
        first = await _post({"description": "pods getting OOM killed", "name": "nl:oom"})
        assert first.status_code == 200
        _compiler(monkeypatch, json.dumps({"watch_predicates": [
            {"kind": "Pod", "status_regex": "^Evicted$"}]}))
        r = await _post({"description": "pods evicted", "name": "nl:oom"})
        assert r.status_code == 409
        assert "already exists" in r.json()["detail"]


# ── (b) determinism: pinned temperature, stored compilation, reuse ──────────────────────────────

class TestCompilationIsPinnedAndStored:
    async def test_the_compiler_is_called_at_temperature_zero(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_TEMPERATURE", 1.0)   # the rest of the product runs warm
        fake = _compiler(monkeypatch, json.dumps(_LIVE))
        raw, provenance = await authoring.compile_nl_to_detect_block("pods getting OOM killed")
        assert fake.bound == [{"temperature": 0.0}]
        assert provenance["temperature"] == 0.0
        assert provenance["model"] == "fake-compiler"
        assert raw == _LIVE

    async def test_the_compilation_is_stored_with_its_provenance(
            self, monkeypatch, store, engine):
        _compiler(monkeypatch, json.dumps(_LIVE))
        await _post({"description": "pods getting OOM killed", "name": "nl:oom"})
        stored = json.loads(store.rows[0]["predicate"])
        assert stored["watch_predicates"] == _LIVE["watch_predicates"]
        assert stored["compilation"]["temperature"] == 0.0
        assert stored["compilation"]["description_sha256"] == \
            authoring.description_digest("pods getting OOM killed")

    async def test_identical_prose_reuses_the_stored_compilation(
            self, monkeypatch, store, engine):
        """The campaign's 47-minutes-apart case: the second submission would have compiled
        differently. Here the model's second answer is DIFFERENT on purpose — and never asked."""
        fake = _compiler(monkeypatch, json.dumps(_LIVE), json.dumps({"watch_predicates": [
            {"kind": "Pod", "status_regex": "^OOMKilled$"},
            {"kind": "Event", "reason_regex": "^OOMKilling$"}]}))
        first = await _post({"description": "pods getting OOM killed", "name": "nl:oom"})
        second = await _post({"description": "  pods getting   OOM killed "})
        assert first.status_code == 200 and second.status_code == 200, second.text
        assert fake.calls == 1, "identical prose was recompiled"
        body = second.json()
        assert body["reused"] is True
        assert body["compilation"]["source"] == "stored"
        assert body["name"] == "nl:oom"
        assert body["compiled"] == first.json()["compiled"]
        assert store.inserts == 1

    async def test_recompile_is_fresh_and_labelled_so(self, monkeypatch, store, engine):
        fake = _compiler(monkeypatch, json.dumps(_LIVE), json.dumps({"watch_predicates": [
            {"kind": "Event", "reason_regex": "^OOMKilling$"}]}))
        await _post({"description": "pods getting OOM killed", "name": "nl:oom"})
        r = await _post({"description": "pods getting OOM killed", "name": "nl:oom-v2",
                         "recompile": True})
        assert r.status_code == 200, r.text
        assert fake.calls == 2
        assert r.json()["compilation"]["source"] == "fresh"
        assert "reused" not in r.json()

    async def test_a_stored_compilation_is_regated_not_trusted(self, monkeypatch, engine):
        """A row stored before the gate existed (no provenance, PromQL inside) must not come
        back as a success just because it is not being recompiled."""
        legacy = {**_LIVE, "promql": ["up == 0"]}
        store = FakeStore([{"cluster_id": "global", "name": "nl:legacy", "source": "nl",
                            "predicate": json.dumps(legacy), "status": "shadow",
                            "created_from": "pods getting OOM killed"}])
        monkeypatch.setattr(mem_service, "_pool", store)
        fake = _compiler(monkeypatch, json.dumps(_LIVE))
        r = await _post({"description": "pods getting OOM killed"})
        assert r.status_code == 422
        assert fake.calls == 0
        assert "recompile=true" in r.json()["detail"]

    async def test_a_demoted_compilation_is_not_reported_as_staged(self, monkeypatch, engine):
        store = FakeStore([{"cluster_id": "global", "name": "nl:oom", "source": "nl",
                            "predicate": json.dumps(_LIVE), "status": "demoted",
                            "created_from": "pods getting OOM killed"}])
        monkeypatch.setattr(mem_service, "_pool", store)
        _compiler(monkeypatch, json.dumps(_LIVE))
        r = await _post({"description": "pods getting OOM killed"})
        assert r.status_code == 409
        assert "demoted" in r.json()["detail"]

    def test_the_prompt_no_longer_offers_promql_as_an_output(self):
        assert '"promql": OPTIONAL' not in authoring._AUTHORING_SYSTEM
        assert 'Do NOT emit "promql"' in authoring._AUTHORING_SYSTEM
