"""`kubeintellect init` must finish on a machine that has no Docker.

`_docker_available()` ran `subprocess.run(["docker", "info"], ...)` and returned
the comparison on its return code. When Docker is not installed at all there is
no return code: `subprocess.run` raises FileNotFoundError from the exec. The
predicate therefore raised precisely in the case it exists to detect.

Measured on a clean Ubuntu 24.04 Azure VM on 2026-08-29 against the published
2.4.0: `kubeintellect init` printed the full "Setup complete" banner, wrote both
.env files and the API key, and then died with a traceback ending in
`_docker_available`. The onboarding command documented on every install page
crashed after telling the user it had succeeded.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from app.cli import _docker_available


def test_no_docker_binary_reads_as_unavailable_not_a_crash():
    with patch("subprocess.run", side_effect=FileNotFoundError("docker")):
        assert _docker_available() is False


def test_a_hung_docker_daemon_reads_as_unavailable():
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="docker info", timeout=5),
    ):
        assert _docker_available() is False


def test_a_working_docker_still_reads_as_available():
    with patch("subprocess.run", return_value=subprocess.CompletedProcess(
        args=["docker", "info"], returncode=0, stdout=b"", stderr=b"",
    )):
        assert _docker_available() is True


def test_a_present_but_broken_docker_reads_as_unavailable():
    with patch("subprocess.run", return_value=subprocess.CompletedProcess(
        args=["docker", "info"], returncode=1, stdout=b"", stderr=b"denied",
    )):
        assert _docker_available() is False
