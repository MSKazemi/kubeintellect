"""A format whose columns the *caller* chooses cannot be filtered — so it must be refused.

Pass 88 of the standing audit (T38), reached by re-asking pass 87's question — *does this
component interpret a payload by its own assumption rather than by what the payload declares?* —
of the kubectl namespace filters.

Both filter families ended in a branch that assumes a kubectl table with NAMESPACE (or NAME) as
the first column. They refused `-o name` and `-o jsonpath` by name and let **everything else**
reach that branch. `-o custom-columns`, `-o go-template` and their `-file`/`template` variants
render whatever the caller asked for, in whatever order, so that assumption is simply false and
the rows went out whole and unannotated.

Measured 2026-08-20 through the real `run_kubectl` with a stubbed subprocess:

    kubectl get pods -A -o custom-columns=NAME:.metadata.name,IMAGE:.spec.containers[0].image
        -> the coredns and prometheus rows returned, nothing said to be withheld
    kubectl get pods -A -o go-template={{range .items}}{{.metadata.name}}={{.metadata.namespace}}{{end}}
        -> every row returned, `kube-system` and `monitoring` included
    kubectl get namespaces -o custom-columns=STATUS:.status.phase,NAME:.metadata.name
        -> `kube-system` and `monitoring` returned

The `get namespaces -o custom-columns=NAME:...` case *was* filtered — but only because that
caller happened to put NAME first. Moving it to the second column defeated it, which is what
makes this an assumption rather than a check.

`run_helm`'s docstring already records the general form of this lesson, about the sibling verb
check: *an allowlist turns a parser bug into a usability complaint; a deny-list turns the same
bug into a bypass.* The verbs were inverted to an allowlist in 2026-08-13. The output format was
left a deny-list of the two formats someone thought of.

Sharpest detail: the tool's own parse-error message told the model to *"use -o custom-columns"* —
the guard's own guidance pointed at the one format that bypassed it.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.tools import kubectl_tool as kt

PODS_TABLE = (
    "NAMESPACE     NAME              READY   STATUS    AGE\n"
    "shop          payment-api-0     1/1     Running   4d\n"
    "kube-system   coredns-abc       1/1     Running   9d\n"
    "monitoring    prometheus-0      1/1     Running   9d\n"
)
CUSTOM_COLUMNS = (
    "NAME              IMAGE\n"
    "payment-api-0     shop/payment:1.2\n"
    "coredns-abc       registry.k8s.io/coredns:1.11\n"
    "prometheus-0      quay.io/prometheus:2.5\n"
)
GO_TEMPLATE = "payment-api-0=shop\ncoredns-abc=kube-system\nprometheus-0=monitoring\n"
NS_NAME_SECOND = "STATUS   NAME\nActive   shop\nActive   kube-system\nActive   monitoring\n"
NS_TABLE = "NAME            STATUS   AGE\nshop            Active   4d\nkube-system     Active   9d\n"
PODS_JSON = json.dumps({"apiVersion": "v1", "kind": "List", "items": [
    {"metadata": {"name": "payment-api-0", "namespace": "shop"}},
    {"metadata": {"name": "coredns-abc", "namespace": "kube-system"}},
]})

PROTECTED = ("coredns", "prometheus-0", "kube-system", "monitoring", "kubeintellect")


@pytest.fixture
def kubectl(monkeypatch):
    """Run the real tool against a canned kubectl output."""
    box: dict = {}

    class _Proc:
        returncode = 0

        def __init__(self):
            self.stdout = box["out"]
            self.stderr = ""

    monkeypatch.setattr(kt.subprocess, "run", lambda *a, **k: _Proc())

    def _run(command, out):
        box["out"] = out
        return kt.run_kubectl.func(command)

    return _run


def _leaks(text: str) -> list[str]:
    return [n for n in PROTECTED if n in text]


# ── 1. Caller-shaped formats are refused, not guessed at ──────────────────────

class TestACallerShapedFormatIsRefused:
    @pytest.mark.parametrize("out_format,output", [
        ("custom-columns=NAME:.metadata.name,IMAGE:.spec.containers[0].image", CUSTOM_COLUMNS),
        ("custom-columns-file=/tmp/cols.txt", CUSTOM_COLUMNS),
        ("go-template={{range .items}}{{.metadata.namespace}}{{end}}", GO_TEMPLATE),
        ("go-template-file=/tmp/t.tmpl", GO_TEMPLATE),
        ("template={{.items}}", GO_TEMPLATE),
        ("templatefile=/tmp/t.tmpl", GO_TEMPLATE),
    ])
    def test_all_namespaces(self, kubectl, out_format, output):
        out = kubectl(f"kubectl get pods -A -o {out_format}", output)
        assert _leaks(out) == [], f"{out_format} leaked {_leaks(out)}"
        assert "[Protected]" in out

    @pytest.mark.parametrize("out_format", [
        "custom-columns=STATUS:.status.phase,NAME:.metadata.name",
        "go-template={{range .items}}{{.metadata.name}}{{end}}",
    ])
    def test_namespace_listing(self, kubectl, out_format):
        out = kubectl(f"kubectl get namespaces -o {out_format}", NS_NAME_SECOND)
        assert _leaks(out) == []
        assert "[Protected]" in out

    def test_the_refusal_names_the_format_and_offers_a_way_through(self, kubectl):
        out = kubectl("kubectl get pods -A -o custom-columns=NAME:.metadata.name", CUSTOM_COLUMNS)
        assert "custom-columns" in out
        assert "-o json" in out and "-n <namespace>" in out

    def test_a_column_order_cannot_change_the_verdict(self, kubectl):
        """The old check passed whenever the caller happened to put the name first."""
        first = kubectl("kubectl get namespaces -o custom-columns=NAME:.metadata.name",
                        NS_TABLE)
        second = kubectl("kubectl get namespaces -o custom-columns=STATUS:.status.phase,"
                         "NAME:.metadata.name", NS_NAME_SECOND)
        assert "[Protected]" in first and "[Protected]" in second


# ── 2. The fixed-shape formats keep working ───────────────────────────────────

class TestKubectlChosenShapesStillFilter:
    def test_the_default_table(self, kubectl):
        out = kubectl("kubectl get pods -A", PODS_TABLE)
        assert _leaks(out) == []
        assert "payment-api-0" in out and "withheld" in out

    def test_wide(self, kubectl):
        out = kubectl("kubectl get pods -A -o wide", PODS_TABLE)
        assert _leaks(out) == []
        assert "payment-api-0" in out

    def test_json_stays_parseable_and_is_filtered(self, kubectl):
        out = kubectl("kubectl get pods -A -o json", PODS_JSON)
        doc = json.loads(out)
        assert [i["metadata"]["namespace"] for i in doc["items"]] == ["shop"]
        assert doc["withheldByPolicy"]

    def test_a_namespace_table(self, kubectl):
        out = kubectl("kubectl get namespaces", NS_TABLE)
        assert "shop" in out and "kube-system" not in out

    def test_name_and_jsonpath_keep_their_own_handling(self, kubectl):
        refused = kubectl("kubectl get pods -A -o name", "pod/coredns-abc\n")
        assert "[Protected]" in refused and "carries no namespace" in refused
        filtered = kubectl("kubectl get namespaces -o name",
                           "namespace/shop\nnamespace/kube-system\n")
        assert "shop" in filtered and "kube-system" not in filtered


# ── 3. A structured payload that is not a list of items fails closed ──────────

class TestAnUnexpectedStructuredShapeFailsClosed:
    def test_a_bare_object_is_not_passed_through(self, kubectl):
        """Hardening — `get -A` always renders a List, so this has no reproduction."""
        bare = json.dumps({"kind": "Pod",
                           "metadata": {"name": "coredns-abc", "namespace": "kube-system"}})
        out = kubectl("kubectl get pods -A -o json", bare)
        assert _leaks(out) == []
        assert "[Protected]" in out

    def test_unparseable_output_still_fails_closed(self, kubectl):
        out = kubectl("kubectl get pods -A -o json", "{not json at all")
        assert "[Protected]" in out


# ── 4. The tool's own advice must not point at a refused format ───────────────

class TestTheAdviceDoesNotNameABypass:
    def test_the_truncated_jsonpath_message_recommends_json(self, kubectl):
        with pytest.raises(ValueError) as exc:
            kubectl("kubectl get pods -o jsonpath='{.items[*].metadata.name}", "")
        assert "-o json" in str(exc.value)
        assert "use -o custom-columns" not in str(exc.value)


# ── 5. The allowlist itself ───────────────────────────────────────────────────

class TestTheAllowlist:
    def test_it_holds_only_shapes_kubectl_decides(self):
        assert kt._FIXED_SHAPE_FORMATS == frozenset({"", "wide", "json", "yaml"})

    @pytest.mark.parametrize("fmt", ["custom-columns=X:.y", "go-template={{.x}}", "template=x",
                                     "custom-columns-file=f", "go-template-file=f", "templatefile=f"])
    def test_caller_shaped_formats_are_outside_it(self, fmt):
        assert fmt not in kt._FIXED_SHAPE_FORMATS

    def test_the_blocklist_setting_is_what_drives_the_filter(self, kubectl):
        with patch.object(settings, "KUBECTL_BLOCKED_NAMESPACES", "shop"):
            out = kubectl("kubectl get pods -A", PODS_TABLE)
        assert "payment-api-0" not in out and "coredns-abc" in out
