"""
Unit tests for config validation and the `kq config` subcommand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kube_q.cli import config_cmd
from kube_q.core.config import Config, load_config, validate_config

# ── validate_config ──────────────────────────────────────────────────────────


def test_defaults_are_valid() -> None:
    assert validate_config(Config()) == []


def test_invalid_url_scheme_flagged() -> None:
    errors = validate_config(Config(url="ftp://bogus"))
    assert any("Invalid URL" in e for e in errors)


def test_invalid_url_missing_host_flagged() -> None:
    errors = validate_config(Config(url="http://"))
    assert any("Invalid URL" in e for e in errors)


def test_non_positive_timeout_flagged() -> None:
    errors = validate_config(Config(timeout=0))
    assert any("Invalid timeout" in e for e in errors)
    errors = validate_config(Config(timeout=-1.0))
    assert any("Invalid timeout" in e for e in errors)


def test_startup_retry_timeout_zero_is_allowed() -> None:
    errors = validate_config(Config(startup_retry_timeout=0))
    assert not any("startup_retry_timeout" in e for e in errors)


def test_invalid_output_flagged() -> None:
    errors = validate_config(Config(output="html"))
    assert any("Invalid output" in e for e in errors)


def test_invalid_log_level_flagged() -> None:
    errors = validate_config(Config(log_level="FOO"))
    assert any("Invalid log_level" in e for e in errors)


def test_log_level_case_insensitive() -> None:
    assert validate_config(Config(log_level="debug")) == []


def test_negative_cost_override_flagged() -> None:
    errors = validate_config(Config(cost_per_1k_prompt=-0.01))
    assert any("cost_per_1k_prompt" in e for e in errors)


def test_empty_display_name_flagged() -> None:
    errors = validate_config(Config(user_name="   "))
    assert any("Invalid user_name" in e for e in errors)


def test_error_messages_mention_env_var() -> None:
    errors = validate_config(Config(url="bogus"))
    assert any("KUBE_Q_URL" in e for e in errors)


def test_load_config_strict_exits_on_bad_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("kube_q.core.config.CONFIG_DIR", tmp_path)
    monkeypatch.setenv("KUBE_Q_TIMEOUT", "-5")
    with pytest.raises(SystemExit) as exc_info:
        load_config(strict=True)
    assert exc_info.value.code == 2


def test_load_config_non_strict_returns_invalid_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("kube_q.core.config.CONFIG_DIR", tmp_path)
    monkeypatch.setenv("KUBE_Q_TIMEOUT", "-5")
    cfg = load_config(strict=False)
    assert cfg.timeout == -5.0
    assert any("timeout" in e for e in validate_config(cfg))


# ── kq config subcommand ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    env_file = tmp_path / ".env"
    monkeypatch.setattr(config_cmd, "ENV_FILE", env_file)
    # Also redirect CONFIG_DIR so load_config() reads from the temp file.
    monkeypatch.setattr("kube_q.core.config.CONFIG_DIR", tmp_path)
    # Clear any KUBE_Q_* env vars set by the outer shell so tests are hermetic.
    for k in list(__import__("os").environ):
        if k.startswith("KUBE_Q_"):
            monkeypatch.delenv(k, raising=False)
    return env_file


def test_normalize_key_accepts_env_var() -> None:
    assert config_cmd._normalize_key("KUBE_Q_URL") == "KUBE_Q_URL"


def test_normalize_key_accepts_field_alias() -> None:
    assert config_cmd._normalize_key("url") == "KUBE_Q_URL"
    assert config_cmd._normalize_key("URL") == "KUBE_Q_URL"


def test_normalize_key_rejects_unknown() -> None:
    assert config_cmd._normalize_key("foo") is None


def test_cmd_set_writes_to_env_file(_isolated_env_file: Path) -> None:
    assert config_cmd.run(["set", "url=http://example:9000"]) == 0
    assert "KUBE_Q_URL=http://example:9000" in _isolated_env_file.read_text()


def test_cmd_set_refuses_invalid_value(_isolated_env_file: Path) -> None:
    rc = config_cmd.run(["set", "KUBE_Q_TIMEOUT=-10"])
    assert rc == 2
    assert not _isolated_env_file.exists()


def test_cmd_set_refuses_unknown_key(_isolated_env_file: Path) -> None:
    rc = config_cmd.run(["set", "KUBE_Q_BOGUS=x"])
    assert rc == 2


def test_cmd_set_replaces_existing_value(_isolated_env_file: Path) -> None:
    _isolated_env_file.write_text("KUBE_Q_URL=http://old:1\nKUBE_Q_MODEL=x\n")
    config_cmd.run(["set", "url=http://new:2"])
    lines = _isolated_env_file.read_text().splitlines()
    assert "KUBE_Q_URL=http://new:2" in lines
    assert "KUBE_Q_URL=http://old:1" not in lines
    assert "KUBE_Q_MODEL=x" in lines


def test_cmd_reset_removes_key(_isolated_env_file: Path) -> None:
    _isolated_env_file.write_text("KUBE_Q_URL=http://x:1\nKUBE_Q_MODEL=y\n")
    config_cmd.run(["reset", "url"])
    text = _isolated_env_file.read_text()
    assert "KUBE_Q_URL" not in text
    assert "KUBE_Q_MODEL=y" in text


def test_cmd_reset_no_key_deletes_file(_isolated_env_file: Path) -> None:
    _isolated_env_file.write_text("KUBE_Q_URL=http://x:1\n")
    config_cmd.run(["reset"])
    assert not _isolated_env_file.exists()


def test_cmd_reset_missing_key_is_noop(_isolated_env_file: Path) -> None:
    _isolated_env_file.write_text("KUBE_Q_URL=http://x:1\n")
    rc = config_cmd.run(["reset", "model"])
    assert rc == 0
    # URL still present
    assert "KUBE_Q_URL=http://x:1" in _isolated_env_file.read_text()


def test_cmd_show_runs(_isolated_env_file: Path) -> None:
    _isolated_env_file.write_text("KUBE_Q_URL=http://example:8080\n")
    assert config_cmd.run(["show"]) == 0


def test_help_flag() -> None:
    assert config_cmd.run(["--help"]) == 0
    assert config_cmd.run([]) == 0


def test_unknown_subcommand() -> None:
    assert config_cmd.run(["bogus"]) == 2
