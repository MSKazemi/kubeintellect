"""The `status` summary must be computed from the config the program actually runs on.

`kubeintellect status` ends with the line that answers the only question the command is asked:
*is anything wrong with my setup?* — either a list of issues or "✓ No configuration issues
found." Every row above it reads the effective environment: `cmd_status` loads
`~/.kubeintellect/.env` and then `./.env`, neither overwriting a variable already set, so the
rows reflect shell → user config → project config.

The summary read `~/.kubeintellect/.env` **alone**, and so contradicted the rows three lines above
it in both directions. Harmless direction: "[error] AZURE_OPENAI_API_KEY is not set" printed
underneath an "LLM: ✓ configured" row, because the key lives in `./.env`. Dangerous direction:
"✓ No configuration issues found." printed underneath an "LLM: ✗ OPENAI_API_KEY missing" row,
because the shell overrode `LLM_PROVIDER` — an all-clear over a configuration that cannot start.

`docs/troubleshooting.md` tells operators to resolve every ✗, which makes the summary the line
that decides whether they go looking. These tests pin it to the effective config.
"""

from __future__ import annotations

import contextlib

import pytest

from app import cli

_AZURE = (
    "AZURE_OPENAI_API_KEY=a-real-key\n"
    "AZURE_OPENAI_ENDPOINT=https://r.openai.azure.com/\n"
    "KUBEINTELLECT_ADMIN_KEYS=ki-admin-abc\n"
)


@pytest.fixture
def board(tmp_path, monkeypatch, capsys):
    """Render the board with a user config, a project config, and a controlled environment."""
    user_env = tmp_path / "user.env"
    monkeypatch.setattr(cli, "_CONFIG_FILE", user_env)
    monkeypatch.chdir(tmp_path)
    for var in (
        "LLM_PROVIDER", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
        "KUBEINTELLECT_ADMIN_KEYS", "KUBEINTELLECT_OPERATOR_KEYS", "KUBEINTELLECT_READONLY_KEYS",
        "PROMETHEUS_URL", "LOKI_URL", "GRAFANA_URL", "LANGFUSE_ENABLED", "DATABASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("USE_SQLITE", "true")
    monkeypatch.setenv("KUBECONFIG_PATH", str(tmp_path / "kubeconfig"))
    (tmp_path / "kubeconfig").write_text("apiVersion: v1\nkind: Config\n")
    monkeypatch.setattr(cli, "_cluster_reachable", lambda _p: None)

    def _render(*, user: str = "", project: str = "") -> str:
        user_env.write_text(user)
        if project:
            (tmp_path / ".env").write_text(project)
        capsys.readouterr()
        # `status` exits 1 when any row is ✗ (see test_status_exit_code.py). These tests are
        # about what the board *says*, and several of them render a deliberately broken one.
        with contextlib.suppress(SystemExit):
            cli.cmd_status(None)
        return capsys.readouterr().out

    return _render


class TestTheSummaryAgreesWithTheRowsAboveIt:
    def test_a_key_that_lives_in_the_project_env_is_not_reported_missing(self, board):
        out = board(user="USE_SQLITE=true\n", project=_AZURE)
        assert "LLM:" in out and "✗" not in out.split("LLM:")[1].split("\n")[0]
        assert "AZURE_OPENAI_API_KEY is not set" not in out
        assert "No configuration issues found" in out

    def test_an_admin_key_in_the_project_env_is_not_reported_as_open_access(self, board):
        out = board(user="USE_SQLITE=true\n", project=_AZURE)
        assert "No admin API key configured" not in out

    def test_a_provider_overridden_in_the_shell_is_the_one_validated(self, board, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        out = board(user="LLM_PROVIDER=azure\n" + _AZURE)
        # The row already says openai; the summary must not clear a config the row just failed.
        assert "openai /" in out
        assert "OPENAI_API_KEY: LLM_PROVIDER=openai but OPENAI_API_KEY is not set." in out
        assert "No configuration issues found" not in out

    def test_a_genuinely_broken_config_is_still_reported(self, board):
        out = board(user="LLM_PROVIDER=azure\nKUBEINTELLECT_ADMIN_KEYS=ki-admin-abc\n")
        assert "AZURE_OPENAI_API_KEY is not set" in out
        assert "No configuration issues found" not in out


class TestTheSummarySaysWhatItChecked:
    def test_the_all_clear_names_its_three_sources(self, board):
        out = board(user="USE_SQLITE=true\n", project=_AZURE)
        assert "No configuration issues found" in out
        assert "checked the effective config" in out
        assert "./.env" in out

    def test_a_project_env_alone_still_gets_a_summary(self, board, tmp_path, monkeypatch, capsys):
        """The config need not live in ~/.kubeintellect/.env for the question to be answerable."""
        monkeypatch.setattr(cli, "_CONFIG_FILE", tmp_path / "does-not-exist.env")
        (tmp_path / ".env").write_text(_AZURE)
        capsys.readouterr()
        with contextlib.suppress(SystemExit):   # no ~/.kubeintellect/.env ⇒ a ✗ row ⇒ exit 1
            cli.cmd_status(None)
        out = capsys.readouterr().out
        assert "No configuration issues found" in out


class TestTheseTestsWouldNoticeTheDefect:
    def test_the_project_env_is_actually_read_by_the_command(self, board, tmp_path):
        """Vacuity guard: if ./.env were ignored entirely, the cases above would prove nothing."""
        out = board(user="USE_SQLITE=true\n", project=_AZURE)
        assert "a-real-key" not in out, "the key must never be echoed"
        assert "ki-admin-abc" in out, "the Auth row proves ./.env reached the environment"

    def test_the_summary_block_is_reached_at_all(self, board):
        out = board(user="LLM_PROVIDER=nonsense\n")
        assert "Configuration issues:" in out
        assert "must be 'openai' or 'azure'" in out
