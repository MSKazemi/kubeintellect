"""`kubeintellect init` must not print "Setup complete" over a config that cannot run.

The wizard validated the file it had just written, printed

    Issues detected in the saved configuration:
      [error]  OPENAI_API_KEY: LLM_PROVIDER=openai but OPENAI_API_KEY is not set.

and then, four lines later:

    ── Setup complete ───────────────────────────────────────────────────────

…offered to install a systemd service that starts the server on every login, and exited
**0**. Reproduced 2026-08-24 by pressing Enter at the key prompt — the single most likely
thing a first-time user does when they do not have a key to hand yet.

Two readers are misled by that, and the second is the one that cannot argue back:

* the human, who is told the setup completed and then meets a server that answers no
  question it is asked, with no memory of a warning four lines above a green banner; and
* `kubeintellect init && kubeintellect serve`, which sees exit 0 and carries on.

`kubeintellect status` has classified this *same* issue list — the same function, the same
`level == "error"` — as an exit-1 failure since 2026-08-24. One classifier, two consumers,
and only one of them was reading it. That is the shape this pins: the wizard that *wrote*
the file must not be more optimistic about it than the command that merely reads it.

Warnings stay warnings. A missing kubeconfig is a normal state on a laptop before
`kubeintellect kind-setup` runs, and it must not fail the setup.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import io

import pytest

from app import cli

_CLEARED = (
    "LLM_PROVIDER",
    "OPENAI_API_KEY", "OPENAI_COORDINATOR_MODEL", "OPENAI_SUBAGENT_MODEL",
    "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
    "AZURE_COORDINATOR_DEPLOYMENT", "AZURE_SUBAGENT_DEPLOYMENT",
    "DATABASE_URL", "POSTGRES_HOST", "POSTGRES_PASSWORD", "KUBECONFIG_PATH",
    "KUBEINTELLECT_ADMIN_KEYS", "KUBEINTELLECT_OPERATOR_KEYS", "KUBEINTELLECT_READONLY_KEYS",
    "PROMETHEUS_URL", "LOKI_URL", "LANGFUSE_HOST", "LANGFUSE_ENABLED",
)


class _Wizard:
    """What one `kubeintellect init` run did: its output, its exit code, its side effects."""

    def __init__(self, out: str, code: int, prompts: list[str], started_db: bool):
        self.out = out
        self.code = code
        self.prompts = prompts
        self.started_db = started_db

    @property
    def offered_to_start_anything(self) -> bool:
        return any("Start server" in p or "background service" in p for p in self.prompts)


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    """Run `cmd_init` against an isolated HOME and cwd, answering a scripted list.

    The cwd matters: `cmd_init` reads `./.env` as a config source, so a run inside the
    repository would quietly borrow the repository's own key and never reach the state
    under test.
    """
    def run(answers: list[str]) -> _Wizard:
        home = tmp_path / "home"
        cwd = tmp_path / "cwd"
        home.mkdir(exist_ok=True)
        cwd.mkdir(exist_ok=True)
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("HOME", str(home))
        for key in _CLEARED:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(cli, "_CONFIG_DIR", home / ".kubeintellect")
        monkeypatch.setattr(cli, "_CONFIG_FILE", home / ".kubeintellect" / ".env")
        monkeypatch.setattr(cli, "_ensure_tool", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_systemd_available", lambda: False)

        seen: dict[str, bool] = {"db": False}

        def _db() -> None:
            seen["db"] = True

        monkeypatch.setattr(cli, "_ensure_database", _db)

        prompts: list[str] = []
        pending = iter(answers)

        def _input(prompt: str = "") -> str:
            prompts.append(prompt)
            return next(pending)

        monkeypatch.setattr(builtins, "input", _input)

        buf, code = io.StringIO(), 0
        try:
            with contextlib.redirect_stdout(buf):
                cli.cmd_init(argparse.Namespace())
        except SystemExit as exc:
            code = exc.code or 0
        return _Wizard(buf.getvalue(), code, prompts, seen["db"])

    return run


# `1` = OpenAI · the key · `n` = no Kind cluster · `n` = do not start the server
NO_KEY = ["1", "", "n", "n"]
WITH_KEY = ["1", "sk-proj-not-a-real-key", "n", "n"]


class TestAnUnusableConfigIsNotACompletedSetup:
    def test_pressing_enter_at_the_key_prompt_does_not_complete_the_setup(self, wizard):
        result = wizard(NO_KEY)
        assert result.code == 1
        assert "Setup complete" not in result.out

    def test_it_says_which_settings_are_blocking(self, wizard):
        """*In the banner* — the issue list above it already names them, and is not read."""
        result = wizard(NO_KEY)
        assert "Setup INCOMPLETE" in result.out
        banner = result.out.split("Setup INCOMPLETE", 1)[1]
        assert "OPENAI_API_KEY" in banner
        assert "1 setting(s) must be fixed" in banner.replace("\x1b[1m", "").replace("\x1b[0m", "")

    def test_the_work_it_did_do_is_not_thrown_away(self, wizard, tmp_path):
        """Failing must not mean "start over" — the file and the key were written."""
        result = wizard(NO_KEY)
        config = tmp_path / "home" / ".kubeintellect" / ".env"
        assert config.exists()
        assert "LLM_PROVIDER=openai" in config.read_text()
        assert "were still written" in result.out

    def test_it_does_not_offer_to_start_a_server_that_cannot_answer(self, wizard):
        result = wizard(NO_KEY)
        assert not result.offered_to_start_anything

    def test_it_does_not_provision_a_database_for_a_setup_that_failed(self, wizard):
        """`_ensure_database` can start a Docker container. Not for a failed setup."""
        assert wizard(NO_KEY).started_db is False

    def test_it_points_at_the_command_that_rechecks(self, wizard):
        assert "kubeintellect status" in wizard(NO_KEY).out


class TestAWorkingConfigStillCompletes:
    def test_a_key_completes_the_setup(self, wizard):
        result = wizard(WITH_KEY)
        assert result.code == 0
        assert "Setup complete" in result.out
        assert "Setup INCOMPLETE" not in result.out

    def test_a_missing_kubeconfig_is_a_warning_not_a_failure(self, wizard, tmp_path):
        """No cluster yet is the normal state before `kubeintellect kind-setup`."""
        result = wizard(WITH_KEY)
        assert not (tmp_path / "home" / ".kube" / "config").exists()
        assert result.code == 0

    def test_the_completed_path_still_resolves_the_database(self, wizard):
        assert wizard(WITH_KEY).started_db is True


class TestTheWizardAndStatusReadTheSameClassifier:
    """One `_validate_config`, one meaning for `error` — in both commands."""

    @pytest.mark.parametrize(
        ("cfg", "blocking"),
        [
            ({"LLM_PROVIDER": "openai"}, ["OPENAI_API_KEY"]),
            ({"LLM_PROVIDER": "azure", "AZURE_OPENAI_ENDPOINT": "https://x.openai.azure.com/"},
             ["AZURE_OPENAI_API_KEY"]),
            ({"LLM_PROVIDER": "azure", "AZURE_OPENAI_API_KEY": "k"},
             ["AZURE_OPENAI_ENDPOINT"]),
            ({"LLM_PROVIDER": "azure", "AZURE_OPENAI_API_KEY": "k",
              "AZURE_OPENAI_ENDPOINT": "my-resource.openai.azure.com"},
             ["AZURE_OPENAI_ENDPOINT"]),
            ({"LLM_PROVIDER": "gemini"}, ["LLM_PROVIDER"]),
            ({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k", "DATABASE_URL": "mysql://x/y"},
             ["DATABASE_URL"]),
        ],
    )
    def test_every_error_level_setting_would_block_the_wizard(self, cfg, blocking, tmp_path):
        cfg = {"KUBECONFIG_PATH": str(tmp_path), **cfg}
        errors = [i.field for i in cli._validate_config(cfg) if i.level == "error"]
        assert errors == blocking

    def test_a_warning_only_config_blocks_neither_command(self, tmp_path):
        cfg = {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k",
               "KUBECONFIG_PATH": str(tmp_path / "nope"),
               "PROMETHEUS_URL": "prometheus:9090"}
        issues = cli._validate_config(cfg)
        assert issues, "this config is meant to produce warnings"
        assert [i for i in issues if i.level == "error"] == []

    def test_the_wizard_gates_on_error_level_and_nothing_else(self, wizard, monkeypatch):
        """Swap the classifier's verdict and the wizard's verdict must follow it."""
        warn_only = [cli._Issue("PROMETHEUS_URL", "warn", "not a URL", "fix it")]
        monkeypatch.setattr(cli, "_validate_config", lambda _cfg: warn_only)
        assert wizard(NO_KEY).code == 0

        blocking = [cli._Issue("SOMETHING", "error", "unusable", "fix it")]
        monkeypatch.setattr(cli, "_validate_config", lambda _cfg: blocking)
        result = wizard(WITH_KEY)
        assert result.code == 1
        assert "SOMETHING" in result.out
