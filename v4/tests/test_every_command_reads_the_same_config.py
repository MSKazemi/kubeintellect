"""`status` must not report a configuration the server will not run on.

`kubeintellect status` is what `docs/troubleshooting.md` and `docs/quickstart.md` tell an operator
to run to check their setup. It read `~/.kubeintellect/.env` **and** a directory-local `./.env`.
`kubeintellect serve` and `kubeintellect db-init` read the user file alone — and `docs/index.md`
tells readers to `cp .env.example .env`, so the split was the normal case, not a corner.

Measured before the fix, in one directory, at one moment:

    status:  LLM  ✓  azure / gpt-4o   configured
             Auth ✓  enabled · admin ki-admin-abc
             ✓  No configuration issues found.
    serve:   AZURE_OPENAI_API_KEY seen by the server process: None
             [error] LLM_PROVIDER=azure but AZURE_OPENAI_API_KEY is not set.

The `Auth` row is the one that matters: a green board saying authentication is on, printing the
key, over a server that accepts unauthenticated requests. All three commands now load through
`_load_effective_config`, so the board and the runtime cannot disagree.
"""

from __future__ import annotations

import os

import pytest

from app import cli

_PROJECT_ENV = (
    "LLM_PROVIDER=azure\n"
    "AZURE_OPENAI_API_KEY=lives-only-in-project-env\n"
    "AZURE_OPENAI_ENDPOINT=https://r.openai.azure.com/\n"
    "KUBEINTELLECT_ADMIN_KEYS=ki-admin-abc\n"
)

_VARS = (
    "LLM_PROVIDER", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
    "KUBEINTELLECT_ADMIN_KEYS", "KUBEINTELLECT_OPERATOR_KEYS", "KUBEINTELLECT_READONLY_KEYS",
)


@pytest.fixture
def project_only(tmp_path, monkeypatch):
    """A user config file with no LLM settings, and a ./.env that has them all."""
    user_env = tmp_path / "user.env"
    user_env.write_text("USE_SQLITE=true\n")
    (tmp_path / ".env").write_text(_PROJECT_ENV)
    monkeypatch.setattr(cli, "_CONFIG_FILE", user_env)
    monkeypatch.chdir(tmp_path)
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)
    return tmp_path


class TestTheLoaderIsOneCodePath:
    def test_serve_sees_what_status_reported(self, project_only):
        cli._load_effective_config()
        assert os.environ["AZURE_OPENAI_API_KEY"] == "lives-only-in-project-env"

    def test_the_admin_key_reaches_the_server(self, project_only):
        """The security-relevant half: 'Auth ✓ enabled' over an open-access server."""
        cli._load_effective_config()
        assert os.environ["KUBEINTELLECT_ADMIN_KEYS"] == "ki-admin-abc"
        assert not cli._validate_config(dict(os.environ)), "the board said this config was clean"

    @pytest.mark.parametrize("command", ["cmd_status", "cmd_serve", "cmd_db_init"])
    def test_all_three_commands_load_through_the_shared_helper(self, command):
        """A fourth copy of the four-line loader is how this defect came back."""
        import inspect
        src = inspect.getsource(getattr(cli, command))
        assert "_load_effective_config()" in src, f"{command} loads config its own way"
        assert "_load_dotenv(_CONFIG_FILE)" not in src, f"{command} still has a private loader"


class TestPrecedenceIsUnchanged:
    def test_an_exported_variable_still_wins(self, project_only, monkeypatch):
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "from-the-shell")
        cli._load_effective_config()
        assert os.environ["AZURE_OPENAI_API_KEY"] == "from-the-shell"

    def test_the_user_config_file_still_beats_the_project_one(self, project_only):
        (project_only / "user.env").write_text(
            "USE_SQLITE=true\nAZURE_OPENAI_API_KEY=from-the-user-file\n"
        )
        cli._load_effective_config()
        assert os.environ["AZURE_OPENAI_API_KEY"] == "from-the-user-file"

    def test_a_missing_project_env_is_not_an_error(self, tmp_path, monkeypatch):
        user_env = tmp_path / "user.env"
        user_env.write_text("USE_SQLITE=true\n")
        monkeypatch.setattr(cli, "_CONFIG_FILE", user_env)
        monkeypatch.chdir(tmp_path)
        cli._load_effective_config()          # no ./.env here at all
        assert os.environ.get("USE_SQLITE") == "true"

    def test_no_config_at_all_does_not_raise(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "_CONFIG_FILE", tmp_path / "absent.env")
        monkeypatch.chdir(tmp_path)
        cli._load_effective_config()


class TestTheseTestsWouldNoticeTheDefect:
    def test_the_project_env_is_the_only_source_of_the_key(self, project_only):
        """Vacuity guard: if the key were also in the user file, every test above would pass anyway."""
        assert "AZURE_OPENAI_API_KEY" not in (project_only / "user.env").read_text()
        assert "AZURE_OPENAI_API_KEY" not in os.environ
