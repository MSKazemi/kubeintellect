"""Prometheus says what shape it returned; the tool must not guess it from its own arguments.

Pass 87 of the standing audit (T38), reached by re-asking pass 86's question — *did this tool
guess at something the datasource had already told it?* — of the sibling observability tool.

`query_prometheus` discarded `data.resultType` entirely and chose its renderer from
`range_minutes`, the **caller's** parameter. Prometheus's `/api/v1/query` answers with four
different shapes, and only one of them was handled:

===================  ===========================================  ===========================
`resultType`         when                                          what the tool did
===================  ===========================================  ===========================
`vector`             an ordinary instant query                     correct
`matrix`             the expression carries a range selector,      read `r["value"]`, which a
                     e.g. ``container_cpu_..._total{...}[5m]``     matrix entry has not, and
                                                                   printed ``= N/A`` for every
                                                                   series over live data
`scalar`             ``time()``, ``scalar(count(up))``             ``AttributeError: 'int'
`string`             a string literal                               object has no attribute
                                                                   'get'`` — raised from inside
                                                                   the namespace filter
===================  ===========================================  ===========================

All measured 2026-08-20 against the real tool with a stubbed HTTP client. A range selector in an
instant query is not exotic: `query_prometheus`'s own docstring examples use `rate(x[5m])`, and
`metric[5m]` without a function is the ordinary way to ask "show me the raw samples".

Two of these are worse than a wrong number. `= N/A` is the shape of *no data*, so the agent was
handed a confident "this metric is empty" over samples that were right there. And the exception
came from `series_labels`, i.e. **the namespace guard — the thing whose job is to make an answer
safe — was what destroyed it.**

The deterministic consumers were checked and are *not* affected: trend predicates always issue
range queries, and `_default_scalar` wraps its call in `except Exception: return None`, which
pass 79 made surface as `metrics-unavailable` rather than a zero.
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.tools import prometheus_tool as pt
from app.tools.namespace_guard import series_labels

VECTOR = {"status": "success", "data": {"resultType": "vector", "result": [
    {"metric": {"pod": "payment-api-0"}, "value": [1, "0.42"]}]}}
MATRIX = {"status": "success", "data": {"resultType": "matrix", "result": [
    {"metric": {"pod": "payment-api-0", "namespace": "shop"},
     "values": [[1, "0.42"], [2, "0.55"], [3, "0.91"]]},
    {"metric": {"pod": "coredns-1", "namespace": "kube-system"}, "values": [[1, "9.10"]]}]}}
SCALAR = {"status": "success", "data": {"resultType": "scalar", "result": [1755000000, "7"]}}
STRING = {"status": "success", "data": {"resultType": "string", "result": [1755000000, "hi"]}}


@pytest.fixture
def prom(monkeypatch):
    """Stub the HTTP client; `payload` selects what Prometheus answers."""
    box: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return box["payload"]

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, _url, params=None):
            box["params"] = params
            return _Resp()

    monkeypatch.setattr(pt.httpx, "Client", _Client)
    monkeypatch.setattr(settings, "PROMETHEUS_URL", "http://prom.test:9090")
    return box


def _run(prom, payload, promql, range_minutes=0):
    prom["payload"] = payload
    return pt.query_prometheus.func(promql, range_minutes)


# ── 1. A matrix from an instant query ─────────────────────────────────────────

class TestARangeSelectorIsNotEmpty:
    def test_the_values_are_rendered_not_reported_as_N_A(self, prom):
        out = _run(prom, MATRIX, 'container_cpu_usage_seconds_total{pod=~"api.*"}[5m]')
        assert "N/A" not in out, "live samples reported as no data"
        assert "min=0.4200" in out and "max=0.9100" in out

    def test_it_says_the_span_came_from_a_range_selector(self, prom):
        out = _run(prom, MATRIX, 'x[5m]')
        assert "range selector" in out
        assert "range 0m" not in out

    def test_the_namespace_filter_still_applies(self, prom):
        out = _run(prom, MATRIX, 'x[5m]')
        assert "9.10" not in out and "coredns" not in out
        assert "withheld" in out

    def test_an_explicit_range_query_still_names_its_window(self, prom):
        out = _run(prom, MATRIX, 'rate(x[5m])', 60)
        assert "range 60m" in out


# ── 2. Scalar and string ──────────────────────────────────────────────────────

class TestAScalarIsAnAnswerNotACrash:
    @pytest.mark.parametrize("payload,promql,shown", [
        (SCALAR, "scalar(count(up))", "7"),
        (STRING, '"hi"', "hi"),
    ])
    def test_it_returns_the_value(self, prom, payload, promql, shown):
        out = _run(prom, payload, promql)
        assert f"= {shown}" in out

    def test_it_names_the_result_type(self, prom):
        assert "PromQL (scalar)" in _run(prom, SCALAR, "time()")

    def test_it_does_not_raise(self, prom):
        """The regression: the namespace guard raised AttributeError out of the tool."""
        for payload in (SCALAR, STRING):
            _run(prom, payload, "q")  # must not raise

    def test_a_malformed_scalar_is_reported_not_rendered(self, prom):
        broken = {"status": "success", "data": {"resultType": "scalar", "result": []}}
        assert "unreadable" in _run(prom, broken, "time()")


# ── 3. The guard must never be what breaks the answer ─────────────────────────

class TestTheGuardSurvivesAnUnexpectedShape:
    @pytest.mark.parametrize("item", [1755000000, "7", None, ["a", "b"]])
    def test_series_labels_returns_empty_for_a_non_mapping(self, item):
        assert series_labels(item, "metric") == {}

    def test_a_real_series_is_still_read(self):
        assert series_labels({"metric": {"namespace": "shop"}}, "metric") == {"namespace": "shop"}


# ── 4. The series-only entry point reports a shape it cannot project ──────────

class TestSeriesConsumersAreToldTheShapeIsWrong:
    def test_a_scalar_is_an_error_not_an_empty_list(self, prom):
        """`[]` would read as *no data*; these callers feed verdicts (the pass-79 lens)."""
        results, error = pt.query_prometheus_series("time()", 0)
        prom["payload"] = SCALAR
        results, error = pt.query_prometheus_series("time()", 0)
        assert results == []
        assert error is not None and "scalar" in error

    def test_a_vector_is_returned_normally(self, prom):
        prom["payload"] = VECTOR
        results, error = pt.query_prometheus_series("up", 0)
        assert error is None
        assert results[0]["metric"]["pod"] == "payment-api-0"

    def test_a_matrix_is_returned_normally(self, prom):
        prom["payload"] = MATRIX
        results, error = pt.query_prometheus_series("rate(x[5m])", 60)
        assert error is None and len(results) == 2


# ── 5. Controls — the shapes that always worked still work ────────────────────

class TestTheOrdinaryPathsAreUnchanged:
    def test_an_instant_vector(self, prom):
        out = _run(prom, VECTOR, "up")
        assert "PromQL (instant): up" in out
        assert "[pod=payment-api-0] = 0.42" in out

    def test_an_instant_query_asks_for_no_step(self, prom):
        _run(prom, VECTOR, "up")
        assert "step" not in prom["params"]

    def test_a_range_query_asks_for_a_step(self, prom):
        _run(prom, MATRIX, "rate(x[5m])", 60)
        assert "step" in prom["params"]

    def test_a_blocked_namespace_in_the_query_is_still_refused(self, prom):
        out = _run(prom, VECTOR, 'up{namespace="kube-system"}')
        assert "[Protected]" in out

    def test_a_genuinely_empty_result_still_says_no_data(self, prom):
        empty = {"status": "success", "data": {"resultType": "vector", "result": []}}
        assert "No data for query" in _run(prom, empty, "up")


# ── 6. Every early return of the seam agrees on its shape ─────────────────────
# `_query_typed` returns a 3-tuple; each of its six exits must. Two of them were still
# returning the old 2-tuple after the refactor, and only one had a test — mypy caught the
# other. Untested, `Prometheus HTTP 500` would have raised
# `ValueError: not enough values to unpack (expected 3, got 2)` out of the tool.

class TestEveryErrorExitReturnsAnError:
    def test_an_http_error_is_reported(self, prom, monkeypatch):
        class _Resp:
            status_code = 503
            text = "service unavailable"

            def json(self):
                raise AssertionError("must not be parsed")

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, _url, params=None):
                return _Resp()

        monkeypatch.setattr(pt.httpx, "Client", _Client)
        out = pt.query_prometheus.func("up", 0)
        assert "Prometheus HTTP 503" in out

    def test_a_prometheus_level_error_is_reported(self, prom):
        bad = {"status": "error", "error": "parse error at char 3"}
        assert "parse error at char 3" in _run(prom, bad, "up{")

    def test_an_unconfigured_url_is_reported(self, prom, monkeypatch):
        monkeypatch.setattr(settings, "PROMETHEUS_URL", "")
        assert "not configured" in pt.query_prometheus.func("up", 0)

    def test_a_connection_failure_is_reported(self, prom, monkeypatch):
        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, _url, params=None):
                raise pt.httpx.ConnectError("refused")

        monkeypatch.setattr(pt.httpx, "Client", _Client)
        assert "Cannot reach Prometheus" in pt.query_prometheus.func("up", 0)
