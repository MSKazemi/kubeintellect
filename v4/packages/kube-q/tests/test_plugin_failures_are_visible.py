"""A plugin that fails to import must be distinguishable from one that was never installed.

`load_plugins` promised its errors were "logged (and printed as a dim warning)". Only the first
half was true: the `kube_q` logger gets a stderr handler solely under `--debug`, so the warning
went to `~/.kube-q/kube-q.log` and nowhere else. Measured 2026-08-24 with `setup_logging()`
configured exactly as `kq` does at start-up, a plugins directory containing a broken file
produced **empty stdout and empty stderr**. The user drops a file into `~/.kube-q/plugins/`, sees
a start-up banner that does not mention it, types their command, and is told it is unknown.

The sharper half is the registry. `register` runs at import time, so a module that registers a
command and *then* raises left that command callable and listed in `/help` — while the same
screen's "Plugins loaded:" line said the module had not loaded. Two surfaces, one fact, opposite
answers.
"""

from __future__ import annotations

import inspect
import sys
import textwrap

import pytest

from kube_q import plugins


@pytest.fixture
def plugin_dir(tmp_path):
    """An isolated plugins directory, with the process-wide registry restored afterwards."""
    saved_registry = dict(plugins._REGISTRY)
    saved_modules = set(sys.modules)
    directory = tmp_path / "plugins"
    directory.mkdir()

    class Dir:
        path = directory

        @staticmethod
        def write(name: str, body: str) -> None:
            (directory / name).write_text(textwrap.dedent(body), encoding="utf-8")

    yield Dir()

    plugins._REGISTRY.clear()
    plugins._REGISTRY.update(saved_registry)
    plugins._FAILURES.clear()
    for module in set(sys.modules) - saved_modules:
        if module.startswith("kube_q_plugin_"):
            del sys.modules[module]


_GOOD = """
    from kube_q.plugins import register

    @register("/alpha", help="works")
    def alpha(ctx):
        ctx.print("alpha")
"""

_RAISES_AFTER_REGISTERING = """
    from kube_q.plugins import register

    @register("/beta", help="registered, then the module blew up")
    def beta(ctx):
        ctx.print("beta")

    raise RuntimeError("the rest of this module never ran")
"""

_IMPORT_ERROR = """
    import kube_q_no_such_dependency_4b21
"""


class TestAFailedPluginIsReported:
    def test_a_working_plugin_still_loads(self, plugin_dir):
        """Vacuity guard: if nothing loaded, every assertion below would pass for free."""
        plugin_dir.write("alpha.py", _GOOD)
        assert plugins.load_plugins(plugin_dir.path) == ["alpha"]
        assert plugins.load_failures() == []
        assert "/alpha" in plugins.registered_commands()

    def test_a_broken_plugin_is_named_with_its_reason(self, plugin_dir):
        plugin_dir.write("broken.py", _IMPORT_ERROR)
        plugins.load_plugins(plugin_dir.path)
        failures = plugins.load_failures()
        assert [name for name, _ in failures] == ["broken.py"]
        assert "kube_q_no_such_dependency_4b21" in failures[0][1]
        assert "ModuleNotFoundError" in failures[0][1]

    def test_one_broken_plugin_does_not_stop_the_others(self, plugin_dir):
        plugin_dir.write("aaa_broken.py", _IMPORT_ERROR)   # sorts first
        plugin_dir.write("alpha.py", _GOOD)
        assert plugins.load_plugins(plugin_dir.path) == ["alpha"]
        assert len(plugins.load_failures()) == 1

    def test_the_failure_is_still_logged(self, plugin_dir, caplog):
        """The log line was the only report before; it stays, it is just no longer the only one."""
        plugin_dir.write("broken.py", _IMPORT_ERROR)
        with caplog.at_level("WARNING", logger="kube_q.plugins"):
            plugins.load_plugins(plugin_dir.path)
        messages = [r.getMessage() for r in caplog.records]
        assert any("failed to load plugin" in m for m in messages), messages

    def test_failures_do_not_accumulate_across_calls(self, plugin_dir):
        plugin_dir.write("broken.py", _IMPORT_ERROR)
        plugins.load_plugins(plugin_dir.path)
        assert len(plugins.load_failures()) == 1
        (plugin_dir.path / "broken.py").unlink()
        plugin_dir.write("alpha.py", _GOOD)
        plugins.load_plugins(plugin_dir.path)
        assert plugins.load_failures() == []


class TestAFailedModuleLeavesNothingBehind:
    def test_a_module_that_raises_after_registering_advertises_no_command(self, plugin_dir):
        """`/help` is built from the registry; it must not list a command whose module failed."""
        plugin_dir.write("beta.py", _RAISES_AFTER_REGISTERING)
        loaded = plugins.load_plugins(plugin_dir.path)
        assert loaded == []
        assert "/beta" not in plugins.registered_commands(), (
            "a half-executed module left its command callable — the banner says it did not load"
        )
        assert plugins.load_failures()[0][0] == "beta.py"

    def test_a_command_a_failing_module_overwrote_is_restored(self, plugin_dir):
        """Registering an existing name replaces the handler. A module that does that and then
        raises must not leave the replacement in place."""
        plugin_dir.write("alpha.py", _GOOD)
        plugins.load_plugins(plugin_dir.path)
        original = plugins.registered_commands()["/alpha"]

        (plugin_dir.path / "alpha.py").unlink()
        plugin_dir.write("zeta.py", """
            from kube_q.plugins import register

            @register("/alpha", help="hijacked")
            def hijack(ctx):
                ctx.print("hijacked")

            raise RuntimeError("and then it failed")
        """)
        plugins.load_plugins(plugin_dir.path)
        assert plugins.registered_commands()["/alpha"] == original

    def test_a_failed_module_is_not_left_importable(self, plugin_dir):
        plugin_dir.write("beta.py", _RAISES_AFTER_REGISTERING)
        plugins.load_plugins(plugin_dir.path)
        assert "kube_q_plugin_beta" not in sys.modules

    def test_a_clean_load_leaves_the_module_importable(self, plugin_dir):
        """Vacuity guard for the test above: the cleanup must be specific to failures."""
        plugin_dir.write("alpha.py", _GOOD)
        plugins.load_plugins(plugin_dir.path)
        assert "kube_q_plugin_alpha" in sys.modules

    def test_a_missing_directory_is_not_a_failure(self, tmp_path):
        assert plugins.load_plugins(tmp_path / "nope") == []


class TestTheReplShowsIt:
    def test_run_repl_reads_the_failures_and_prints_them(self):
        """Wiring check: the list is useless if the one caller never looks at it."""
        from kube_q.cli import repl

        source = inspect.getsource(repl.run_repl)
        assert "plugins.load_failures()" in source
        assert "did not load" in source


class TestThePluginsCommand:
    """`/plugins` exists to answer "what plugins do I have?" — it must not answer it wrongly.

    Reading only the registry made it print *"No plugins loaded. Drop Python files into
    ~/.kube-q/plugins/…"* to a user whose plugins directory held nothing but files that had all
    failed to import: it told them to do the thing they had already done.
    """

    @staticmethod
    def _render(capsys):
        from kube_q.cli.repl import _render_plugins

        _render_plugins()
        return capsys.readouterr().out

    def test_it_does_not_tell_you_to_install_what_you_already_installed(
        self, plugin_dir, capsys
    ):
        plugin_dir.write("broken.py", _IMPORT_ERROR)
        plugin_dir.write("beta.py", _RAISES_AFTER_REGISTERING)
        plugins._REGISTRY.clear()
        plugins.load_plugins(plugin_dir.path)

        out = self._render(capsys)
        assert "Drop Python files" not in out
        assert "failed to load" in out
        assert "broken.py" in out and "beta.py" in out
        assert "ModuleNotFoundError" in out and "RuntimeError" in out

    def test_an_empty_directory_still_gets_the_invitation(self, plugin_dir, capsys):
        """Vacuity guard: the helpful text must survive for the case it was written for."""
        plugins._REGISTRY.clear()
        plugins.load_plugins(plugin_dir.path)
        out = self._render(capsys)
        assert "Drop Python files" in out
        assert "✗" not in out

    def test_loaded_and_failed_are_both_reported(self, plugin_dir, capsys):
        plugin_dir.write("alpha.py", _GOOD)
        plugin_dir.write("broken.py", _IMPORT_ERROR)
        plugins._REGISTRY.clear()
        plugins.load_plugins(plugin_dir.path)

        out = self._render(capsys)
        assert "/alpha" in out, "a working plugin must still be listed"
        assert "broken.py" in out, "a failure must not be hidden by a success"

    def test_a_clean_load_reports_no_failures(self, plugin_dir, capsys):
        """Vacuity guard for the test above — the ✗ lines must be specific to failures."""
        plugin_dir.write("alpha.py", _GOOD)
        plugins._REGISTRY.clear()
        plugins.load_plugins(plugin_dir.path)
        out = self._render(capsys)
        assert "/alpha" in out
        assert "✗" not in out
