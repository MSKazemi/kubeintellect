"""`kubeintellect init` must not write a Prometheus it has never seen — and must not throw
away the one it *did* detect.

Two bugs in the same three lines of the generated `.env`, both found on 2026-08-28 while
recording the installation demo, and neither of which looks broken:

1. **The endpoint was invented.** The template called
   ``_line("PROMETHEUS_URL", "http://172.18.0.2:30090")``. `_line` treats its second argument
   as a *default value*, so `_v()` returned it, so `val` was truthy, so the line was written
   **active** — always, for everyone. `172.18.0.2` is the first address of Docker's default
   `kind` bridge. A user who declines the Kind cluster, or points at EKS, gets three red ✗ on
   their very first `kubeintellect status` for services they never installed. The comment
   directly above those lines said "Set automatically by 'kubeintellect init' when
   observability stack is installed", which is what the code was supposed to do and did not.

2. **The endpoint that was real got discarded.** `_setup_observability()` detects the node IP
   from the live cluster and appends `PROMETHEUS_URL=…` to the config file. But it runs *after*
   `existing` is loaded at the top of `cmd_init`, and the template rewrite at the end is built
   from `existing` — so the detected value was overwritten by the hardcoded guess. On a default
   `kind` bridge the guess is frequently 172.18.0.2 as well, which is exactly why this survived.

The two failure modes point opposite ways, so both directions are pinned here: nothing invented
when there is nothing, and nothing lost when there is something.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import io

import pytest

from app import cli

_CLEARED = (
    "LLM_PROVIDER", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
    "DATABASE_URL", "POSTGRES_HOST", "POSTGRES_PASSWORD", "KUBECONFIG_PATH",
    "KUBEINTELLECT_ADMIN_KEYS", "KUBEINTELLECT_OPERATOR_KEYS", "KUBEINTELLECT_READONLY_KEYS",
    "PROMETHEUS_URL", "LOKI_URL", "GRAFANA_URL", "LANGFUSE_HOST", "LANGFUSE_ENABLED",
)

# `1` = OpenAI · a usable key · `n` = no Kind cluster · `n` = no server
ANSWERS = ["1", "sk-proj-not-a-real-key", "n", "n"]


@pytest.fixture
def run_init(tmp_path, monkeypatch):
    """Run `cmd_init` against an isolated HOME and cwd, and hand back the written `.env`.

    cwd is isolated too: `cmd_init` reads `./.env` as a config source, so a run inside the
    repository would borrow the repository's own observability URLs and prove nothing.
    """
    def run(preexisting: str | None = None) -> str:
        home = tmp_path / "home"
        cwd = tmp_path / "cwd"
        home.mkdir(exist_ok=True)
        cwd.mkdir(exist_ok=True)
        config_dir = home / ".kubeintellect"
        config_file = config_dir / ".env"
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("HOME", str(home))
        for key in _CLEARED:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(cli, "_CONFIG_DIR", config_dir)
        monkeypatch.setattr(cli, "_CONFIG_FILE", config_file)
        monkeypatch.setattr(cli, "_ensure_tool", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_systemd_available", lambda: False)
        monkeypatch.setattr(cli, "_ensure_database", lambda: None)

        if preexisting is not None:
            config_dir.mkdir(parents=True, exist_ok=True)
            config_file.write_text(preexisting, encoding="utf-8")

        pending = iter(ANSWERS)
        monkeypatch.setattr(builtins, "input", lambda prompt="": next(pending))

        with contextlib.suppress(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_init(argparse.Namespace())
        return config_file.read_text(encoding="utf-8")

    return run


# `1` = OpenAI · key · `y` = create Kind · `y` = observability · `n` = RCA demos · `n` = no server
KIND_ANSWERS = ["1", "sk-proj-not-a-real-key", "y", "y", "n", "n"]


@pytest.fixture
def kind_run_init(tmp_path, monkeypatch):
    """Run `cmd_init` down the Kind branch, with `_setup_observability` standing in for a cluster.

    The stand-in does the one thing that matters here: it APPENDS the detected URL to the config
    file, mid-run, after `existing` was loaded. Everything else about installing a monitoring
    stack is irrelevant to whether that value survives.
    """
    def run(detected: str) -> str:
        home = tmp_path / "kindhome"
        cwd = tmp_path / "kindcwd"
        home.mkdir(exist_ok=True)
        cwd.mkdir(exist_ok=True)
        config_dir = home / ".kubeintellect"
        config_file = config_dir / ".env"
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("HOME", str(home))
        for key in _CLEARED:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(cli, "_CONFIG_DIR", config_dir)
        monkeypatch.setattr(cli, "_CONFIG_FILE", config_file)
        monkeypatch.setattr(cli, "_ensure_tool", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_systemd_available", lambda: False)
        monkeypatch.setattr(cli, "_ensure_database", lambda: None)
        monkeypatch.setattr(cli, "_setup_kind_with_samples", lambda: None)
        monkeypatch.setattr(cli, "_setup_demo_rca", lambda: None)

        def _observability() -> None:
            config_dir.mkdir(parents=True, exist_ok=True)
            with config_file.open("a", encoding="utf-8") as fh:
                fh.write(f"PROMETHEUS_URL={detected}\n")

        monkeypatch.setattr(cli, "_setup_observability", _observability)

        pending = iter(KIND_ANSWERS)
        monkeypatch.setattr(builtins, "input", lambda prompt="": next(pending))

        with contextlib.suppress(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_init(argparse.Namespace())
        return config_file.read_text(encoding="utf-8")

    return run


class TestNothingIsInvented:
    def test_a_fresh_install_declares_no_prometheus(self, run_init):
        env = run_init()
        for key in ("PROMETHEUS_URL", "LOKI_URL", "GRAFANA_URL"):
            active = [ln for ln in env.splitlines()
                      if ln.startswith(f"{key}=")]
            assert not active, f"{key} was written as configuration on a fresh install: {active}"

    def test_the_example_is_still_shown_but_commented(self, run_init):
        """The hint has to survive — a user with a stack needs to know the shape."""
        env = run_init()
        assert "# PROMETHEUS_URL=" in env
        assert "<kind-node-ip>" in env

    def test_the_hardcoded_bridge_address_is_gone(self, run_init):
        assert "172.18.0.2" not in run_init()


class TestNothingDetectedIsLost:
    """The real path, which a pre-written file does NOT reproduce.

    A config file that exists *before* `cmd_init` starts is picked up by the load at the top of
    the function, so pinning bug 2 that way passes with or without the fix — it proves nothing.
    The bug needs the value to appear in the file **during** the run, which is exactly what
    `_setup_observability()` does: it detects the node IP from the live cluster and appends.
    """

    def test_a_url_written_during_the_run_survives_the_template_rewrite(
        self, tmp_path, monkeypatch, kind_run_init
    ):
        env = kind_run_init(detected="http://10.42.0.7:30090")
        assert "PROMETHEUS_URL=http://10.42.0.7:30090" in env, (
            "the detected node IP was discarded by the template rewrite"
        )

    def test_a_url_already_in_the_config_file_also_survives(self, run_init):
        env = run_init(preexisting="PROMETHEUS_URL=http://10.42.0.7:30090\n")
        assert "PROMETHEUS_URL=http://10.42.0.7:30090" in env
        assert "# PROMETHEUS_URL=" not in env
