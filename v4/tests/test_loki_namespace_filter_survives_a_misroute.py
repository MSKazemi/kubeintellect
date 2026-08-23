"""The namespace filter must not depend on a guess about the query.

Pass 86 of the standing audit (T38). `query_loki` chose between a log render and a metric
render by testing whether the LogQL text *starts with* one of eight function names. That guess
also decided which key the namespace filter read labels from — `"stream"` for logs, `"metric"`
for metrics. A Loki *matrix* payload has no `stream` key, so a misrouted metric query filtered
against `{}`, `{}.get("namespace", "")` is `""`, `""` is in no blocklist, and **every series
passed, `kube-system` included**.

Measured 2026-08-20 — seven of ten ordinary LogQL metric expressions failed the test, including
`sum by (namespace) (rate({app="web"}[5m]))`, which is the most idiomatic form there is (a
space after `sum`, not a paren). The tool returned the `kube-system` series, printed no labels
at all (it was reading `stream`), and said nothing about filtering.

Three independent defences now, so no single wrong answer can switch the guard off:

1. `_is_metric_query` recognises the real LogQL metric function set.
2. Rendering and filtering follow Loki's own `resultType`, not the guess.
3. `drop_blocked_series` looks in every known label container, so a wrong hint cannot disable it.
"""
from __future__ import annotations

import pytest

from app.tools import loki_tool as lt
from app.tools.namespace_guard import drop_blocked_series, series_labels

MATRIX = {"status": "success", "data": {"resultType": "matrix", "result": [
    {"metric": {"namespace": "shop", "app": "web"}, "values": [[1, "1.0"]]},
    {"metric": {"namespace": "kube-system", "app": "coredns"}, "values": [[1, "9.9"]]},
]}}
STREAMS = {"status": "success", "data": {"resultType": "streams", "result": [
    {"stream": {"namespace": "shop"}, "values": [["1", "hello from shop"]]},
    {"stream": {"namespace": "kube-system"}, "values": [["1", "TOKEN=abcdef"]]},
]}}


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload):
        self._payload = payload
        self.params = None

    def get(self, _url, params=None):
        self.params = params
        return _Resp(self._payload)


# ── 1. Classification ─────────────────────────────────────────────────────────

class TestMetricExpressionsAreRecognised:
    @pytest.mark.parametrize("logql", [
        'rate({app="web"}[5m])',
        'sum by (namespace) (rate({app="web"}[5m]))',
        'sum without (pod) (rate({app="web"}[5m]))',
        'sum_over_time({app="web"} | unwrap d [5m])',
        'avg_over_time({app="web"} | unwrap d [5m])',
        'quantile_over_time(0.99, {app="web"} | unwrap d [5m])',
        'absent_over_time({app="web"}[5m])',
        'topk(5, rate({app="web"}[5m]))',
        'bottomk(5, rate({app="web"}[5m]))',
        '(sum(rate({app="web"}[5m])))',
        'count_over_time({app="web"}[5m])',
        'bytes_over_time({app="web"}[5m])',
    ])
    def test_metric(self, logql):
        assert lt._is_metric_query(logql), logql

    @pytest.mark.parametrize("logql", [
        '{app="web"} |= "ERROR"',
        '{namespace="shop"} | json | status >= 500',
        '  {app="web"}',
        '{app="web"} |~ "timeout|connection refused"',
    ])
    def test_log_query(self, logql):
        assert not lt._is_metric_query(logql), logql

    def test_a_label_named_like_a_function_is_not_a_metric_query(self):
        assert not lt._is_metric_query('{sum="x"}')


# ── 2. The response decides, not the guess ────────────────────────────────────

class TestTheResultTypeDecides:
    def test_a_matrix_reaching_the_log_path_is_still_filtered(self):
        """The load-bearing property: even a misroute cannot leak."""
        out = lt._log_query(_Client(MATRIX), "http://x", "anything", 100, 0, 1)
        assert "9.9" not in out, "kube-system series survived a misrouted query"
        assert "withheld" in out

    def test_a_matrix_reaching_the_log_path_renders_as_metrics(self):
        out = lt._log_query(_Client(MATRIX), "http://x", "anything", 100, 0, 1)
        assert "LogQL (metric)" in out
        assert "namespace=shop" in out

    def test_streams_reaching_the_metric_path_are_still_filtered(self):
        out = lt._range_query(_Client(STREAMS), "http://x", "anything", 0, 1)
        assert "TOKEN=abcdef" not in out
        assert "hello from shop" in out

    def test_the_correct_route_still_works_for_each_shape(self):
        assert "hello from shop" in lt._log_query(
            _Client(STREAMS), "http://x", '{app="web"}', 100, 0, 1)
        assert "namespace=shop" in lt._range_query(
            _Client(MATRIX), "http://x", 'rate({app="web"}[5m])', 0, 1)


# ── 3. The filter itself does not trust the hint ──────────────────────────────

class TestTheFilterIgnoresAWrongHint:
    def test_a_wrong_hint_still_drops_the_blocked_series(self):
        kept, dropped = drop_blocked_series(MATRIX["data"]["result"], "stream")
        assert dropped == 1
        assert [k["metric"]["namespace"] for k in kept] == ["shop"]

    def test_a_wrong_hint_still_drops_blocked_streams(self):
        kept, dropped = drop_blocked_series(STREAMS["data"]["result"], "metric")
        assert dropped == 1

    def test_no_hint_at_all_still_filters(self):
        _kept, dropped = drop_blocked_series(MATRIX["data"]["result"])
        assert dropped == 1

    def test_the_hint_wins_when_both_containers_are_present(self):
        item = {"stream": {"namespace": "shop"}, "metric": {"namespace": "kube-system"}}
        assert series_labels(item, "metric")["namespace"] == "kube-system"
        assert series_labels(item, "stream")["namespace"] == "shop"

    def test_a_result_with_no_labels_is_not_dropped(self):
        """Documented residual: node/cluster metrics legitimately carry no namespace."""
        _kept, dropped = drop_blocked_series([{"metric": {"instance": "node-1"}}], "metric")
        assert dropped == 0

    def test_an_empty_label_container_falls_through_to_the_next(self):
        item = {"stream": {}, "metric": {"namespace": "kube-system"}}
        assert series_labels(item, "stream")["namespace"] == "kube-system"


# ── The request parameters still follow the query shape ───────────────────────

class TestRequestShaping:
    def test_a_log_query_asks_for_a_limit_and_direction(self):
        client = _Client(STREAMS)
        lt._log_query(client, "http://x", '{app="web"}', 50, 0, 1)
        assert client.params["limit"] == 50
        assert client.params["direction"] == "backward"

    def test_a_metric_query_asks_for_a_step(self):
        client = _Client(MATRIX)
        lt._range_query(client, "http://x", 'rate({app="web"}[5m])', 0, int(1e9) * 600)
        assert "step" in client.params
