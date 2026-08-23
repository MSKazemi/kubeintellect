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

from app.core.config import Settings, settings

# Experimental / feature-track flag prefixes (ADR-019): additive default-off toggles whose ON/OFF
# state is part of what distinguishes one v4.x build's *behavior* from another's.
#
# `CORTEX_V4` earns its place for the same reason the others do, and more sharply: it selects the
# tiered Cortex graph over the lean V2 coordinator FROM THE SAME IMAGE, so two arms of a comparison
# are byte-identical containers distinguished by this switch alone. Leaving it out meant the one
# flag that decides which system is under test was the one flag the reporting surface could not
# show — a campaign could compare V2 against V2 and every health check would agree it was fine.
_EXPERIMENTAL_PREFIXES = ("KI_V5_", "MEMORY_", "CORTEX_V4", "CORTEX_V5")

#: Flags that are DECLARED in config.py and DOCUMENTED, but read by no code (audited 2026-08-19).
#:
#: Reporting one of these as "active" is a false statement about the running system: the operator
#: set a switch, the product answered that a feature is on, and nothing whatsoever changed. That is
#: worse than ignoring the setting silently, because it is the reporting surface an operator uses to
#: confirm a rollout. So `active_experimental_flags()` excludes them, and `set_but_unwired_flags()`
#: reports them separately — the operator still learns their setting did nothing, which is the part
#: they actually need to know.
#:
#: ⛔ This set may only SHRINK. It is the production copy of the list `tests/test_v5_flag_wiring.py`
#: verifies against real `settings.<FLAG>` consumption, so wiring a flag and forgetting to delete it
#: here fails the suite. All 10 `MEMORY_*` booleans are wired; the rot is confined to the v5 track,
#: where the configuration surface was written ahead of the implementation.
UNWIRED_EXPERIMENTAL_FLAGS = frozenset({
    "KI_V5_ACI_MUTATING_VERBS",
    # Unwired since 2026-08-20 and deliberately so. Its only consumer was `auto_write_permitted`,
    # where it short-circuited to "allow" *before* the kill switch and change freeze — so setting
    # it did not add a gate, it was the thing that disabled one. The brakes are now unconditional.
    # The spend cap it was meant to govern still has no usage source, and `gate_write` takes the
    # figure from its caller, so there is nothing left for this flag to switch. Retire-or-wire is
    # an owner call; inventing a consumer to silence this list would be the same error again.
    "KI_V5_BLAST_RADIUS_BUDGET",
    "KI_V5_ACI_READ_VERBS_ENABLED",
    "KI_V5_AGENTIC_WORKLOAD_DETECTOR",
    "KI_V5_AGENT_COST_RATE_CAP",
    "KI_V5_AGENT_TOOL_RATE_CAP",
    "KI_V5_AIRGAP_FLOOR",
    "KI_V5_CAPABILITY_SANDBOX",
    "KI_V5_DETECTOR_MIN_FIRINGS",
    "KI_V5_DETECTOR_PRECISION_THETA",
    "KI_V5_FAILURE_DOMAIN_BUDGET",
    "KI_V5_FLEET_EXCHANGE",
    "KI_V5_FLEET_PATTERN_MIN_CLUSTERS",
    "KI_V5_FLEET_SIGNAL_POOLING",
    "KI_V5_GPU_HEALTH_DETECTOR",
    "KI_V5_MAX_UNAVAILABLE_PER_ZONE",
    "KI_V5_MODEL_ROUTING",
    "KI_V5_NL_DETECTOR_LADDER",
    "KI_V5_OFFLINE_SHADOW_WEIGHT",
    "KI_V5_RIGHTSIZING",
    "KI_V5_SPEND_IN_PRICE_PER_1K",
    "KI_V5_SPEND_OUT_PRICE_PER_1K",
    "KI_V5_STAGED_PROPAGATION",
    "KI_V5_STAGE_SIZE",
    "KI_V5_STAGE_WINDOW_SECONDS",
    "KI_V5_STATISTICAL_PROMOTION",
})


def code_version() -> str:
    """The authoritative software version (from the installed package = pyproject `version`).

    In the container there is no installed package to read: the image installs dependencies only
    and copies the module trees in flat, so this falls through to the build stamp the image was
    labelled with. Without that fallback every published release reported ``0+unknown`` on
    ``/healthz`` -- the one field that identifies which release is running. An installed package
    always wins; the stamp is the degraded answer, never an override.
    """
    try:
        return _pkg_version("kubeintellect")
    except PackageNotFoundError:  # running from source without an install (e.g. the image)
        # `git describe` emits the tag verbatim, and the tags are `vX.Y.Z`; the leading `v` is
        # tag syntax, not part of the version.
        stamp = settings.KI_BUILD_VERSION.strip().removeprefix("v")
        return stamp or "0+unknown"


def arm() -> str:
    """The architecture generation / paper-arm label (coarse; not a SemVer)."""
    return settings.KI_VERSION


def active_experimental_flags() -> list[str]:
    """Sorted names of the experimental boolean toggles that are currently ON.

    Only booleans set to True are reported — the tuning knobs (ints/floats/strings like
    KI_V5_HEARTBEAT_SECONDS) are configuration, not on/off identity. Flags in
    ``UNWIRED_EXPERIMENTAL_FLAGS`` are excluded: they change no behaviour, so calling them
    active would misreport the runtime. See ``set_but_unwired_flags()``.
    """
    return sorted(_on_booleans() - UNWIRED_EXPERIMENTAL_FLAGS)


def _on_booleans() -> set[str]:
    """Every experimental boolean currently True — wired or not."""
    dumped = settings.model_dump()
    return {
        name for name, value in dumped.items()
        if isinstance(value, bool) and value and name.startswith(_EXPERIMENTAL_PREFIXES)
    }


def _set_knobs() -> set[str]:
    """Every experimental *non-boolean* knob the operator moved off its declared default.

    Truthiness is the wrong test for a knob. ``KI_V5_AGENT_COST_RATE_CAP=0`` is a deliberate
    setting, not an absent one, and ``KI_V5_STAGE_SIZE=1`` is already the default. The declared
    default is the only thing that separates *the operator asked for this* from *nobody touched it*.
    """
    fields = Settings.model_fields
    knobs: set[str] = set()
    for name, value in settings.model_dump().items():
        if isinstance(value, bool) or not name.startswith(_EXPERIMENTAL_PREFIXES):
            continue
        field = fields.get(name)
        if field is None or field.is_required():
            continue                      # no declared default to compare against
        if value != field.default:
            knobs.add(name)
    return knobs


def set_but_unwired_flags() -> list[str]:
    """Settings the operator changed that no code reads — i.e. settings that did nothing.

    Reported alongside the active set rather than hidden: a silently-ignored switch is how an
    operator ends up believing a slice is live during a rollout.

    Covers **knobs as well as switches**. Until 2026-08-20 it read only booleans, so 11 of the 26
    names in :data:`UNWIRED_EXPERIMENTAL_FLAGS` — every ``float`` and ``int`` in it, including
    ``KI_V5_AGENT_COST_RATE_CAP`` and ``KI_V5_SPEND_OUT_PRICE_PER_1K`` — could not be emitted by
    this function at any value, while `docs/v5-experimental-flags.md` told the operator in as many
    words that setting one would surface it here. Measured 2026-08-20 with
    ``KI_V5_AGENT_COST_RATE_CAP=0.10``, ``KI_V5_SPEND_OUT_PRICE_PER_1K=0.99`` and
    ``KI_V5_DETECTOR_MIN_FIRINGS=3`` alongside ``KI_V5_RIGHTSIZING=true``: the boolean was
    reported by ``/healthz`` and the startup line, the three knobs by nothing anywhere.

    A dead cost cap is the one that matters. It reads as a spend brake, it is named as one on a
    public page, and it was the quietest of the eleven.
    """
    return sorted((_on_booleans() | _set_knobs()) & UNWIRED_EXPERIMENTAL_FLAGS)


def version_info() -> dict[str, object]:
    """Full runtime identity: arm + semver + active experimental flags."""
    return {
        "arm": arm(),
        "semver": code_version(),
        "experimental_flags": active_experimental_flags(),
        "set_but_unwired_flags": set_but_unwired_flags(),
    }


def version_line() -> str:
    """One-line human/log form, e.g. 'KubeIntellect v4 (2.1.0) [flags: CORTEX_V5_ENABLED,…]'."""
    flags = active_experimental_flags()
    suffix = f" [flags: {', '.join(flags)}]" if flags else " [flags: none — v4 baseline]"
    dead = set_but_unwired_flags()
    if dead:
        suffix += f" [set but NOT WIRED, no effect: {', '.join(dead)}]"
    return f"KubeIntellect {arm()} ({code_version()}){suffix}"
