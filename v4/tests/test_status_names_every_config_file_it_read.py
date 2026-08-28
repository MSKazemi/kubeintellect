"""`kubeintellect status` must name every `.env` it read, not only the user one.

`_load_effective_config` reads **two** files — `~/.kubeintellect/.env` and a `./.env` in the
working directory — and until 2026-08-28 the Config row named only the first. Run from a
directory that happens to hold a `.env` and every row underneath described a configuration
assembled partly from a file the board never mentioned; a checkout that followed the
documented `cp .env.example .env` printed *"Config ✗ … run: kubeintellect init"* directly
above rows filled in from the file sitting next to it.

The reason it has to be *named* rather than quietly merged is that the two loaders disagree
about which file wins, measured on one key present in both:

    kubeintellect serve  ->  the HOME value  (the CLI exports it first, and a real environment
                             variable outranks any .env)
    uvicorn / container / Helm chart
                         ->  the WORKING-DIRECTORY value (Settings lists ./.env last, and
                             pydantic-settings gives the last file priority)

Which of those *should* win is an open product decision. Whether a caller who has both files
is told so is not.
"""

from __future__ import annotations

import pytest

from app import cli


@pytest.fixture
def status_out(tmp_path, monkeypatch, capsys):
    """Run `cmd_status` with the two config paths under the test's control."""
    home_env = tmp_path / "home.env"
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setattr(cli, "_CONFIG_FILE", home_env)
    monkeypatch.chdir(workdir)
    # The rows below Config make network calls; none of them are under test here.
    monkeypatch.setattr(cli, "_http_ok", lambda _url, timeout=3.0: False)
    monkeypatch.setattr(cli, "_cluster_reachable", lambda _p: False)

    def _run() -> str:
        capsys.readouterr()
        try:
            cli.cmd_status(None)
        except SystemExit:
            pass
        return capsys.readouterr().out

    return _run, home_env, workdir


class TestTheConfigRowNamesWhatWasActuallyRead:
    def test_a_working_directory_env_is_named(self, status_out):
        run, home_env, workdir = status_out
        home_env.write_text("USE_SQLITE=true\n", encoding="utf-8")
        (workdir / ".env").write_text("USE_SQLITE=true\n", encoding="utf-8")
        out = run()
        assert str(home_env) in out
        assert str((workdir / ".env").resolve()) in out
        assert "(working directory)" in out

    def test_a_key_set_differently_in_both_is_called_out(self, status_out):
        run, home_env, workdir = status_out
        home_env.write_text("LLM_PROVIDER=azure\n", encoding="utf-8")
        (workdir / ".env").write_text("LLM_PROVIDER=openai\n", encoding="utf-8")
        out = run()
        assert "both files set LLM_PROVIDER to different values" in out

    def test_matching_values_are_not_called_out(self, status_out):
        """Vacuity guard: the warning must be about a *conflict*, not about having two files."""
        run, home_env, workdir = status_out
        home_env.write_text("LLM_PROVIDER=openai\n", encoding="utf-8")
        (workdir / ".env").write_text("LLM_PROVIDER=openai\n", encoding="utf-8")
        out = run()
        assert "different values" not in out

    def test_a_working_directory_env_alone_is_not_reported_as_missing_config(self, status_out):
        """A container, a Compose stack and a fresh `cp .env.example .env` all have config and
        no user file. Calling that ✗ called a configured install broken."""
        run, home_env, workdir = status_out
        (workdir / ".env").write_text("USE_SQLITE=true\n", encoding="utf-8")
        out = run()
        config_row = next(line for line in out.splitlines() if "Config:" in line)
        assert "✗" not in config_row, config_row
        assert str((workdir / ".env").resolve()) in config_row

    def test_neither_file_still_points_at_init(self, status_out):
        run, _home_env, _workdir = status_out
        out = run()
        config_row = next(line for line in out.splitlines() if "Config:" in line)
        assert "✗" in config_row
        assert "kubeintellect init" in out
