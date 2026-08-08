"""
plugins.py — Extensible slash-command registry.

Users can drop Python files into ``~/.kube-q/plugins/`` (or the directory named
by ``KUBE_Q_PLUGIN_DIR``) to add their own ``/commands``. Each file may call
:func:`register` any number of times::

    # ~/.kube-q/plugins/hello.py
    from kube_q.plugins import register

    @register("/hello", help="Say hello")
    def hello(ctx):
        ctx.print("hi there")

The handler receives a :class:`PluginContext` with:
  * ``args``: the rest of the user's line after the command (``str``)
  * ``state``: the current :class:`SessionState`
  * ``cfg``: the current :class:`ReplConfig`
  * ``print(text)``: convenience for writing to the Rich console
  * ``console``: the underlying Rich console (for advanced rendering)

Plugins run in-process and have full Python access — only install files you
trust. Import failures are logged and the REPL continues to start.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kube_q.core.config import CONFIG_DIR

_logger = logging.getLogger(__name__)

PLUGINS_DIR = Path(os.environ.get("KUBE_Q_PLUGIN_DIR") or (CONFIG_DIR / "plugins"))

# name → (handler, help_text)
_REGISTRY: dict[str, tuple[Callable[[PluginContext], None], str]] = {}


@dataclass
class PluginContext:
    """Everything a plugin command handler gets to see."""
    args: str
    state: Any                 # SessionState
    cfg: Any                   # ReplConfig
    console: Any               # rich.Console
    metadata: dict[str, Any] = field(default_factory=dict)

    def print(self, text: str) -> None:
        self.console.print(text)


def register(
    name: str,
    *,
    help: str = "",
) -> Callable[[Callable[[PluginContext], None]], Callable[[PluginContext], None]]:
    """Decorator to register a slash command handler.

    The command name must start with ``/`` and match ``[a-z0-9_-]+`` after the slash.
    Registering the same name twice replaces the earlier handler (useful during dev).
    """
    if not name.startswith("/") or not name[1:].replace("-", "").replace("_", "").isalnum():
        raise ValueError(
            f"Invalid plugin command name {name!r}: must start with '/' "
            "and contain only alphanumerics, '-', or '_'."
        )

    def _wrap(fn: Callable[[PluginContext], None]) -> Callable[[PluginContext], None]:
        _REGISTRY[name.lower()] = (fn, help or "")
        _logger.debug("plugin registered: %s", name)
        return fn

    return _wrap


def registered_commands() -> dict[str, tuple[Callable[[PluginContext], None], str]]:
    """Return a copy of the current plugin registry."""
    return dict(_REGISTRY)


def load_plugins(directory: Path | None = None) -> list[str]:
    """Import every ``*.py`` file in the plugins directory.

    Returns the list of successfully loaded plugin module names. Errors are
    logged (and printed as a dim warning) but never raise — a broken plugin
    should not prevent the REPL from starting.
    """
    target = directory or PLUGINS_DIR
    if not target.exists() or not target.is_dir():
        return []

    loaded: list[str] = []
    for path in sorted(target.glob("*.py")):
        if path.name.startswith("_"):
            continue
        mod_name = f"kube_q_plugin_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            loaded.append(path.stem)
        except Exception as exc:
            _logger.warning("failed to load plugin %s: %s", path, exc)
    return loaded


def dispatch(name: str, ctx: PluginContext) -> bool:
    """Invoke the registered handler for *name*. Returns True if handled."""
    entry = _REGISTRY.get(name.lower())
    if entry is None:
        return False
    handler, _help = entry
    try:
        handler(ctx)
    except Exception as exc:
        _logger.exception("plugin %s raised %s", name, exc)
        ctx.console.print(f"[red]✗ plugin {name} failed: {exc}[/red]")
    return True
