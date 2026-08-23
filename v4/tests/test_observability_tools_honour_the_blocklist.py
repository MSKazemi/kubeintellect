"""`kubectl logs -n kube-system` is refused; `{namespace="kube-system"}` returned the same lines.

Pass 59 established that the blocklist is a guarantee about the *product*, and that the way to
find gaps is to enumerate the paths to the cluster rather than the checks on any one path. Four
tools are registered in `app/tools/registry.py`. Two reach the cluster through a command line
and enforce the blocklist by parsing `-n`. Two reach the same cluster's data through a *query
language* and, measured 2026-08-20, enforced nothing:

    query_loki       {namespace="kube-system"}                     RAN
    query_loki       {namespace="kubeintellect"} |= "key"          RAN
    query_loki       {namespace="monitoring"} |~ "token|password"  RAN
    query_prometheus kube_secret_info{namespace="kubeintellect"}   RAN

Loki is the sharper end: `kubectl logs` is namespace-gated precisely because logs are where
credentials appear in plaintext, and `query_loki`'s own docstring advertises it as the better
way to read logs.

Two gates, because a query language cannot be guarded by reading the query alone — the input
gate gives a clear refusal for a query that names a blocked namespace, and the output filter
is the load-bearing one, working on the labels the datasource itself reports.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.core.config import settings
from app.tools.loki_tool import query_loki
from app.tools.prometheus_tool import query_prometheus, query_prometheus_range_raw

SECRET_LINE = 'level=info msg="authenticated" token=eyJhbGciOiJSUzI1NiJ9.SECRET'


def _fake_client(namespaces, kind="logs"):
    """A datasource that answers honestly: it labels results with the namespaces given."""
    sent = []

    def get(url, params=None, **_kw):
        sent.append(params.get("query"))
        resp = MagicMock()
        resp.status_code = 200
        if kind == "logs":
            resp.json.return_value = {"data": {"result": [
                {"stream": {"namespace": ns, "pod": f"{ns}-pod"},
                 "values": [["1755600000000000000", SECRET_LINE]]}
                for ns in namespaces]}}
        else:
            resp.json.return_value = {"status": "success", "data": {
                "resultType": "vector", "result": [
                    {"metric": {"namespace": ns, "pod": f"{ns}-pod"},
                     "value": [1755600000, "1"], "values": [[1755600000, "1"]]}
                    for ns in namespaces]}}
        return resp

    return get, sent


def _loki(logql, namespaces=("prod",)):
    get, sent = _fake_client(namespaces, "logs")
    with patch("httpx.Client") as c:
        c.return_value.__enter__.return_value.get = get
        return str(query_loki.invoke({"logql": logql})), sent


def _prom(promql, namespaces=("prod",), range_minutes=0):
    get, sent = _fake_client(namespaces, "metrics")
    with patch("httpx.Client") as c:
        c.return_value.__enter__.return_value.get = get
        return str(query_prometheus.invoke(
            {"promql": promql, "range_minutes": range_minutes})), sent


BLOCKED_QUERIES_LOKI = [
    '{namespace="kube-system"}',
    '{namespace="kubeintellect"} |= "key"',
    '{namespace="monitoring"} |~ "token|password"',
    '{namespace = "cert-manager"}',
    '{namespace="kube-public", app="x"}',
    'rate({namespace="ingress-nginx"} |= "error" [5m])',
    '{namespace=~"kube-.*"}',
]
BLOCKED_QUERIES_PROM = [
    'kube_secret_info{namespace="kubeintellect"}',
    'kube_pod_info{namespace="kube-system"}',
    'up{namespace="monitoring"}',
    'sum(rate(container_cpu_usage_seconds_total{namespace="cert-manager"}[5m]))',
    'kube_pod_info{namespace=~"kube-.*"}',
]


class TestTheQueryNeverReachesTheDatasource:
    @pytest.mark.parametrize("logql", BLOCKED_QUERIES_LOKI)
    def test_loki_refuses_before_sending(self, logql):
        out, sent = _loki(logql, namespaces=("kube-system",))
        assert "[Protected]" in out, out[:160]
        assert sent == [], "the query was sent to Loki anyway"
        assert "SECRET" not in out

    @pytest.mark.parametrize("promql", BLOCKED_QUERIES_PROM)
    def test_prometheus_refuses_before_sending(self, promql):
        out, sent = _prom(promql, namespaces=("kube-system",))
        assert "[Protected]" in out, out[:160]
        assert sent == []


class TestTheOutputFilterIsTheLoadBearingGate:
    """The input gate cannot read every query. The datasource's own labels can."""

    def test_a_label_query_matching_a_blocked_namespace_is_filtered(self):
        out, sent = _loki('{app="nginx"}', namespaces=("kube-system", "prod"))
        assert sent, "this one must reach Loki — only the answer can be judged"
        assert "kube-system" not in out
        assert "prod-pod" in out

    def test_the_secret_bearing_line_does_not_survive(self):
        out, _sent = _loki('{app="nginx"}', namespaces=("kube-system",))
        assert "SECRET" not in out
        assert "[Protected]" in out and "withheld" in out

    def test_prometheus_series_are_filtered_the_same_way(self):
        out, sent = _prom("kube_pod_info", namespaces=("monitoring", "prod"))
        assert sent
        assert "monitoring" not in out and "prod-pod" in out

    def test_range_queries_are_filtered_too(self):
        out, _sent = _prom("kube_pod_info", namespaces=("monitoring", "prod"),
                           range_minutes=60)
        assert "monitoring" not in out and "prod-pod" in out

    def test_the_filtering_is_declared_not_silent(self):
        out, _sent = _loki('{app="nginx"}', namespaces=("kube-system", "prod"))
        assert "withheld" in out, "results vanished with no explanation"

    def test_a_metric_logql_query_is_filtered(self):
        out, _sent = _loki('sum(rate({app="nginx"}[5m]))',
                           namespaces=("kube-system", "prod"))
        assert "kube-system" not in out


class TestNoOverBlocking:
    @pytest.mark.parametrize("logql", [
        '{namespace="prod"}',
        '{namespace="staging"} |= "ERROR"',
        '{app="nginx"}',
        '{namespace!="kube-system"}',
        '{namespace=~"prod|staging"}',
        'rate({namespace="prod"} |= "error" [5m])',
    ])
    def test_ordinary_loki_queries_run(self, logql):
        _out, sent = _loki(logql, namespaces=("prod",))
        assert sent == [logql], f"over-blocked {logql!r}"

    @pytest.mark.parametrize("promql", [
        'kube_pod_info{namespace="prod"}',
        'up',
        'sum(rate(container_cpu_usage_seconds_total[5m])) by (pod)',
        'kube_pod_info{namespace!="kube-system"}',
    ])
    def test_ordinary_prometheus_queries_run(self, promql):
        _out, sent = _prom(promql, namespaces=("prod",))
        assert sent == [promql], f"over-blocked {promql!r}"

    def test_a_negative_matcher_is_a_request_to_exclude_not_to_read(self):
        _out, sent = _loki('{namespace!~"kube-.*"}', namespaces=("prod",))
        assert sent, "excluding a blocked namespace was mistaken for selecting it"

    def test_an_unparseable_regex_matcher_does_not_crash(self):
        _out, sent = _loki('{namespace=~"["}', namespaces=("prod",))
        assert sent, "an invalid regex should fall through to the output filter"

    def test_results_without_a_namespace_label_survive(self):
        """Node- and cluster-level metrics legitimately carry no namespace."""
        get, sent = _fake_client([], "metrics")

        def node_metrics(url, params=None, **_kw):
            sent.append(params.get("query"))
            r = MagicMock(); r.status_code = 200
            r.json.return_value = {"status": "success", "data": {
                "resultType": "vector",
                "result": [{"metric": {"node": "worker-1"}, "value": [1, "0.42"]}]}}
            return r

        with patch("httpx.Client") as c:
            c.return_value.__enter__.return_value.get = node_metrics
            out = str(query_prometheus.invoke({"promql": "node_load1"}))
        assert "worker-1" in out


class TestTheDetectorEngineIsDeliberatelyExempt:
    """`query_prometheus_range_raw` is fed by human-reviewed playbooks, not by chat.

    Detectors are *supposed* to watch kube-system for control-plane and node problems.
    Gating them on the user-facing blocklist would break ADR-010 trend detection, so the
    guard sits on the tool the LLM calls, not on the shared query path.
    """

    def test_it_still_reads_protected_namespaces(self):
        get, sent = _fake_client(("kube-system",), "metrics")
        with patch("httpx.Client") as c:
            c.return_value.__enter__.return_value.get = get
            series = query_prometheus_range_raw(
                'kube_pod_info{namespace="kube-system"}', 60)
        assert sent, "the detector engine was blocked from its own data source"
        assert series and series[0]["metric"]["namespace"] == "kube-system"


class TestOneDefinition:
    def test_both_tools_follow_the_configured_blocklist(self):
        """Re-point the setting; a copied literal would pass equality today and rot later."""
        with patch.object(settings, "KUBECTL_BLOCKED_NAMESPACES", "vault-system"):
            out, sent = _loki('{namespace="vault-system"}')
            assert "[Protected]" in out and sent == []
            _out, sent = _loki('{namespace="kube-system"}')
            assert sent, "loki kept a hardcoded blocklist of its own"
            out, sent = _prom('up{namespace="vault-system"}')
            assert "[Protected]" in out and sent == []
