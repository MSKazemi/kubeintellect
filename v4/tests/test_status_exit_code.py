"""`kubeintellect status` must say in its exit code what it says on screen.

The command is documented as a "Health dashboard for every component" and is the obvious thing
to put in a Makefile, a container healthcheck, or `kubeintellect status && kubeintellect serve`.
Until 2026-08-24 it returned 0 unconditionally: measured in a clean HOME it printed four ✗ rows —
missing config file, no LLM key, an unreachable PostgreSQL and a missing kubeconfig — and exited
**0**, so none of those uses could ever fail.

A `-` row is deliberately *not* a failure. It means "not configured", which is a choice: nobody
running without Prometheus should have their healthcheck go red for it.
"""

from __future__ import annotations

import subprocess

import pytest

from app import cli


@pytest.fixture
def board(tmp_path, monkeypatch, capsys):
    """A fully green board, plus a knob per row so a test can break exactly one thing."""
    user_env = tmp_path / "user.env"
    user_env.write_text("USE_SQLITE=true\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_CONFIG_FILE", user_env)
    monkeypatch.chdir(tmp_path)

    for var in (
        "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "DATABASE_URL",
        "PROMETHEUS_URL", "LOKI_URL", "GRAFANA_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setenv("USE_SQLITE", "true")
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    monkeypatch.setenv("KUBEINTELLECT_ADMIN_KEYS", "ki-admin-abc")

    sqlite_path = tmp_path / "kubeintellect.db"
    sqlite_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("SQLITE_PATH", str(sqlite_path))

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    monkeypatch.setenv("KUBECONFIG_PATH", str(kubeconfig))
    monkeypatch.setattr(cli, "_cluster_reachable", lambda _p: True)
    monkeypatch.setattr(cli, "_get_kube_context", lambda _p: "test-ctx")

    # `which kubectl` / `which kq` must not depend on the machine running the suite.
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "/usr/bin/x\n", ""),
    )
    monkeypatch.setattr(cli, "_http_ok", lambda _url, timeout=3.0: True)

    def _run() -> tuple[int, str]:
        capsys.readouterr()
        code = 0
        try:
            cli.cmd_status(None)
        except SystemExit as exc:
            code = int(exc.code or 0)
        return code, capsys.readouterr().out

    return _run


class TestTheExitCodeMatchesTheBoard:
    def test_a_green_board_exits_zero(self, board, capsys):
        """Vacuity guard: without it, "exits 1 when broken" would pass for a command that
        always exits 1."""
        code, out = board()
        assert code == 0, out
        assert "Not working" not in out

    def test_a_missing_llm_key_exits_one(self, board, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        code, out = board()
        assert code == 1
        assert "Not working: LLM" in out

    def test_an_unreachable_cluster_exits_one(self, board, monkeypatch):
        monkeypatch.setattr(cli, "_cluster_reachable", lambda _p: False)
        code, out = board()
        assert code == 1
        assert "cluster" in out.split("Not working:")[1]

    def test_an_unreachable_postgres_exits_one(self, board, monkeypatch):
        monkeypatch.setenv("USE_SQLITE", "false")
        monkeypatch.setattr(cli, "_check_db", lambda _dsn: False)
        code, out = board()
        assert code == 1
        assert "DB" in out.split("Not working:")[1]

    def test_every_broken_component_is_named_once(self, board, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(cli, "_cluster_reachable", lambda _p: False)
        code, out = board()
        assert code == 1
        named = out.split("Not working:")[1].split("\n")[0]
        assert "LLM" in named and "cluster" in named
        assert named.count("LLM") == 1, f"duplicated component: {named!r}"


class TestAWarningIsNotAFailure:
    def test_unconfigured_observability_does_not_fail(self, board, monkeypatch):
        """Prometheus / Loki / Grafana / Langfuse print `-` when unset. Running without them is
        a choice, and a choice must not turn a healthcheck red."""
        code, out = board()
        assert code == 0
        assert "Prometheus:-" in out.replace(" ", "") or "not configured" in out

    def test_a_configured_but_unreachable_prometheus_does_fail(self, board, monkeypatch):
        """Vacuity guard for the test above: `-` is ignored because it means *unset*, not
        because observability rows are exempt."""
        monkeypatch.setenv("PROMETHEUS_URL", "http://prom.invalid:9090")
        monkeypatch.setattr(cli, "_http_ok", lambda _url, timeout=3.0: False)
        code, out = board()
        assert code == 1
        assert "Prometheus" in out.split("Not working:")[1]

    def test_a_warn_level_config_issue_does_not_fail(self, board, monkeypatch):
        """`Auth: - open access` is a warn-level issue, and running open is a choice."""
        monkeypatch.delenv("KUBEINTELLECT_ADMIN_KEYS", raising=False)
        code, out = board()
        assert "open access" in out, out
        assert code == 0, out

    def test_an_error_level_config_issue_does_fail(self, board, monkeypatch):
        """The board can be all-✓ and the configuration still be wrong: a malformed
        DATABASE_URL is an *error* from `_validate_config` and has no row of its own. With
        USE_SQLITE=true nothing reads it, so this is the config check on its own."""
        monkeypatch.setenv("DATABASE_URL", "mysql://user@localhost/db")
        code, out = board()
        assert "DATABASE_URL" in out
        assert code == 1, out
        assert "config" in out.split("Not working:")[1]

    def test_a_missing_sqlite_file_does_not_fail(self, board, monkeypatch, tmp_path):
        """It is created on first start — the row says so and prints `-`, not ✗."""
        monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "not-yet.db"))
        code, out = board()
        assert "will be created on first start" in out
        assert code == 0


class TestTheMessageIsActionable:
    def test_it_explains_why_the_command_failed(self, board, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        _code, out = board()
        assert "exits 1 when any row is ✗" in out
