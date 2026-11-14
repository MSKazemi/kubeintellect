"""A manifest is checked against the cluster's schema before it is sent, and a wrong field is named.

Measured 2026-11-14 (`papers/v4-operator-r1/A3-LOOP-INVESTIGATION.md` §2d): E3's re-typed Job manifest had an
unquoted `- echo 'ERROR: …'` in `command`, which YAML parses as a mapping. Client-side `kubectl apply` — also with
`--dry-run=server` and `--validate=strict` — answered with an error naming no field, and the session resent it 73
times. The same manifest checked against the cluster's OpenAPI v3 schema names `containers[0].command[2]`.

The schema below is a minimal hand-written subset in the cluster's own format ($ref, single-element allOf, oneOf for
Quantity); the real Kubernetes 1.37 schema was checked by hand against the same cases (no false positives on valid
Deployments/Services/ConfigMaps/Jobs, including integer CPU quantities, IntOrString ports and timestamps).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from app.core.config import settings
from app.tools import kubectl_tool as kt
from app.tools import manifest_lint as ml

E3_MANIFEST = """apiVersion: batch/v1
kind: Job
metadata:
  name: report
spec:
  backoffLimit: 5
  template:
    metadata:
      labels:
        job-name: report
    spec:
      containers:
      - name: c
        image: docker.io/library/busybox:1.36
        command:
        - sh
        - -c
        - echo 'ERROR: nightly export failed (exit 1)'; exit 1
      restartPolicy: OnFailure
"""

_REF = "#/components/schemas/"
SCHEMA = {
    "components": {
        "schemas": {
            "io.k8s.api.batch.v1.Job": {
                "type": "object",
                "x-kubernetes-group-version-kind": [
                    {"group": "batch", "kind": "Job", "version": "v1"}
                ],
                "properties": {
                    "apiVersion": {"type": "string"},
                    "kind": {"type": "string"},
                    "metadata": {"allOf": [{"$ref": _REF + "ObjectMeta"}]},
                    "spec": {"allOf": [{"$ref": _REF + "JobSpec"}]},
                },
            },
            "ObjectMeta": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "namespace": {"type": "string"},
                    "labels": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "annotations": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
            },
            "JobSpec": {
                "type": "object",
                "properties": {
                    "backoffLimit": {"type": "integer"},
                    "template": {"allOf": [{"$ref": _REF + "PodTemplateSpec"}]},
                },
            },
            "PodTemplateSpec": {
                "type": "object",
                "properties": {
                    "metadata": {"allOf": [{"$ref": _REF + "ObjectMeta"}]},
                    "spec": {"allOf": [{"$ref": _REF + "PodSpec"}]},
                },
            },
            "PodSpec": {
                "type": "object",
                "properties": {
                    "restartPolicy": {"type": "string"},
                    "containers": {
                        "type": "array",
                        "items": {"allOf": [{"$ref": _REF + "Container"}]},
                    },
                },
            },
            "Container": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "image": {"type": "string"},
                    "imagePullPolicy": {"type": "string"},
                    "command": {"type": "array", "items": {"type": "string"}},
                    "env": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "value": {"type": "string"},
                            },
                        },
                    },
                    "resources": {
                        "type": "object",
                        "properties": {
                            "limits": {
                                "type": "object",
                                "additionalProperties": {"$ref": _REF + "Quantity"},
                            }
                        },
                    },
                },
            },
            "Quantity": {"oneOf": [{"type": "string"}, {"type": "number"}]},
        }
    }
}


@pytest.fixture
def cluster_schema():
    ml.clear_cache()
    with mock.patch.object(
        ml, "_fetch_openapi", lambda gv, kc: SCHEMA if gv == "apis/batch/v1" else None
    ):
        yield
    ml.clear_cache()


def _job(container: str) -> str:
    return (
        "apiVersion: batch/v1\nkind: Job\nmetadata: {name: r}\nspec:\n  template:\n    spec:\n"
        f"      restartPolicy: Never\n      containers:\n      - {container}\n"
    )


class TestTheLint:
    def test_e3s_manifest_is_named_with_cause_and_fix(self, cluster_schema):
        findings = ml.lint(E3_MANIFEST, "kc")
        assert len(findings) == 1
        assert findings[0].startswith(
            "spec.template.spec.containers[0].command[2] is a mapping"
        )
        assert "quote" in findings[0]

    def test_a_valid_manifest_has_no_findings(self, cluster_schema):
        ok = E3_MANIFEST.replace(
            "- echo 'ERROR: nightly export failed (exit 1)'; exit 1",
            "- \"echo 'ERROR: nightly export failed (exit 1)'; exit 1\"",
        )
        assert ml.lint(ok, "kc") == []

    def test_quantities_accept_numbers_and_strings(self, cluster_schema):
        assert (
            ml.lint(
                _job(
                    "{name: c, image: x, resources: {limits: {cpu: 1, memory: 64Mi}}}"
                ),
                "kc",
            )
            == []
        )

    def test_a_misspelt_field_is_named_with_the_closest_valid_one(self, cluster_schema):
        [f] = ml.lint(_job("{name: c, image: x, imagePullPolicyy: Always}"), "kc")
        assert "imagePullPolicyy is not a field" in f and "imagePullPolicy" in f

    @pytest.mark.parametrize("value, cause", [("8080", "number"), ("yes", "boolean")])
    def test_yaml_pitfalls_in_a_string_field(self, cluster_schema, value, cause):
        [f] = ml.lint(
            _job(f"{{name: c, image: x, env: [{{name: P, value: {value}}}]}}"), "kc"
        )
        assert "env[0].value" in f and cause in f and "quote" in f

    def test_it_never_turns_could_not_check_into_a_finding(self, cluster_schema):
        crd = "apiVersion: example.com/v1\nkind: Widget\nmetadata: {name: w}\nspec: {anything: [1, 2]}\n"
        assert ml.lint(crd, "kc") == []  # no schema for that group-version
        with mock.patch.object(ml, "_fetch_openapi", lambda gv, kc: None):
            ml.clear_cache()
            assert ml.lint(E3_MANIFEST, "kc") == []  # schema endpoint unreachable


def _apply(stdin: str, *, lint_on: bool = True):
    calls: list[list[str]] = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        return SimpleNamespace(
            returncode=0, stdout="job.batch/report configured\n", stderr=""
        )

    cfg = {
        "configurable": {
            "user_role": "operator",
            "hitl_bypass": True,
            "thread_id": "auto-l",
            "bound_namespace": "h3-2",
        }
    }
    with (
        mock.patch.object(settings, "MANIFEST_LINT_ENABLED", lint_on),
        mock.patch.object(kt.subprocess, "run", fake_run),
        mock.patch.object(kt, "_capture_rollback_point", lambda *a, **k: None),
        mock.patch.object(kt, "interrupt", lambda p: True),
    ):
        # A Job manifest carrying an `image` is always-confirm (ADR-006); a human approves here, so a manifest the
        # lint lets through reaches kubectl.
        out = kt.run_kubectl.invoke(
            {"command": "kubectl apply -f - -n h3-2", "stdin": stdin}, config=cfg
        )
    return calls, out


class TestTheToolRefusesBeforeSending:
    def test_e3s_manifest_never_reaches_the_cluster(self, cluster_schema):
        calls, out = _apply(E3_MANIFEST)
        assert not calls, "the malformed manifest must not be sent"
        assert out.startswith("[Invalid manifest: not sent to the cluster]")
        assert "containers[0].command[2]" in out and "[permanent error:" in out

    def test_a_valid_manifest_is_sent(self, cluster_schema):
        ok = E3_MANIFEST.replace(
            "- echo 'ERROR: nightly export failed (exit 1)'; exit 1", '- "echo ok"'
        )
        calls, out = _apply(ok)
        assert calls and "configured" in out

    def test_the_setting_turns_it_off(self, cluster_schema):
        calls, _ = _apply(E3_MANIFEST, lint_on=False)
        assert calls
