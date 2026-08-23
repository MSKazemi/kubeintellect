"""`GET /v1/namespaces` must not report an unreachable cluster as an empty one.

The endpoint shells out to `kubectl get namespaces`. It never checked the return code, so any
failure — unreachable API server, expired credentials, RBAC denial, wrong `KUBECONFIG_PATH` —
produced empty stdout and was returned as `200 {"namespaces": []}`.

That is the shape this audit keeps finding: a value that means "none" standing in for a state that
is "unknown". Here it travelled. `kq` uses this endpoint to validate `/ns <name>`, and its REPL is
deliberately careful — it keeps three states and only rejects a namespace on a definite absence,
so an outage cannot block an operator. A 200 with an empty list *is* a definite absence, so the
care was defeated and the operator was told:

    Namespace 'prod' not found in the cluster.

during exactly the incident where their credentials had just expired. The same empty list also
silently emptied `kq`'s namespace tab-completion.

A failure is now a 503 carrying the first line of kubectl's stderr, which is where the actionable
text lives ("connection refused", "Unauthorized"). An empty list now means one thing only.

The protected-namespace filter (added 2026-08-20, same endpoint) is re-asserted here so a future
change to the error handling cannot quietly drop it.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from app.api.v1.endpoints.namespaces import router
from app.core.config import settings
from fastapi import FastAPI
from starlette.testclient import TestClient


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["kubectl"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class TestAFailedKubectlIsReportedAsAFailure:
    @pytest.mark.parametrize(
        ("label", "stderr"),
        [
            ("unreachable API server",
             "The connection to the server localhost:8080 was refused - did you specify the "
             "right host or port?"),
            ("expired credentials", "error: You must be logged in to the server (Unauthorized)"),
            ("rbac denial",
             'Error from server (Forbidden): namespaces is forbidden: User "dev" cannot list '
             'resource "namespaces" at the cluster scope'),
            ("bad kubeconfig", "error: stat /nonexistent/config: no such file or directory"),
        ],
    )
    def test_it_is_a_503_not_an_empty_list(self, label, stderr):
        with patch("subprocess.run", return_value=_proc(1, "", stderr)):
            r = _client().get("/namespaces")
        assert r.status_code == 503, f"{label}: an unreachable cluster looked like an empty one"
        assert "namespaces" not in r.json(), r.json()

    def test_the_reason_reaches_the_caller(self):
        with patch("subprocess.run",
                   return_value=_proc(1, "", "The connection to the server was refused")):
            detail = _client().get("/namespaces").json()["detail"]
        assert "connection to the server was refused" in detail.lower(), detail

    def test_only_the_first_stderr_line_is_passed_on(self):
        noisy = "error: connection refused\nUsage:\n  kubectl get [flags]\n" + "x" * 500
        with patch("subprocess.run", return_value=_proc(1, "", noisy)):
            detail = _client().get("/namespaces").json()["detail"]
        assert "Usage:" not in detail and len(detail) < 350, detail

    def test_a_silent_failure_still_reports_the_exit_code(self):
        with patch("subprocess.run", return_value=_proc(7, "", "")):
            detail = _client().get("/namespaces").json()["detail"]
        assert "7" in detail, detail

    def test_kubectl_missing_is_a_503(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("kubectl")):
            r = _client().get("/namespaces")
        assert r.status_code == 503 and "not installed" in r.json()["detail"]

    def test_a_kubectl_timeout_is_a_503(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("kubectl", 10)):
            r = _client().get("/namespaces")
        assert r.status_code == 503 and "did not respond" in r.json()["detail"]


class TestSuccessIsStillSuccess:
    def test_a_working_cluster_returns_its_namespaces(self):
        with patch("subprocess.run", return_value=_proc(0, "default prod staging")):
            r = _client().get("/namespaces")
        assert r.status_code == 200
        assert r.json()["namespaces"] == ["default", "prod", "staging"]

    def test_a_genuinely_empty_result_is_still_200_with_an_empty_list(self):
        """The distinction only means something if the honest empty case survives."""
        with patch("subprocess.run", return_value=_proc(0, "   ")):
            r = _client().get("/namespaces")
        assert r.status_code == 200 and r.json()["namespaces"] == []

    def test_stderr_on_a_successful_call_is_ignored(self):
        """kubectl writes deprecation warnings to stderr while succeeding."""
        with patch("subprocess.run",
                   return_value=_proc(0, "default prod", "W0820 deprecated flag")):
            r = _client().get("/namespaces")
        assert r.status_code == 200 and r.json()["namespaces"] == ["default", "prod"]


class TestTheProtectedFilterSurvivesTheErrorHandling:
    def test_blocked_namespaces_are_still_removed(self):
        with patch.object(settings, "KUBECTL_BLOCKED_NAMESPACES", "kube-system,kubeintellect"), \
             patch("subprocess.run",
                   return_value=_proc(0, "default kube-system prod kubeintellect")):
            names = _client().get("/namespaces").json()["namespaces"]
        assert names == ["default", "prod"], names

    def test_a_list_that_is_entirely_blocked_is_an_empty_200_not_an_error(self):
        """Withholding everything is a real answer; it must not masquerade as a failure."""
        with patch.object(settings, "KUBECTL_BLOCKED_NAMESPACES", "kube-system"), \
             patch("subprocess.run", return_value=_proc(0, "kube-system")):
            r = _client().get("/namespaces")
        assert r.status_code == 200 and r.json()["namespaces"] == []
