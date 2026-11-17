"""`kubeintellect init` / `status` printed secrets they had a function for masking.

Three faults, one cause: the CLI's idea of "masked" and "shown" was never stated once.

* `_mask` returned `value[:4] + "***" + value[-4:]` -- eight characters of every secret it
  masked, rendered live as `sk-d***0000`. Anything pasted from a screen-share, a recording or a
  bug report carried a quarter of the key.
* `status` printed all three API keys in full, every time it ran. `status` is the command put in
  Makefiles, healthchecks and bug-report templates; it is the *least* private output the tool has.
* `init` printed the generated key in full twice -- in the ✓ block and again in the closing
  banner -- and printed an *existing* key in full on every re-run, though nothing was created.

A generated key must be visible once, because the user has to copy it. After that it is in
`~/.kubeintellect/.env` and `~/.kube-q/.env`, and is never echoed again.
"""
from __future__ import annotations

import argparse
import builtins
import contextlib
import io
import re
import subprocess

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

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_ADMIN = "ki-admin-0123456789abcdef0123"
_OPERATOR = "ki-op-fedcba9876543210fedc"
_READONLY = "ki-ro-aaaabbbbccccddddeeee"
_LLM_KEY = "sk-proj-QRSTUVWXYZ0123456789zz"


class TestMaskRevealsNothing:
    @pytest.mark.parametrize("key", [
        "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "POSTGRES_PASSWORD",
        "KUBEINTELLECT_ADMIN_KEYS", "LANGFUSE_SECRET_KEY",
    ])
    def test_no_character_of_a_long_secret_survives(self, key):
        masked = cli._mask(key, _LLM_KEY)
        assert "sk-p" not in masked
        assert "zz" not in masked
        assert masked != _LLM_KEY

    def test_a_short_secret_is_still_hidden(self):
        assert cli._mask("POSTGRES_PASSWORD", "hunter2") == "***"

    def test_an_unset_secret_stays_empty_so_callers_can_tell_it_is_unset(self):
        assert cli._mask("OPENAI_API_KEY", "") == ""

    def test_a_non_secret_is_shown_as_is(self):
        assert cli._mask("PROMETHEUS_URL", "http://prom:9090") == "http://prom:9090"


@pytest.fixture
def board(tmp_path, monkeypatch, capsys):
    """`cmd_status` over a green board with all three role keys set."""
    user_env = tmp_path / "user.env"
    user_env.write_text("USE_SQLITE=true\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_CONFIG_FILE", user_env)
    monkeypatch.chdir(tmp_path)
    for var in ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "DATABASE_URL",
                "PROMETHEUS_URL", "LOKI_URL", "GRAFANA_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", _LLM_KEY)
    monkeypatch.setenv("USE_SQLITE", "true")
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    monkeypatch.setenv("KUBEINTELLECT_ADMIN_KEYS", _ADMIN)
    monkeypatch.setenv("KUBEINTELLECT_OPERATOR_KEYS", _OPERATOR)
    monkeypatch.setenv("KUBEINTELLECT_READONLY_KEYS", _READONLY)
    sqlite_path = tmp_path / "kubeintellect.db"
    sqlite_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("SQLITE_PATH", str(sqlite_path))
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    monkeypatch.setenv("KUBECONFIG_PATH", str(kubeconfig))
    monkeypatch.setattr(cli, "_cluster_reachable", lambda _p: True)
    monkeypatch.setattr(cli, "_get_kube_context", lambda _p: "test-ctx")
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "/usr/bin/x\n", ""),
    )
    monkeypatch.setattr(cli, "_http_ok", lambda _url, timeout=3.0: True)

    def _run() -> str:
        capsys.readouterr()
        with contextlib.suppress(SystemExit):
            cli.cmd_status(None)
        return _ANSI.sub("", capsys.readouterr().out)

    return _run


class TestStatusDoesNotPrintTheKeys:
    @pytest.mark.parametrize("secret", [_ADMIN, _OPERATOR, _READONLY])
    def test_no_role_key_appears_in_full(self, board, secret):
        assert secret not in board()

    def test_nor_any_piece_of_one(self, board):
        """Only the role rows are searched: the rest of the board prints temp paths whose names
        can contain any four characters."""
        rows = [ln for ln in board().splitlines() if re.match(r"\s+(admin|operator|readonly)\s", ln)]
        assert len(rows) == 3
        for secret in (_ADMIN, _OPERATOR, _READONLY):
            random_part = secret.rsplit("-", 1)[1]
            for row in rows:
                for i in range(len(random_part) - 3):
                    assert random_part[i:i + 4] not in row, (secret, row)

    def test_it_still_says_that_auth_is_on_and_where_the_keys_live(self, board):
        out = board()
        assert "enabled" in out
        assert str(cli._CONFIG_FILE) in out


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    """Run `cmd_init` against an isolated HOME and cwd, answering a scripted list."""
    def run(answers: list[str]) -> str:
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
        monkeypatch.setattr(cli, "_ensure_database", lambda: None)
        pending = iter(answers)
        monkeypatch.setattr(builtins, "input", lambda prompt="": next(pending))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
            cli.cmd_init(argparse.Namespace())
        return _ANSI.sub("", buf.getvalue())

    return run


# `1` = OpenAI · the key · `n` = no Kind cluster · `n` = do not start the server
COMPLETE = ["1", _LLM_KEY, "n", "n"]
INCOMPLETE = ["1", "", "n", "n"]
# Re-run: keep the provider, keep the stored LLM key (Enter), no Kind, do not start.
KEEP_EVERYTHING = ["", "", "n", "n"]


def _written_admin_key(tmp_path) -> str:
    text = (tmp_path / "home" / ".kubeintellect" / ".env").read_text(encoding="utf-8")
    return re.search(r"^KUBEINTELLECT_ADMIN_KEYS=(\S+)$", text, re.M).group(1)


class TestInitShowsAGeneratedKeyExactlyOnce:
    def test_a_completed_setup_prints_the_new_key_once(self, wizard, tmp_path):
        out = wizard(COMPLETE)
        key = _written_admin_key(tmp_path)
        assert "Setup complete" in out
        assert out.count(key) == 1

    def test_an_incomplete_setup_prints_the_new_key_once(self, wizard, tmp_path):
        out = wizard(INCOMPLETE)
        key = _written_admin_key(tmp_path)
        assert "Setup INCOMPLETE" in out
        assert out.count(key) == 1

    def test_the_key_is_still_written_where_kq_reads_it(self, wizard, tmp_path):
        wizard(COMPLETE)
        key = _written_admin_key(tmp_path)
        kq_env = (tmp_path / "home" / ".kube-q" / ".env").read_text(encoding="utf-8")
        assert f"KUBE_Q_API_KEY={key}" in kq_env

    def test_the_llm_key_typed_by_the_user_is_never_echoed_back(self, wizard):
        assert _LLM_KEY not in wizard(COMPLETE).split("Existing configuration", 1)[-1]


class TestInitDoesNotReprintAKeyItDidNotCreate:
    def test_a_rerun_over_an_existing_key_prints_it_zero_times(self, wizard, tmp_path):
        wizard(COMPLETE)
        key = _written_admin_key(tmp_path)
        out = wizard(KEEP_EVERYTHING)
        assert "Setup complete" in out
        assert key not in out
        assert _LLM_KEY not in out
        assert "unchanged" in out
