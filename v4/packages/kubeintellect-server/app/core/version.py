"""Version identity — the single place that answers "which version am I, and what's active?".

Three distinct axes, per ADR-019 (kept separate on purpose):

- ``arm``   — the architecture generation / paper arm (``KI_VERSION``, e.g. "v4"). Coarse; does
  NOT encode the minor. Changes only when a new architecture ships on by default (→ v5.0).
- ``semver`` — the software version from ``pyproject.toml`` (the ``kubeintellect`` package). THIS is
  what distinguishes v4 from "v4.1/v4.2": a minor bump per additive, default-off feature wave.
- ``flags`` — the experimental toggles currently ON. Because every v4.x slice is default-off, two
  builds at the same ``semver`` behave identically until flags flip — so the active-flag set is
  part of the runtime identity, not just the number.

``version_info()`` returns all three so a single ``/healthz`` (or log line) fully identifies a
running instance.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from app.core.config import settings

# Experimental / feature-track flag prefixes (ADR-019): additive default-off toggles whose ON/OFF
# state is part of what distinguishes one v4.x build's *behavior* from another's.
_EXPERIMENTAL_PREFIXES = ("KI_V5_", "MEMORY_", "CORTEX_V5")


def code_version() -> str:
    """The authoritative software version (from the installed package = pyproject `version`)."""
    try:
        return _pkg_version("kubeintellect")
    except PackageNotFoundError:  # running from source without an install
        return "0+unknown"


def arm() -> str:
    """The architecture generation / paper-arm label (coarse; not a SemVer)."""
    return settings.KI_VERSION


def active_experimental_flags() -> list[str]:
    """Sorted names of the experimental boolean toggles that are currently ON.

    Only booleans set to True are reported — the tuning knobs (ints/floats/strings like
    KI_V5_HEARTBEAT_SECONDS) are configuration, not on/off identity.
    """
    dumped = settings.model_dump()
    on = [
        name for name, value in dumped.items()
        if isinstance(value, bool) and value and name.startswith(_EXPERIMENTAL_PREFIXES)
    ]
    return sorted(on)


def version_info() -> dict[str, object]:
    """Full runtime identity: arm + semver + active experimental flags."""
    return {
        "arm": arm(),
        "semver": code_version(),
        "experimental_flags": active_experimental_flags(),
    }


def version_line() -> str:
    """One-line human/log form, e.g. 'KubeIntellect v4 (2.1.0) [flags: CORTEX_V5_ENABLED,…]'."""
    flags = active_experimental_flags()
    suffix = f" [flags: {', '.join(flags)}]" if flags else " [flags: none — v4 baseline]"
    return f"KubeIntellect {arm()} ({code_version()}){suffix}"
