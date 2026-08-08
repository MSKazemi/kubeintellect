"""Unit tests for the kubectl error interpreter (C1)."""
from __future__ import annotations

import pytest

from app.tools import kubectl_errors


@pytest.mark.parametrize(
    "stderr,expected_pattern",
    [
        ('Error from server (NotFound): pods "foo" not found', "pod_not_found"),
        ('Error from server (NotFound): namespaces "missing" not found', "namespace_not_found"),
        ("Error from server (Forbidden): pods is forbidden: User cannot list", "forbidden"),
        ("Unable to connect to the server: dial tcp 1.2.3.4:6443: connect: connection refused", "apiserver_unreachable"),
        ("Unable to connect to the server: dial tcp: lookup api.k8s.local: no such host", "unable_to_connect"),
        ("error: the server could not find the requested resource", "missing_resource_type"),
        ("error: unable to recognize \"manifest.yaml\": no matches for kind", "crd_not_recognized"),
        ("Error: container nginx is not valid for pod", "container_not_found"),
        ("etcdserver: leader changed", "etcd_recovering"),
        ("error converting YAML to JSON: yaml: line 4: mapping values are not allowed here", "yaml_parse_error"),
        ("The Service \"my-svc\" is invalid: spec.clusterIP: field is immutable", "immutable_field"),
        ("Operation cannot be fulfilled on deployments.apps \"app\": the object has been modified", "concurrent_modification"),
        ("error: MethodNotAllowed", "method_not_allowed"),
    ],
)
def test_interpret_matches_known_patterns(stderr: str, expected_pattern: str) -> None:
    name, hint = kubectl_errors.interpret(stderr)
    assert name == expected_pattern, f"expected {expected_pattern}, got {name}"
    assert hint and hint.startswith("→ ")


def test_interpret_returns_none_for_unknown() -> None:
    name, hint = kubectl_errors.interpret("some completely novel error message")
    assert name is None
    assert hint is None


def test_interpret_handles_empty_input() -> None:
    assert kubectl_errors.interpret("") == (None, None)
    assert kubectl_errors.interpret(None) == (None, None)  # type: ignore[arg-type]


def test_annotate_appends_hint_only_once() -> None:
    err = 'Error from server (NotFound): pods "foo" not found'
    annotated, name = kubectl_errors.annotate(err)
    assert name == "pod_not_found"
    assert annotated.startswith(err)
    assert "→ " in annotated
    # Re-annotating must not double-append.
    re_annotated, _ = kubectl_errors.annotate(annotated)
    assert re_annotated == annotated


def test_annotate_passes_through_unknown() -> None:
    err = "weird unknown failure"
    out, name = kubectl_errors.annotate(err)
    assert out == err
    assert name is None
