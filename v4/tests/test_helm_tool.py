"""Unit tests for the run_helm tool (Track C)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from app.tools.helm_tool import run_helm


def _invoke(command: str) -> str:
    return run_helm.invoke({"command": command})


# ── Shell injection guard ──────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_cmd", [
    "helm list; rm -rf /",
    "helm list && echo pwned",
    "helm list | cat /etc/passwd",
    "helm list > /tmp/out",
    "helm list `id`",
    "helm list $(whoami)",
])
def test_rejects_shell_metacharacters(bad_cmd: str) -> None:
    result = _invoke(bad_cmd)
    assert "disallowed shell characters" in result


# ── Write-verb block ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("write_cmd", [
    "helm install my-release ./chart",
    "helm upgrade my-release ./chart -n prod",
    "helm rollback my-release 1",
    "helm uninstall my-release -n prod",
    "helm repo add stable https://charts.helm.sh/stable",
    "helm plugin install https://example.com/plugin",
])
def test_rejects_write_verbs(write_cmd: str) -> None:
    result = _invoke(write_cmd)
    assert "Not Allowed" in result
    assert "write operation" in result


# ── Unknown verb ───────────────────────────────────────────────────────────────

def test_rejects_unknown_verb() -> None:
    result = _invoke("helm frobulate my-release")
    assert "Not Allowed" in result
    assert "not a supported subcommand" in result


# ── Read-only verbs execute ────────────────────────────────────────────────────

def _mock_proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def test_helm_list_all_namespaces() -> None:
    proc = _mock_proc(stdout="NAME\tNAMESPACE\tREVISION\nmy-app\tdefault\t3\n")
    with patch("subprocess.run", return_value=proc) as mock_run:
        result = _invoke("helm list -A")
    assert "my-app" in result
    called_args = mock_run.call_args[0][0]
    assert called_args[0] == "helm"
    assert called_args[1] == "list"
    assert "-A" in called_args


def test_helm_status() -> None:
    proc = _mock_proc(stdout="STATUS: deployed\nLAST DEPLOYED: Mon May 10 2026\n")
    with patch("subprocess.run", return_value=proc):
        result = _invoke("helm status my-release -n default")
    assert "deployed" in result


def test_helm_get_values() -> None:
    proc = _mock_proc(stdout="replicaCount: 2\nimage:\n  tag: v1.2.3\n")
    with patch("subprocess.run", return_value=proc):
        result = _invoke("helm get values my-release -n prod")
    assert "replicaCount" in result


def test_helm_get_manifest() -> None:
    proc = _mock_proc(stdout="---\napiVersion: apps/v1\nkind: Deployment\n")
    with patch("subprocess.run", return_value=proc):
        result = _invoke("helm get manifest my-release -n prod")
    assert "Deployment" in result


def test_helm_history() -> None:
    proc = _mock_proc(stdout="REVISION\tSTATUS\n1\tdeployed\n2\tsuperseded\n")
    with patch("subprocess.run", return_value=proc):
        result = _invoke("helm history my-release -n default")
    assert "REVISION" in result


def test_helm_version() -> None:
    proc = _mock_proc(stdout='version.BuildInfo{Version:"v3.14.0"}')
    with patch("subprocess.run", return_value=proc):
        result = _invoke("helm version")
    assert "v3" in result


def test_helm_env() -> None:
    proc = _mock_proc(stdout="HELM_CACHE_HOME=/home/user/.cache/helm\n")
    with patch("subprocess.run", return_value=proc):
        result = _invoke("helm env")
    assert "HELM_CACHE_HOME" in result


# ── Command normalisation ──────────────────────────────────────────────────────

def test_auto_prefixes_helm() -> None:
    proc = _mock_proc(stdout="NAME\n")
    with patch("subprocess.run", return_value=proc) as mock_run:
        _invoke("list -A")  # no "helm" prefix
    called_args = mock_run.call_args[0][0]
    assert called_args[0] == "helm"
    assert called_args[1] == "list"


# ── Output cap ────────────────────────────────────────────────────────────────

def test_output_is_capped() -> None:
    proc = _mock_proc(stdout="x" * 10_000)
    with patch("subprocess.run", return_value=proc):
        result = _invoke("helm list -A")
    assert len(result) < 7_000
    assert "truncated" in result


# ── Error handling ────────────────────────────────────────────────────────────

def test_helm_not_found() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = _invoke("helm list")
    assert "not found" in result.lower()


def test_helm_nonzero_exit_returns_stderr() -> None:
    proc = _mock_proc(stderr="Error: release not found", returncode=1)
    with patch("subprocess.run", return_value=proc):
        result = _invoke("helm status missing-release -n default")
    assert "release not found" in result
