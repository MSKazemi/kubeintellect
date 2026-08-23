"""A bare `kubectl get namespaces` is allowed *because* the blocked ones are stripped from it.

`_targeted_namespaces` deliberately returns [] for a listing with no target, so the command
runs and `_filter_namespace_output` removes the protected entries. That promise held for three
of the six output formats. Measured 2026-08-20 with a `readonly` key and a stubbed kubectl:

    kubectl get ns                      filtered
    kubectl get ns -o name              filtered
    kubectl get ns -oname               LEAKED    ← same flag, attached-shorthand spelling
    kubectl get ns -o json              LEAKED
    kubectl get ns -o yaml              LEAKED
    kubectl describe ns                 LEAKED    ← filter hardcoded the verb "get"

Three separate causes, one function:

1. the `-o` reader handled `-o x`, `-o=x`, `--output x` and `--output=x` but not `-ox`, which is
   the same gap pass 54 fixed on `-n`. There is now **one** `_flag_value` parser for both, so
   they cannot drift again;
2. `json`/`yaml` did `return output` with the comment *"too complex to strip reliably; blocked at
   execution anyway"* — the second half was false, nothing blocks a bare listing at execution,
   and `-o json` is the format a model asks for most readily;
3. `describe` reached `_extract_resource_type("get", args)`, whose `args.index("get")` raises for
   any other verb, so the resource came back None and the filter returned early — handing back
   the labels, annotations and quotas of every namespace the listing exists to hide.

What leaks here is namespace names and metadata, not credentials. It is still the difference
between a control that works and one that works in half its spellings.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import yaml

from app.tools.kubectl_tool import _flag_value, run_kubectl

TABLE = (
    "NAME              STATUS   AGE\n"
    "prod              Active   1d\n"
    "kube-system       Active   9d\n"
    "monitoring        Active   9d\n"
)
NAMES = "namespace/prod\nnamespace/kube-system\nnamespace/monitoring\n"
JSON_LIST = json.dumps({"kind": "NamespaceList", "items": [
    {"metadata": {"name": "prod", "labels": {"tier": "app"}}},
    {"metadata": {"name": "kube-system", "labels": {"tier": "infra"}}},
    {"metadata": {"name": "staging"}},   # `monitoring` is itself blocked by default — a fixture
]})                                      # that used it as "an ordinary namespace" was wrong
YAML_LIST = yaml.safe_dump({"kind": "NamespaceList", "items": [
    {"metadata": {"name": "prod"}},
    {"metadata": {"name": "kube-system"}},
]})
DESCRIBE = (
    "Name:         prod\n"
    "Labels:       tier=app\n"
    "Status:       Active\n"
    "\n"
    "Name:         kube-system\n"
    "Labels:       tier=infra\n"
    "Annotations:  secret-ish=value\n"
    "Status:       Active\n"
    "\n"
    "Name:         staging\n"
    "Status:       Active\n"
)

_ALL_FORMS = [
    ("kubectl get ns", TABLE),
    ("kubectl get namespaces", TABLE),
    ("kubectl get ns -o wide", TABLE),
    ("kubectl get ns -o name", NAMES),
    ("kubectl get ns -o=name", NAMES),
    ("kubectl get ns -oname", NAMES),
    ("kubectl get ns --output name", NAMES),
    ("kubectl get ns --output=name", NAMES),
    ("kubectl get ns -o json", JSON_LIST),
    ("kubectl get ns -ojson", JSON_LIST),
    ("kubectl get ns --output=json", JSON_LIST),
    ("kubectl get ns -o yaml", YAML_LIST),
    ("kubectl get ns -oyaml", YAML_LIST),
    # `-o jsonpath` left this list on 2026-08-20: it is caller-shaped, so it is refused
    # rather than filtered. See tests/test_the_jsonpath_filter_was_one_jsonpath.py.
    ("kubectl describe ns", DESCRIBE),
    ("kubectl describe namespaces", DESCRIBE),
]


def _run(command: str, stdout: str, role: str = "readonly") -> str:
    with patch("subprocess.run") as run:
        proc = MagicMock()
        proc.stdout, proc.stderr, proc.returncode = stdout, "", 0
        run.return_value = proc
        return str(run_kubectl.invoke({"command": command, "stdin": None},
                                      config={"configurable": {"user_role": role,
                                                               "thread_id": "t"}}))


class TestEveryOutputFormatIsFiltered:
    @pytest.mark.parametrize("command,stdout", _ALL_FORMS)
    def test_a_blocked_namespace_never_appears(self, command, stdout):
        out = _run(command, stdout)
        assert "kube-system" not in out, f"{command!r} leaked: {out[:160]!r}"

    @pytest.mark.parametrize("command,stdout", _ALL_FORMS)
    def test_an_ordinary_namespace_survives(self, command, stdout):
        """A filter that empties the answer is not a filter, it is an outage."""
        out = _run(command, stdout)
        assert "prod" in out, f"{command!r} over-filtered: {out[:160]!r}"


class TestTheStructuredFormatsStayStructured:
    def test_json_remains_parseable_and_loses_only_the_blocked_entry(self):
        out = _run("kubectl get ns -o json", JSON_LIST)
        doc = json.loads(out)
        assert [i["metadata"]["name"] for i in doc["items"]] == ["prod", "staging"]
        assert doc["kind"] == "NamespaceList"

    def test_yaml_remains_parseable(self):
        out = _run("kubectl get ns -o yaml", YAML_LIST)
        doc = yaml.safe_load(out)
        assert [i["metadata"]["name"] for i in doc["items"]] == ["prod"]

    @pytest.mark.parametrize("payload", ["<<not json>>", "", "[1, 2, 3]", "just a string"])
    def test_an_unparseable_payload_is_refused_rather_than_passed_through(self, payload):
        """Failing closed matters more here than preserving an answer we cannot inspect."""
        out = _run("kubectl get ns -o json", payload)
        assert "[Protected]" in out, f"passed through unfiltered: {out[:120]!r}"


class TestDescribeBlocksAreSplitCorrectly:
    def test_only_the_blocked_block_is_dropped(self):
        out = _run("kubectl describe namespaces", DESCRIBE)
        assert "tier=app" in out and "staging" in out
        assert "tier=infra" not in out and "secret-ish" not in out

    def test_an_indented_name_line_does_not_split_a_block(self):
        """Nested fields are indented; only a column-zero `Name:` starts a new entry."""
        stdout = (
            "Name:         prod\n"
            "Resource Quotas\n"
            "  Name:       kube-system-quota\n"
            "  Hard:       cpu=1\n"
        )
        out = _run("kubectl describe ns", stdout)
        assert "prod" in out and "kube-system-quota" in out


class TestNonNamespaceListingsAreUntouched:
    @pytest.mark.parametrize("command", [
        "kubectl get pods -n prod -o json",
        "kubectl get deployments -o yaml -n prod",
        "kubectl describe deployment api -n prod",
    ])
    def test_output_passes_through_verbatim(self, command):
        stdout = '{"items":[{"metadata":{"name":"kube-system-thing"}}]}'
        assert _run(command, stdout) == stdout


class TestTheSharedFlagParser:
    @pytest.mark.parametrize("args,expected", [
        (["kubectl", "get", "ns", "-o", "json"], "json"),
        (["kubectl", "get", "ns", "-o=json"], "json"),
        (["kubectl", "get", "ns", "-ojson"], "json"),
        (["kubectl", "get", "ns", "--output", "json"], "json"),
        (["kubectl", "get", "ns", "--output=json"], "json"),
        (["kubectl", "get", "ns"], None),
        (["kubectl", "get", "ns", "-o"], None),
        (["kubectl", "label", "ns", "x", "--overwrite"], None),   # --o…, not -o
    ])
    def test_output_is_read_in_every_spelling(self, args, expected):
        assert _flag_value(args, "-o", "--output") == expected

    def test_it_is_the_same_parser_the_namespace_flag_uses(self):
        """One parser, so the two readers cannot drift apart again — which is how this bug began."""
        from app.tools.kubectl_tool import _extract_namespace
        args = ["kubectl", "get", "pods", "-nkube-system"]
        assert _extract_namespace(args) == _flag_value(args, "-n", "--namespace") == "kube-system"
