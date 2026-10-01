"""`promql:` predicates in a `detect:` block are EVALUATED (#20) — and say so when they cannot be.

Before #20 the queries were parsed, stored and exported, and nothing ran them; a promql-only
detector was refused because it would have loaded and never fired. The engine now runs each
instant query on its own interval (`PROMQL_DETECTION_ENABLED`, `PROMQL_DETECTION_INTERVAL_SECONDS`)
and fires on every element of the result vector, Prometheus alerting-rule style.

The failure this file is mostly about is the quiet one: a Prometheus that is unconfigured,
unreachable, timing out or answering with an error must never look like "the condition does not
hold". Every Prometheus answer below comes from a FAKE reader patched over
`engine.query_prometheus_vector` (the engine's only door to Prometheus), so these tests need no
cluster. Before this change: no detector fired from `promql`, `evaluate_promql` did not exist,
the authoring gate refused every `promql` entry, and `GET /v1/findings` had no `promql` field.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.core.config import settings
from app.detectors import authoring
from app.detectors import engine as engine_mod
from app.detectors import perception
from app.detectors.engine import DetectorEngine, load_db_detectors
from app.detectors.models import EVALUATED_PREDICATE_KEYS, parse_detect_block
from app.detectors.predicate_shape import promql_shape_errors
from app.memory import service as mem_service
from app.tools import prometheus_tool as prom

CRASHLOOP = 'kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"} == 1'
PENDING = 'kube_persistentvolumeclaim_status_phase{phase="Pending"} == 1'


def _series(namespace: str, pod: str, value: str = "1", **labels) -> dict:
    return {"metric": {"__name__": "x", "namespace": namespace, "pod": pod, **labels},
            "value": [1700000000.0, value]}


class FakePrometheus:
    """Stands in for `query_prometheus_vector`: per-query canned answers, and a call log."""

    def __init__(self, answers: dict | None = None, default=([], None)):
        self.answers = answers or {}
        self.default = default
        self.calls: list[str] = []

    def __call__(self, query: str):
        self.calls.append(query)
        answer = self.answers.get(query, self.default)
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def fake_prom(monkeypatch):
    fake = FakePrometheus()
    monkeypatch.setattr(engine_mod, "query_prometheus_vector", fake)
    monkeypatch.setattr(prom, "query_prometheus_vector", fake)  # the authoring probe's door
    return fake


@pytest.fixture
def promql_on(monkeypatch):
    monkeypatch.setattr(settings, "PROMQL_DETECTION_ENABLED", True)
    monkeypatch.setattr(settings, "PROMETHEUS_URL", "http://prometheus.test:9090")


@pytest.fixture
def promql_off(monkeypatch):
    monkeypatch.setattr(settings, "PROMQL_DETECTION_ENABLED", False)
    monkeypatch.setattr(settings, "PROMETHEUS_URL", "http://prometheus.test:9090")


def _engine(*blocks, shadow=(), on_finding=None) -> DetectorEngine:
    return DetectorEngine(detectors=tuple(blocks), shadow_detectors=tuple(shadow),
                          cluster_id="t", on_finding=on_finding)


def _block(name="CrashLoopBackOff", queries=(CRASHLOOP,), debounce=0, **extra):
    block = parse_detect_block(name, {"promql": list(queries), "debounce_seconds": debounce,
                                      **extra})
    assert block is not None, "a promql-only block is a detector now"
    return block


def _run(eng, now):
    return asyncio.run(eng.evaluate_promql(now=now))


# ── Firing semantics ────────────────────────────────────────────────────────────────────────────

class TestItFires:
    def test_a_non_empty_result_fires_one_finding_per_object(self, fake_prom):
        fake_prom.answers[CRASHLOOP] = ([_series("shop", "api-1"), _series("shop", "api-2")],
                                        None)
        seen = []
        eng = _engine(_block(), on_finding=seen.append)
        fired = _run(eng, 1000.0)
        assert sorted((f.namespace, f.object_name) for f in fired) == [
            ("shop", "api-1"), ("shop", "api-2")]
        assert all(f.source == "promql" for f in fired)
        assert "CrashLoopBackOff" in fired[0].evidence and "= 1" in fired[0].evidence
        assert len(seen) == 2, "an active PromQL finding reaches the watchtower like any other"

    def test_an_empty_result_does_not_fire_and_is_not_blindness(self, fake_prom):
        eng = _engine(_block())
        assert _run(eng, 1000.0) == []
        assert eng.promql_blind_since is None and eng.last_promql_error is None
        assert eng.promql_last_sweep_at == 1000.0, "it looked — and found nothing"

    def test_a_fired_object_does_not_refire_while_the_condition_holds(self, fake_prom):
        fake_prom.answers[CRASHLOOP] = ([_series("shop", "api-1")], None)
        eng = _engine(_block())
        assert len(_run(eng, 1000.0)) == 1
        assert _run(eng, 1030.0) == []
        assert _run(eng, 1060.0) == []

    def test_it_refires_after_a_complete_sweep_saw_it_clear(self, fake_prom):
        fake_prom.answers[CRASHLOOP] = ([_series("shop", "api-1")], None)
        eng = _engine(_block())
        assert len(_run(eng, 1000.0)) == 1
        fake_prom.answers[CRASHLOOP] = ([], None)
        assert _run(eng, 1030.0) == []
        fake_prom.answers[CRASHLOOP] = ([_series("shop", "api-1")], None)
        assert len(_run(eng, 1060.0)) == 1, "a recurrence is a new incident"

    def test_debounce_is_honoured_across_evaluations(self, fake_prom):
        fake_prom.answers[CRASHLOOP] = ([_series("shop", "api-1")], None)
        eng = _engine(_block(debounce=60))
        assert _run(eng, 1000.0) == [], "armed, not fired"
        assert _run(eng, 1030.0) == [], "30s < 60s debounce"
        fired = _run(eng, 1060.0)
        assert len(fired) == 1 and fired[0].first_seen == 1000.0

    def test_a_condition_that_clears_inside_the_debounce_never_fires(self, fake_prom):
        fake_prom.answers[CRASHLOOP] = ([_series("shop", "api-1")], None)
        eng = _engine(_block(debounce=60))
        _run(eng, 1000.0)
        fake_prom.answers[CRASHLOOP] = ([], None)
        _run(eng, 1030.0)
        fake_prom.answers[CRASHLOOP] = ([_series("shop", "api-1")], None)
        assert _run(eng, 1060.0) == [], "re-armed at 1060, so the debounce starts again"

    def test_the_one_second_tick_never_fires_a_promql_armed_key(self, fake_prom):
        """Firing on the clock would fire on a condition last confirmed one interval ago."""
        fake_prom.answers[CRASHLOOP] = ([_series("shop", "api-1")], None)
        eng = _engine(_block(debounce=60))
        _run(eng, 1000.0)
        assert eng.tick(now=1100.0) == []

    def test_two_queries_of_one_detector_fire_once_per_object(self, fake_prom):
        restarts = "increase(kube_pod_container_status_restarts_total[10m]) > 3"
        fake_prom.answers[CRASHLOOP] = ([_series("shop", "api-1")], None)
        fake_prom.answers[restarts] = ([_series("shop", "api-1", value="7")], None)
        eng = _engine(_block(queries=(CRASHLOOP, restarts)))
        assert len(_run(eng, 1000.0)) == 1

    def test_series_naming_no_known_object_do_not_share_a_key(self, fake_prom):
        """Two distinct series must never collapse into one "unknown" debounce key."""
        q = "some_cluster_metric > 0"
        fake_prom.answers[q] = ([{"metric": {"shard": "a"}, "value": [0, "1"]},
                                 {"metric": {"shard": "b"}, "value": [0, "1"]}], None)
        eng = _engine(_block(queries=(q,)))
        assert len(_run(eng, 1000.0)) == 2

    def test_a_deployment_series_is_named_by_its_deployment(self, fake_prom):
        q = "kube_deployment_spec_replicas != kube_deployment_status_replicas_available"
        fake_prom.answers[q] = ([{"metric": {"namespace": "shop", "deployment": "web"},
                                  "value": [0, "3"]}], None)
        (f,) = _run(_engine(_block("DeploymentRolloutStuck", queries=(q,))), 1000.0)
        assert (f.namespace, f.object_name) == ("shop", "web")

    def test_a_shadow_detector_fires_into_the_shadow_buffer_only(self, fake_prom):
        fake_prom.answers[PENDING] = ([{"metric": {"namespace": "db",
                                                   "persistentvolumeclaim": "data-0"},
                                        "value": [0, "1"]}], None)
        seen = []
        eng = _engine(shadow=(_block("nl:pvc", queries=(PENDING,)),), on_finding=seen.append)
        assert _run(eng, 1000.0) == []
        assert [f.object_name for f in eng.shadow_findings] == ["data-0"]
        assert seen == [] and list(eng.findings) == [], "ADR-012: shadow never reaches action"


# ── Error state: never "did not fire" ───────────────────────────────────────────────────────────

class TestAQueryThatCouldNotRunIsBlindNotQuiet:
    @pytest.mark.parametrize("error", [
        "Prometheus is not configured. Set PROMETHEUS_URL in ~/.kubeintellect/.env and restart.",
        "Cannot reach Prometheus at http://prometheus.test:9090.",
        "Prometheus query timed out (15s).",
        "Prometheus error: parse error: unexpected end of input",
        "Prometheus HTTP 503: service unavailable",
    ])
    def test_every_failure_shape_sets_the_blind_state(self, fake_prom, error):
        fake_prom.answers[CRASHLOOP] = ([], error)
        eng = _engine(_block())
        assert _run(eng, 1000.0) == []
        assert eng.promql_blind_since == 1000.0, "an error is not an empty result"
        assert error.split(".")[0][:30] in (eng.last_promql_error or "")
        assert "CrashLoopBackOff" in eng.last_promql_error, "it names the detector"

    def test_a_reader_that_raises_is_blind_too(self, fake_prom):
        fake_prom.answers[CRASHLOOP] = TimeoutError("read timed out")
        eng = _engine(_block())
        assert _run(eng, 1000.0) == []
        assert eng.promql_blind_since == 1000.0
        assert "read timed out" in eng.last_promql_error

    def test_one_failing_query_among_answering_ones_still_blinds_the_sweep(self, fake_prom):
        fake_prom.answers[PENDING] = ([], "Prometheus query timed out (15s).")
        eng = _engine(_block(), _block("PvcPending", queries=(PENDING,)))
        _run(eng, 1000.0)
        assert eng.promql_blind_since == 1000.0
        assert "1 of 2" in eng.last_promql_error

    def test_a_failed_sweep_does_not_clear_an_armed_key(self, fake_prom):
        """Absence is only provable by a query that ran."""
        fake_prom.answers[CRASHLOOP] = ([_series("shop", "api-1")], None)
        eng = _engine(_block(debounce=60))
        _run(eng, 1000.0)
        fake_prom.answers[CRASHLOOP] = ([], "Cannot reach Prometheus at http://p.")
        _run(eng, 1030.0)
        fake_prom.answers[CRASHLOOP] = ([_series("shop", "api-1")], None)
        fired = _run(eng, 1060.0)
        assert len(fired) == 1 and fired[0].first_seen == 1000.0

    def test_blindness_clears_when_every_query_answers_again(self, fake_prom):
        fake_prom.answers[CRASHLOOP] = ([], "Prometheus query timed out (15s).")
        eng = _engine(_block())
        _run(eng, 1000.0)
        fake_prom.answers[CRASHLOOP] = ([], None)
        _run(eng, 1030.0)
        assert eng.promql_blind_since is None and eng.last_promql_error is None

    def test_the_stored_reason_is_redacted(self, fake_prom):
        fake_prom.answers[CRASHLOOP] = ([], "Prometheus HTTP 401: bad token=sk-abcdef0123456789abcdef")
        eng = _engine(_block())
        _run(eng, 1000.0)
        assert "sk-abcdef0123456789abcdef" not in eng.last_promql_error

    def test_perception_reports_blind_and_names_the_gap(self, fake_prom, promql_on):
        fake_prom.answers[CRASHLOOP] = ([], "Cannot reach Prometheus at http://p.")
        eng = _engine(_block())
        _run(eng, 1000.0)
        state = perception.perception_state(eng)
        assert state.promql == perception.BLIND
        assert "Cannot reach Prometheus" in (state.promql_error or "")
        assert any("PromQL detection is blind" in g for g in perception.perception_gaps(state))

    def test_perception_does_not_call_it_active_before_the_first_sweep(self, promql_on):
        state = perception.perception_state(_engine(_block()))
        assert state.promql == perception.STARTING
        assert any("first evaluation" in g for g in perception.perception_gaps(state))

    def test_perception_is_off_not_blind_when_the_flag_is_off(self, promql_off):
        state = perception.perception_state(_engine(_block()))
        assert state.promql == perception.OFF
        assert not any("PromQL" in g for g in perception.perception_gaps(state))

    def test_the_findings_endpoint_carries_the_promql_state(self, fake_prom, promql_on,
                                                            monkeypatch):
        from app.api.v1.endpoints.findings import list_findings
        from app.detectors import service
        from app.sensorium import k8s_watcher
        from app.sensorium.k8s_watcher import StreamHealth, reset_stream_health

        fake_prom.answers[CRASHLOOP] = ([], "Prometheus query timed out (15s).")
        eng = _engine(_block())
        _run(eng, 1000.0)
        reset_stream_health()
        health = StreamHealth("pods")
        health.connected = True
        k8s_watcher._streams["pods"] = health
        monkeypatch.setattr(service, "_engine", eng)
        try:
            payload = asyncio.run(list_findings(limit=100, since=0.0))
        finally:
            reset_stream_health()
        assert payload["promql"] == "blind"
        assert payload["promql_detectors"] == 1
        assert "timed out" in payload["promql_error"]


class TestTheReaderRefusesShapesItCannotFireOn:
    def test_a_matrix_answer_is_an_error_not_an_empty_vector(self, monkeypatch):
        monkeypatch.setattr(prom, "_query_typed",
                            lambda q, r: ("matrix", [{"metric": {}, "values": []}], None))
        series, error = prom.query_prometheus_vector("x[5m]")
        assert series == [] and "range vector" in error

    def test_a_scalar_answer_is_an_error(self, monkeypatch):
        monkeypatch.setattr(prom, "_query_typed", lambda q, r: ("scalar", [0, "1"], None))
        series, error = prom.query_prometheus_vector("scalar(x)")
        assert series == [] and "no labelled series" in error

    def test_a_vector_answer_passes_through(self, monkeypatch):
        rows = [_series("a", "b")]
        monkeypatch.setattr(prom, "_query_typed", lambda q, r: ("vector", rows, None))
        assert prom.query_prometheus_vector("x == 1") == (rows, None)

    def test_an_unconfigured_prometheus_is_an_error(self, monkeypatch):
        monkeypatch.setattr(settings, "PROMETHEUS_URL", "")
        series, error = prom.query_prometheus_vector("up == 0")
        assert series == [] and "not configured" in error


# ── Static shape gate ───────────────────────────────────────────────────────────────────────────

class TestTheShapeGate:
    @pytest.mark.parametrize(("expr", "needle"), [
        ("", "empty"),
        ("x" * 2001, "limit"),
        ("up > bool 0", "bool"),
        ("kube_pod_status_ready[5m]", "bare range selector"),
        ("max_over_time(x[5m:1m])[10m:1m]", "bare range selector"),
        ("rate(x[])", "no valid duration"),
        ("rate(x[400d]) > 0", "more than 24h"),
        ('x{deployment="your-deployment-name"} == 1', "unfilled template"),
        ("sum(rate(x[5m])", "unbalanced"),
        ('x{a="b} == 1', "unterminated"),
        (42, "must be a string"),
    ])
    def test_it_refuses_with_a_reason(self, expr, needle):
        errors = promql_shape_errors(expr)
        assert errors and any(needle in e for e in errors), errors

    @pytest.mark.parametrize("expr", [
        CRASHLOOP,
        'max_over_time(x{a="[bool]"}[5m:1m]) > 0',
        "increase(kube_pod_container_status_restarts_total[10m]) > 3",
        'kube_service_spec_type{type!="ExternalName"} unless on(namespace, service) '
        'label_replace(kube_endpoint_address{ready="true"}, "service", "$1", "endpoint", "(.*)")',
    ])
    def test_it_accepts_ordinary_filters(self, expr):
        assert promql_shape_errors(expr) == []

    def test_every_shipped_query_passes(self):
        from app.agent.playbooks.loader import list_playbooks

        bad = {(p.name, q): promql_shape_errors(q)
               for p in list_playbooks() if p.detect is not None
               for q in p.detect.promql if promql_shape_errors(q)}
        assert bad == {}


# ── The gate and the engine agree ───────────────────────────────────────────────────────────────

class TestAuthoringAndLoading:
    def test_promql_is_an_evaluated_key_in_the_one_shared_list(self):
        assert "promql" in EVALUATED_PREDICATE_KEYS
        assert engine_mod._is_detect_block({"promql": [CRASHLOOP]}) is True

    def test_valid_promql_validates_and_passes_the_deployment_gate_when_evaluated(
            self, promql_on):
        block, errors = authoring.validate_detect_block({"promql": [CRASHLOOP]}, "nl:x")
        assert errors == [] and block is not None and block.promql == (CRASHLOOP,)
        assert authoring.deployment_errors(block) == []

    @pytest.mark.parametrize(("flag", "url", "needle"), [
        (False, "http://prometheus.test:9090", "PROMQL_DETECTION_ENABLED is false"),
        (True, "", "PROMETHEUS_URL is not set"),
    ])
    def test_it_is_refused_with_the_precise_reason_where_nothing_evaluates_it(
            self, monkeypatch, flag, url, needle):
        monkeypatch.setattr(settings, "PROMQL_DETECTION_ENABLED", flag)
        monkeypatch.setattr(settings, "PROMETHEUS_URL", url)
        block, _ = authoring.validate_detect_block(
            {"watch_predicates": [{"kind": "Pod", "status_regex": "^CrashLoopBackOff$"}],
             "promql": [CRASHLOOP]}, "nl:x")
        errors = authoring.deployment_errors(block)
        assert errors and needle in errors[0] and "never evaluated" in errors[0]

    def test_a_malformed_query_is_refused_by_the_schema_gate(self, promql_on):
        block, errors = authoring.validate_detect_block({"promql": ["up > bool 0"]}, "nl:x")
        assert block is None and errors and errors[0].startswith("promql[0]:")

    def test_the_probe_refuses_a_query_prometheus_rejects(self, fake_prom, promql_on):
        fake_prom.answers[CRASHLOOP] = ([], "Prometheus error: parse error at char 3")
        errors = asyncio.run(authoring.promql_probe_errors(_block()))
        assert errors and "parse error" in errors[0]

    def test_the_probe_refuses_when_prometheus_cannot_be_asked(self, fake_prom, promql_on):
        fake_prom.answers[CRASHLOOP] = ([], "Cannot reach Prometheus at http://p.")
        assert asyncio.run(authoring.promql_probe_errors(_block()))

    def test_the_probe_accepts_an_empty_answer(self, fake_prom, promql_on):
        assert asyncio.run(authoring.promql_probe_errors(_block())) == []

    def test_the_prompt_offers_promql_only_where_it_runs(self, monkeypatch):
        monkeypatch.setattr(settings, "PROMETHEUS_URL", "http://p:9090")
        monkeypatch.setattr(settings, "PROMQL_DETECTION_ENABLED", False)
        assert 'Do NOT emit "promql"' in authoring._authoring_system()
        monkeypatch.setattr(settings, "PROMQL_DETECTION_ENABLED", True)
        on = authoring._authoring_system()
        assert 'Do NOT emit "promql"' not in on and '"promql": list of INSTANT' in on

    def test_a_promql_only_db_row_loads_where_promql_runs(self, monkeypatch, promql_on):
        rows = [{"name": "nl:pvc", "predicate": json.dumps({"promql": [PENDING]}),
                 "status": "shadow"}]
        monkeypatch.setattr(mem_service, "_pool", _Rows(rows))
        active, shadow = asyncio.run(load_db_detectors("t"))
        assert [d.playbook for d in shadow] == ["nl:pvc"] and shadow[0].promql == (PENDING,)

    def test_a_promql_only_db_row_is_not_loaded_where_nothing_runs_it(
            self, monkeypatch, promql_off):
        rows = [{"name": "nl:pvc", "predicate": json.dumps({"promql": [PENDING]}),
                 "status": "shadow"}]
        monkeypatch.setattr(mem_service, "_pool", _Rows(rows))
        assert asyncio.run(load_db_detectors("t")) == ((), ())

    def test_promql_beside_a_watch_is_dropped_with_the_reason_where_nothing_runs_it(
            self, monkeypatch, promql_off):
        pred = {"watch_predicates": [{"kind": "Pod", "status_regex": "^CrashLoopBackOff$"}],
                "promql": [CRASHLOOP]}
        monkeypatch.setattr(mem_service, "_pool", _Rows([
            {"name": "nl:both", "predicate": json.dumps(pred), "status": "shadow"}]))
        _, (block,) = asyncio.run(load_db_detectors("t"))
        assert block.promql == () and block.watch_predicates
        assert any("PROMQL_DETECTION_ENABLED is false" in d for d in block.dropped_predicates)

    def test_a_promql_only_playbook_is_not_loaded_where_nothing_runs_it(
            self, monkeypatch, promql_off):
        from types import SimpleNamespace

        import app.agent.playbooks as playbooks

        pbs = [SimpleNamespace(detect=_block("PromqlOnly")),
               SimpleNamespace(detect=parse_detect_block("Watch", {"watch_predicates": [
                   {"kind": "Pod", "status_regex": "^CrashLoopBackOff$"}]}))]
        monkeypatch.setattr(playbooks, "list_playbooks", lambda: pbs)
        assert [d.playbook for d in engine_mod.load_detectors()] == ["Watch"]


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, *_args):
        return self.rows
