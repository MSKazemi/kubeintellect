"""Central registry of ``kq <command>`` subcommands.

Single source of truth for both **dispatch** (``main.py``) and **discovery**
(the ``Commands:`` section in ``kq --help`` and the ``kq help`` listing). Each
entry lazily imports its command module so ``kq`` startup stays fast — the module
is only loaded when its command actually runs.

To add a subcommand: implement ``def run(argv: list[str]) -> int`` in a module
under ``kube_q/cli/`` and add one line here. It then appears in ``kq --help``,
``kq help``, and is dispatched automatically — no edits to ``main.py`` needed.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from importlib import import_module

# name -> (module path, one-line description). Order is the display order.
_SUBCOMMANDS: dict[str, tuple[str, str]] = {
    "config": (
        "kube_q.cli.config_cmd",
        "Show, set, or reset config and manage profiles",
    ),
    "findings": (
        "kube_q.cli.findings_cmd",
        "List detector findings (zero-token detection feed)",
    ),
    "digest": (
        "kube_q.cli.digest_cmd",
        "Show what the watchtower did while you were away",
    ),
    "replay": (
        "kube_q.cli.replay_cmd",
        "Replay a recorded session from the flight recorder",
    ),
    "postmortem": (
        "kube_q.cli.postmortem_cmd",
        "Generate a grounded incident postmortem for a session",
    ),
    "detector": (
        "kube_q.cli.detector_cmd",
        "Author, list, or promote natural-language detectors",
    ),
    "preference": (
        "kube_q.cli.preference_cmd",
        "View or set remembered user preferences",
    ),
    "v5-status": (
        "kube_q.cli.v5_status_cmd",
        "Show v5 trust-plane status (flags, kill switch, spend cap)",
    ),
}

# Extra tokens that list the commands rather than dispatch one.
_HELP_ALIASES = ("help", "commands")


def names() -> list[str]:
    """Return the subcommand names in display order."""
    return list(_SUBCOMMANDS)


def describe() -> list[tuple[str, str]]:
    """Return ``(name, one-line description)`` pairs in display order."""
    return [(name, desc) for name, (_, desc) in _SUBCOMMANDS.items()]


def is_help_alias(token: str) -> bool:
    """Whether *token* (e.g. ``help``, ``commands``) should list the commands."""
    return token in _HELP_ALIASES


def get_runner(name: str) -> Callable[[list[str]], int] | None:
    """Return the ``run(argv)`` callable for *name*, or ``None`` if unknown.

    The command module is imported lazily on first use.
    """
    entry = _SUBCOMMANDS.get(name)
    if entry is None:
        return None
    return import_module(entry[0]).run


def suggest(name: str) -> list[str]:
    """Return up to 3 known commands close to a mistyped *name* (for "did you mean")."""
    return difflib.get_close_matches(name, list(_SUBCOMMANDS), n=3, cutoff=0.6)


def help_block(indent: str = "  ") -> str:
    """Return an aligned ``name  description`` block for help output."""
    width = max((len(name) for name in _SUBCOMMANDS), default=0)
    lines = [f"{indent}{name:<{width}}  {desc}" for name, desc in describe()]
    return "\n".join(lines)
