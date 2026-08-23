"""A filtered listing and a complete listing must not be the same bytes.

Pass 84 of the standing audit (T38). The namespace blocklist has two enforcement shapes: a
**refusal** (`kubectl get ns monitoring` -> `[Protected] …`) and a **filter** (rows removed from
a listing). A refusal is impossible to miss. A filter was, for five of the six paths, completely
silent — measured 2026-08-20:

| command                      | rows removed | caller told |
|------------------------------|--------------|-------------|
| `kubectl get namespaces`     | 3            | **no**      |
| `kubectl get ns -o name`     | 2            | **no**      |
| `kubectl get ns -o json`     | 2            | **no**      |
| `kubectl describe namespaces`| 2            | **no**      |
| `helm list -A`               | 2            | **no**      |
| `kubectl get pods -A`        | 1            | yes         |

So an agent asked *does the monitoring namespace exist?* runs `kubectl get namespaces`, receives
a list it has no way to know is short, and answers **no** — a definite false statement about the
cluster, produced by a security control doing its job. `kubectl get ns monitoring` would have
said `[Protected]`; the listing was the one path that lied by omission.

The sixth path told the truth and broke the format doing it: `_filter_all_namespaces_output`
appended its notice *after* `json.dumps`, so `kubectl get pods -A -o json` returned a document
`json.loads` rejects with `Extra data`. An existing test hid this by parsing only
`out.split("\\n[Protected]")[0]`.
"""
from __future__ import annotations

import json

import pytest
import yaml

from app.tools import helm_tool as ht
from app.tools import kubectl_tool as kt
from app.tools.namespace_guard import WITHHELD_KEY, withheld_note

NS_TABLE = """NAME              STATUS   AGE
default           Active   30d
kube-system       Active   30d
monitoring        Active   12d
shop              Active   5d
"""
NS_NAME = "namespace/default\nnamespace/kube-system\nnamespace/monitoring\n"
NS_JSON = json.dumps({"apiVersion": "v1", "kind": "NamespaceList", "items": [
    {"metadata": {"name": n}} for n in ("default", "kube-system", "monitoring")]}, indent=2)
NS_DESCRIBE = """Name:         default
Status:       Active

Name:         monitoring
Status:       Active
"""
HELM_TABLE = """NAME            NAMESPACE       REVISION  STATUS
shop            shop            3         deployed
prometheus      monitoring      1         deployed
"""


def _ns(cmd: str, output: str) -> str:
    args = cmd.split()
    return kt._filter_namespace_output(args[1], args, output)


# ── The listing must say when it is short ─────────────────────────────────────

class TestEveryTextFormatSaysWhatItWithheld:
    def test_the_default_table(self):
        out = _ns("kubectl get namespaces", NS_TABLE)
        assert "monitoring" not in out
        assert "withheld" in out and "NOT the complete set" in out

    def test_o_name(self):
        out = _ns("kubectl get ns -o name", NS_NAME)
        assert "monitoring" not in out
        assert "withheld" in out

    def test_describe(self):
        out = _ns("kubectl describe namespaces", NS_DESCRIBE)
        assert "monitoring" not in out
        assert "withheld" in out

    def test_the_note_names_namespaces_not_results(self):
        """"3 result(s)" reads as three pods when the caller asked for namespaces."""
        out = _ns("kubectl get namespaces", NS_TABLE)
        assert "namespace(s) withheld" in out

    def test_an_unfiltered_listing_carries_no_note(self):
        clean = "NAME     STATUS   AGE\nshop     Active   5d\n"
        assert "withheld" not in _ns("kubectl get namespaces", clean)

    def test_helm_list_says_what_it_withheld(self):
        out = ht._filter_release_namespaces(HELM_TABLE)
        assert "prometheus" not in out
        assert "release(s) withheld" in out


# ── Structured output carries the notice INSIDE the document ──────────────────

class TestStructuredOutputStaysParseable:
    def test_namespace_json_is_annotated_and_still_parses(self):
        doc = json.loads(_ns("kubectl get ns -o json", NS_JSON))
        assert [i["metadata"]["name"] for i in doc["items"]] == ["default"]
        assert "withheld" in doc[WITHHELD_KEY]

    def test_namespace_yaml_is_annotated_and_still_parses(self):
        payload = yaml.safe_dump(json.loads(NS_JSON))
        doc = yaml.safe_load(_ns("kubectl get ns -o yaml", payload))
        assert [i["metadata"]["name"] for i in doc["items"]] == ["default"]
        assert WITHHELD_KEY in doc

    def test_the_all_namespaces_json_filter_no_longer_emits_invalid_json(self):
        """Regression: this returned `{...}\\n[Protected] …`, which is not JSON."""
        pods = json.dumps({"kind": "PodList", "items": [
            {"metadata": {"name": "web-1", "namespace": "shop"}},
            {"metadata": {"name": "coredns", "namespace": "kube-system"}},
        ]}, indent=2)
        out = kt._filter_all_namespaces_output(
            "get", ["kubectl", "get", "pods", "-A", "-o", "json"], pods)
        doc = json.loads(out)  # used to raise JSONDecodeError: Extra data
        assert [i["metadata"]["name"] for i in doc["items"]] == ["web-1"]
        assert "withheld" in doc[WITHHELD_KEY]

    def test_the_all_namespaces_yaml_filter_stays_parseable(self):
        pods = yaml.safe_dump({"kind": "PodList", "items": [
            {"metadata": {"name": "web-1", "namespace": "shop"}},
            {"metadata": {"name": "coredns", "namespace": "kube-system"}},
        ]})
        out = kt._filter_all_namespaces_output(
            "get", ["kubectl", "get", "pods", "--all-namespaces", "-o", "yaml"], pods)
        doc = yaml.safe_load(out)
        assert [i["metadata"]["name"] for i in doc["items"]] == ["web-1"]

    def test_the_annotation_key_names_no_blocked_namespace(self):
        """The output leak-check greps for blocked namespace names; the key must not trip it."""
        from app.core.config import settings
        assert not any(ns in WITHHELD_KEY.lower() for ns in settings.kubectl_blocked_namespaces)


# ── One wording, one implementation ───────────────────────────────────────────

class TestOneWordingForEveryFilter:
    def test_kubectl_reuses_the_shared_helper(self):
        """`kubectl_tool` carried a byte-identical private copy of this sentence."""
        assert kt._withheld_note is withheld_note

    def test_the_note_says_the_listing_is_incomplete(self):
        note = withheld_note(2, "namespace")
        assert "NOT the complete set" in note
        assert "2 namespace(s)" in note


# ── The stated limit ──────────────────────────────────────────────────────────

class TestTheLimitThatCannotBeClosedInBand:
    @pytest.mark.parametrize("fmt,dump", [
        ("json", lambda d: json.dumps(d, indent=2)),
        ("yaml", lambda d: yaml.safe_dump(d, default_flow_style=False)),
    ])
    def test_a_bare_sequence_cannot_carry_the_note(self, fmt, dump, caplog):
        """`helm list -o json` is a top-level array — no field to hold the notice, and nothing
        may follow it without making the payload unparseable. The withholding is logged
        instead, and the limit is asserted here so it cannot be mistaken for coverage.
        """
        import logging
        payload = dump([{"name": "shop", "namespace": "shop"},
                        {"name": "prom", "namespace": "monitoring"}])
        with caplog.at_level(logging.WARNING):
            out = ht._filter_release_namespaces(payload)
        parsed = json.loads(out) if fmt == "json" else yaml.safe_load(out)
        assert [r["name"] for r in parsed] == ["shop"]
        assert "withheld" not in out, "documented limit — the array carries no notice"
        assert "cannot carry the notice" in caplog.text
