"""
theme.py — Centralized semantic colour palette for the kube-q CLI.

Colours are defined once here as a Rich ``Theme`` and applied to the shared
console in :mod:`kube_q.cli.renderer`. New code references *semantic role* names
(``[accent]``, ``[success]``, ``[plan.done]`` …) rather than raw colours, so the
palette can change in one place. Existing literal markup (``[cyan]`` etc.) keeps
working unchanged.

Accessibility: honours the ``NO_COLOR`` convention (https://no-color.org/) — when
that variable is set, the theme degrades every role to an unstyled passthrough.
"""

import os

from rich.theme import Theme

# ── Semantic roles ────────────────────────────────────────────────────────────
# role name → Rich style string. Keep this list small and meaningful; prefer
# adding a role over hard-coding a colour at a call site.
_ROLES: dict[str, str] = {
    # Brand / structure
    "accent": "cyan",
    "accent.bold": "bold cyan",
    "border": "dim cyan",
    "muted": "dim",
    # Speakers
    "user": "bold green",
    "agent": "bold cyan",
    # Status
    "success": "green",
    "warn": "yellow",
    "error": "bold red",
    # Side-channel activity
    "tool": "dim cyan",
    "plan.title": "bold cyan",
    "plan.done": "green",
    "plan.active": "yellow",
    "plan.pending": "dim",
    "plan.skipped": "dim",
    "plan.failed": "red",
    # The per-turn status footer
    "footer": "dim",
    "footer.key": "cyan",
}

# A neutral theme used when colour is disabled — every role maps to "none" so
# markup tags are still recognised (and stripped) instead of printed literally.
_NEUTRAL = Theme({role: "none" for role in _ROLES}, inherit=False)
_COLOURED = Theme(_ROLES)


def color_enabled() -> bool:
    """Return False when the user opted out of colour via ``NO_COLOR``."""
    return os.environ.get("NO_COLOR") is None


def get_theme() -> Theme:
    """Return the active Rich theme (coloured, or neutral if ``NO_COLOR`` set)."""
    return _COLOURED if color_enabled() else _NEUTRAL


# Plan step status → semantic glyph markup, used by the live plan renderer.
PLAN_ICONS: dict[str, str] = {
    "done": "[plan.done]✓[/plan.done]",
    "skipped": "[plan.skipped]—[/plan.skipped]",
    "failed": "[plan.failed]✗[/plan.failed]",
    "in_progress": "[plan.active]▸[/plan.active]",
    "pending": "[plan.pending]·[/plan.pending]",
}
