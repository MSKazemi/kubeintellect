"""A developer's own config files must not decide what the suite says (#300).

`Settings` reads `~/.kubeintellect/.env` (written by `kubeintellect init`) and the
project `.env` in the working directory. That is right for the product and wrong for
the suite: the first outside contributor to run the tests on a machine where they had
set KubeIntellect up got two red tests that CI never sees (discussion #274) —
`USE_SQLITE=true` turned the digest's "Quiet watch" into "INCOMPLETE", and a reachable
`PROMETHEUS_URL` turned the GPU collector's "could not ask" (`None`) into "asked, got
nothing" (`0.0`). Both tests were right; the configuration they ran under was not theirs.

The product must keep reading both files — `test_the_config_still_reads_the_home_file`
pins that — so the neutralisation lives in `tests/conftest.py`, and this test runs the
affected tests in a child process whose home and working directory hold a hostile
`.env`, the only way to put a file in front of the import that reads it.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

V4 = Path(__file__).resolve().parents[1]

# Each one fails on a hostile file without the conftest neutralisation — measured on
# 59f70954 before the fix. The first two are the pair reported in #274; the last two
# build their own bare `Settings()`, which reads the same files.
VICTIMS = [
    "tests/test_digest.py::TestDigestBuilder::test_empty_window",
    "tests/test_agentic_gpu_collector.py::TestCollect::test_unreadable_metrics_are_none_not_zero",
    "tests/test_llm_temperature.py::test_default_temperature_is_zero",
    "tests/test_both_observation_queues_take_the_same_knob.py::test_it_defaults_to_its_twin",
]
PLANTED = ("USE_SQLITE", "PROMETHEUS_URL", "LLM_TEMPERATURE", "MEMORY_OBS_QUEUE_MAXSIZE")


class _EmptyVector(http.server.BaseHTTPRequestHandler):
    """A Prometheus that answers every query with "no series" — reachable, so not `None`."""

    def do_GET(self) -> None:  # noqa: N802 — the stdlib's name
        body = json.dumps({"status": "success", "data": {"resultType": "vector", "result": []}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def answering_prometheus():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EmptyVector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    # Vacuity guard: an unreachable stub would let the GPU victim pass for the wrong reason.
    with urllib.request.urlopen(f"{url}/api/v1/query?query=up", timeout=5) as resp:
        assert json.load(resp)["status"] == "success"
    yield url
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("where", ["home", "project"])
def test_a_hostile_env_file_does_not_change_a_test_result(where, tmp_path, answering_prometheus):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    (home / ".kubeintellect").mkdir(parents=True)
    cwd.mkdir()
    target = home / ".kubeintellect" / ".env" if where == "home" else cwd / ".env"
    target.write_text(
        "USE_SQLITE=true\n"
        f"PROMETHEUS_URL={answering_prometheus}\n"
        "LLM_TEMPERATURE=0.7\n"
        "MEMORY_OBS_QUEUE_MAXSIZE=7\n",
        encoding="utf-8",
    )

    # Only the file may speak: an inherited variable would outrank it and hide the leak.
    env = {k: v for k, v in os.environ.items() if k not in PLANTED}
    env["HOME"] = env["USERPROFILE"] = str(home)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--rootdir", str(V4), "-c", str(V4 / "pyproject.toml"),
         *(str(V4 / victim) for victim in VICTIMS)],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180,
        check=False,
    )
    assert result.returncode == 0, (
        f"a {where} .env changed the outcome of tests that pass in CI:\n{result.stdout[-4000:]}"
    )
    assert f"{len(VICTIMS)} passed" in result.stdout, result.stdout[-2000:]
