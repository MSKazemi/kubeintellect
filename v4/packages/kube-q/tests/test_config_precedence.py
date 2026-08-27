"""`kq` config layering: every layer must be able to override the one below it.

`load_config` documents four layers — `~/.kube-q/.env`, then a profile, then `./.env`, then the
shell — but until 2026-08-24 the loader copied each file straight into `os.environ` and skipped
any key already present. The *first* file read therefore won, so a profile and a project-local
`./.env` were silent no-ops for every key `~/.kube-q/.env` had already set — which is exactly the
set of keys `kq config set` writes there (`url`, `api_key`, `context`). `kq config profile` is
sold as the way to point `kq` at another cluster; for a configured user it changed nothing.

`kq config show`'s "Source" column had the matching half of the bug: it derived the source by
comparing `os.environ` against `~/.kube-q/.env`, which cannot see a profile or a local `.env` at
all, and labelled both "shell env" — while `docs/cli-reference.md` promises the column names them.
"""

from __future__ import annotations

import pytest

from kube_q.cli import config_cmd
from kube_q.core import config as core_config

_USER = "http://user-level:8000"
_PROFILE = "http://profile:8000"
_LOCAL = "http://project-local:8000"
_SHELL = "http://shell:8000"


@pytest.fixture
def layers(tmp_path, monkeypatch):
    """A private ~/.kube-q and cwd, with a helper that writes any subset of the layers."""
    home = tmp_path / "kube-q"
    profiles = home / "profiles"
    profiles.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    monkeypatch.setattr(core_config, "CONFIG_DIR", home)
    monkeypatch.setattr(core_config, "PROFILES_DIR", profiles)
    monkeypatch.setattr(config_cmd, "CONFIG_DIR", home, raising=False)
    monkeypatch.setattr(config_cmd, "ENV_FILE", home / ".env")

    for key in [k for k in list(core_config.os.environ) if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)
    core_config._INJECTED.clear()
    core_config._VALUE_SOURCES.clear()

    class Layers:
        home_dir = home

        @staticmethod
        def write(user=None, profile=None, local=None, shell=None, profile_name="prod"):
            if user is not None:
                (home / ".env").write_text(f"KUBE_Q_URL={user}\n", encoding="utf-8")
            if profile is not None:
                (profiles / f"{profile_name}.env").write_text(
                    f"KUBE_Q_URL={profile}\n", encoding="utf-8")
                monkeypatch.setenv("KUBE_Q_PROFILE", profile_name)
            if local is not None:
                (project / ".env").write_text(f"KUBE_Q_URL={local}\n", encoding="utf-8")
            if shell is not None:
                monkeypatch.setenv("KUBE_Q_URL", shell)

    yield Layers()
    core_config._INJECTED.clear()
    core_config._VALUE_SOURCES.clear()


class TestTheLayersOverrideInTheDocumentedOrder:
    def test_the_user_file_alone_is_used(self, layers):
        """Vacuity guard: without it, a test suite where everything returns the local value
        would pass whether or not layering works."""
        layers.write(user=_USER)
        assert core_config.load_config(strict=False).url == _USER

    def test_a_profile_overrides_the_user_file(self, layers):
        layers.write(user=_USER, profile=_PROFILE)
        assert core_config.load_config(strict=False).url == _PROFILE

    def test_a_project_local_env_overrides_the_user_file(self, layers):
        layers.write(user=_USER, local=_LOCAL)
        assert core_config.load_config(strict=False).url == _LOCAL

    def test_a_project_local_env_overrides_a_profile(self, layers):
        layers.write(user=_USER, profile=_PROFILE, local=_LOCAL)
        assert core_config.load_config(strict=False).url == _LOCAL

    def test_a_shell_export_beats_every_file(self, layers):
        layers.write(user=_USER, profile=_PROFILE, local=_LOCAL, shell=_SHELL)
        assert core_config.load_config(strict=False).url == _SHELL

    def test_a_profile_named_in_the_user_file_is_still_honoured(self, layers):
        """The old loader could reach a profile named in ~/.kube-q/.env, because it pushed that
        file into os.environ first. Keep that working."""
        layers.home_dir.joinpath(".env").write_text(
            f"KUBE_Q_URL={_USER}\nKUBE_Q_PROFILE=prod\n", encoding="utf-8")
        layers.home_dir.joinpath("profiles", "prod.env").write_text(
            f"KUBE_Q_URL={_PROFILE}\n", encoding="utf-8")
        assert core_config.load_config(strict=False).url == _PROFILE


class TestLoadingTwiceInOneProcess:
    """File values still land in os.environ — `repl` reads it directly — so the loader has to
    remember which ones it put there or a second call mistakes them for shell exports."""

    def test_the_second_load_sees_an_edited_file(self, layers, tmp_path):
        layers.write(user=_USER, local=_LOCAL)
        assert core_config.load_config(strict=False).url == _LOCAL
        (tmp_path / "project" / ".env").write_text(f"KUBE_Q_URL={_PROFILE}\n", encoding="utf-8")
        assert core_config.load_config(strict=False).url == _PROFILE

    def test_a_key_removed_from_every_file_stops_being_returned(self, layers, tmp_path):
        layers.write(local=_LOCAL)
        assert core_config.load_config(strict=False).url == _LOCAL
        (tmp_path / "project" / ".env").write_text("", encoding="utf-8")
        core_config.load_config(strict=False)
        assert "KUBE_Q_URL" not in core_config.os.environ, (
            "a removed key survived in os.environ as a phantom shell export"
        )

    def test_file_values_still_reach_os_environ(self, layers):
        """`repl.py` reads KUBE_Q_API_KEY / URL / MODEL from os.environ directly."""
        layers.write(user=_USER)
        core_config.load_config(strict=False)
        assert core_config.os.environ.get("KUBE_Q_URL") == _USER


class TestTheSourceColumn:
    def test_it_names_the_profile(self, layers):
        layers.write(user=_USER, profile=_PROFILE)
        core_config.load_config(strict=False)
        assert config_cmd._value_source("KUBE_Q_URL") == "profile (prod)"

    def test_it_names_the_project_local_file(self, layers):
        layers.write(user=_USER, local=_LOCAL)
        core_config.load_config(strict=False)
        assert config_cmd._value_source("KUBE_Q_URL") == "file (./.env)"

    def test_it_names_the_user_file(self, layers):
        layers.write(user=_USER)
        core_config.load_config(strict=False)
        assert config_cmd._value_source("KUBE_Q_URL") == f"file ({layers.home_dir / '.env'})"

    def test_it_still_says_shell_env_for_a_real_export(self, layers):
        layers.write(user=_USER, shell=_SHELL)
        core_config.load_config(strict=False)
        assert config_cmd._value_source("KUBE_Q_URL") == "shell env"

    def test_an_unset_key_is_a_default(self, layers):
        layers.write(user=_USER)
        core_config.load_config(strict=False)
        assert config_cmd._value_source("KUBE_Q_MODEL") == "default"

    def test_config_show_runs_and_reports_the_profile(self, layers, capsys):
        layers.write(user=_USER, profile=_PROFILE)
        assert config_cmd.cmd_show() == 0
        assert "profile" in capsys.readouterr().out
