"""The "your setting did nothing" report could not say it about eleven of its own entries.

`UNWIRED_EXPERIMENTAL_FLAGS` (`app/core/version.py`) is the list of v5 settings that are declared,
documented, and read by no code. Its own comment states the contract:

    `active_experimental_flags()` excludes them, and `set_but_unwired_flags()` reports them
    separately — the operator still learns their setting did nothing, which is the part they
    actually need to know.

and `docs/v5-experimental-flags.md`, a public page in the docs-site nav, repeats it for every ⚠️
row: *"Setting one of these does not put it in `experimental_flags`; it appears instead under
`set_but_unwired_flags`."*

`set_but_unwired_flags()` was `_on_booleans() & UNWIRED_EXPERIMENTAL_FLAGS`, and `_on_booleans()`
filters `isinstance(value, bool)`. **11 of the 26 entries are `float` or `int`**, so no value an
operator could give them would ever reach that report. Measured 2026-08-20 — three knobs moved off
their defaults, alongside one boolean as a control:

    KI_V5_RIGHTSIZING=true            -> reported by /healthz, /v1/v5/status and version_line()
    KI_V5_AGENT_COST_RATE_CAP=0.10    -> reported by nothing, anywhere
    KI_V5_SPEND_OUT_PRICE_PER_1K=0.99 -> reported by nothing, anywhere
    KI_V5_DETECTOR_MIN_FIRINGS=3      -> reported by nothing, anywhere

The cost cap is the one that matters: it reads as a spend brake, the public page describes it as
*"USD/min above this ⇒ runaway spend"*, and it was the quietest of the eleven. This is the same
shape as the pass-108 defect one module over — the surface built to catch silent no-ops had a
silent no-op of its own — except here the promise was also written down for users to rely on.

Truthiness is not the fix. `KI_V5_AGENT_COST_RATE_CAP=0` is a deliberate setting and
`KI_V5_STAGE_SIZE=1` is already the default, so `_set_knobs()` compares against the **declared
default**, which is the only thing that separates *the operator asked for this* from *nobody
touched it*.

`active_experimental_flags()` is deliberately unchanged: its docstring's rationale — a knob is
configuration, not on/off runtime identity — is sound, and it is a different question from
"did what I set do anything".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.core.config import Settings, settings
from app.core.version import (
    _EXPERIMENTAL_PREFIXES,
    UNWIRED_EXPERIMENTAL_FLAGS,
    active_experimental_flags,
    set_but_unwired_flags,
    version_info,
    version_line,
)

_DOC = Path(__file__).resolve().parents[1] / "docs" / "v5-experimental-flags.md"

#: Non-vacuity floors (the pass-102 lesson): a partition that stops partitioning must fail, not
#: pass on an empty set. The unwired list may only shrink, so these are floors, not equalities.
_MIN_UNWIRED_KNOBS = 8
_MIN_UNWIRED_SWITCHES = 10
_MIN_WIRED_KNOBS = 10


def _declared_type(name: str) -> type:
    return type(settings.model_dump()[name])


def _partition() -> tuple[list[str], list[str]]:
    """The unwired list, split into knobs (non-boolean) and switches (boolean)."""
    dumped = settings.model_dump()
    knobs = sorted(f for f in UNWIRED_EXPERIMENTAL_FLAGS if not isinstance(dumped[f], bool))
    switches = sorted(f for f in UNWIRED_EXPERIMENTAL_FLAGS if isinstance(dumped[f], bool))
    return knobs, switches


def _wired_knobs() -> list[str]:
    """Experimental non-boolean settings that code *does* read — the negative control."""
    dumped = settings.model_dump()
    return sorted(
        n for n, v in dumped.items()
        if n.startswith(_EXPERIMENTAL_PREFIXES)
        and not isinstance(v, bool)
        and n not in UNWIRED_EXPERIMENTAL_FLAGS
    )


UNWIRED_KNOBS, UNWIRED_SWITCHES = _partition()
WIRED_KNOBS = _wired_knobs()


def _moved(name: str):
    """A value for `name` that is definitely not its declared default."""
    default = Settings.model_fields[name].default
    if isinstance(default, bool):
        return not default
    if isinstance(default, (int, float)):
        return type(default)(default) + type(default)(7)
    return f"{default}-moved"


@pytest.fixture
def set_flag(mocker):
    def _set(name: str, value) -> None:
        mocker.patch.object(settings, name, value)
    return _set


class TestThePartitionThisIsAboutIsReal:
    """If the unwired list ever loses its knobs, every case below would pass on nothing."""

    def test_the_unwired_list_holds_both_kinds(self):
        assert len(UNWIRED_KNOBS) >= _MIN_UNWIRED_KNOBS, (
            f"only {len(UNWIRED_KNOBS)} non-boolean entries in UNWIRED_EXPERIMENTAL_FLAGS — the "
            "partition this suite is about no longer exists"
        )
        assert len(UNWIRED_SWITCHES) >= _MIN_UNWIRED_SWITCHES

    def test_there_are_wired_knobs_to_contrast_against(self):
        assert len(WIRED_KNOBS) >= _MIN_WIRED_KNOBS

    def test_every_unwired_entry_is_a_real_setting(self):
        unknown = sorted(UNWIRED_EXPERIMENTAL_FLAGS - set(Settings.model_fields))
        assert unknown == [], f"the unwired list names settings that do not exist: {unknown}"


class TestAKnobTheOperatorMovedIsReported:

    @pytest.mark.parametrize("name", UNWIRED_KNOBS)
    def test_moving_it_off_the_default_surfaces_it(self, set_flag, name):
        set_flag(name, _moved(name))
        assert name in set_but_unwired_flags(), (
            f"{name} is declared, documented, read by no code, and was changed by the operator — "
            "and the report that exists to say exactly that omitted it"
        )

    @pytest.mark.parametrize("name", UNWIRED_KNOBS)
    def test_leaving_it_at_the_default_says_nothing(self, set_flag, name):
        set_flag(name, Settings.model_fields[name].default)
        assert name not in set_but_unwired_flags(), (
            "a setting nobody touched is not a setting that did nothing — reporting it would "
            "bury the ones that matter"
        )

    @pytest.mark.parametrize("name", UNWIRED_KNOBS)
    def test_it_reaches_the_operator_facing_surfaces(self, set_flag, name):
        set_flag(name, _moved(name))
        assert name in version_info()["set_but_unwired_flags"]
        assert name in version_line()
        assert "set but NOT WIRED" in version_line()

    def test_the_specific_measurement_this_was_found_by(self, set_flag):
        """A dead cost cap, a dead price and a dead threshold, next to a boolean that worked."""
        set_flag("KI_V5_AGENT_COST_RATE_CAP", 0.10)
        set_flag("KI_V5_SPEND_OUT_PRICE_PER_1K", 0.99)
        set_flag("KI_V5_DETECTOR_MIN_FIRINGS", 3)
        set_flag("KI_V5_RIGHTSIZING", True)
        assert set_but_unwired_flags() == [
            "KI_V5_AGENT_COST_RATE_CAP",
            "KI_V5_DETECTOR_MIN_FIRINGS",
            "KI_V5_RIGHTSIZING",
            "KI_V5_SPEND_OUT_PRICE_PER_1K",
        ]


class TestFalsyIsNotUnset:
    """The trap that makes truthiness the wrong test for a knob."""

    @pytest.mark.parametrize("name,value", [
        ("KI_V5_AGENT_COST_RATE_CAP", 0.0),
        ("KI_V5_DETECTOR_PRECISION_THETA", 0.0),
        ("KI_V5_DETECTOR_MIN_FIRINGS", 0),
    ])
    def test_zero_is_a_deliberate_setting_and_is_reported(self, set_flag, name, value):
        assert Settings.model_fields[name].default != value, "premise: zero is not the default here"
        set_flag(name, value)
        assert name in set_but_unwired_flags(), (
            f"{name}=0 is what an operator writes to mean 'allow nothing'; a report keyed on "
            "truthiness treats it as untouched"
        )


class TestASwitchStillBehavesAsBefore:

    @pytest.mark.parametrize("name", UNWIRED_SWITCHES)
    def test_on_is_reported(self, set_flag, name):
        set_flag(name, True)
        assert name in set_but_unwired_flags()

    @pytest.mark.parametrize("name", UNWIRED_SWITCHES)
    def test_off_is_not(self, set_flag, name):
        set_flag(name, False)
        assert name not in set_but_unwired_flags()

    @pytest.mark.parametrize("name", UNWIRED_SWITCHES)
    def test_an_unwired_switch_is_never_called_active(self, set_flag, name):
        set_flag(name, True)
        assert name not in active_experimental_flags()


class TestAWiredKnobIsNotReported:
    """The intersection is what makes this a report rather than a list of everything set."""

    @pytest.mark.parametrize("name", WIRED_KNOBS)
    def test_moving_a_wired_knob_says_nothing(self, set_flag, name):
        set_flag(name, _moved(name))
        assert name not in set_but_unwired_flags(), (
            f"{name} is read by real code — telling the operator it did nothing is the same "
            "false statement in the other direction"
        )


class TestKnobsStayOutOfTheIdentitySet:
    """`active_experimental_flags` answers a different question and must not drift into this one."""

    @pytest.mark.parametrize("name", UNWIRED_KNOBS[:3] + WIRED_KNOBS[:3])
    def test_a_knob_is_never_an_active_flag(self, set_flag, name):
        set_flag(name, _moved(name))
        assert name not in active_experimental_flags()

    def test_the_active_set_is_still_populated_by_switches(self, set_flag):
        set_flag("MEMORY_HYBRID_RETRIEVAL", True)
        assert "MEMORY_HYBRID_RETRIEVAL" in active_experimental_flags()


class TestThePublicPageNowTellsTheTruth:
    """The page states the contract to users; every row it marks ⚠️ must be reachable."""

    def test_every_flag_the_page_marks_unwired_can_actually_be_surfaced(self, set_flag):
        marked = set(re.findall(r"\|\s*`((?:KI_V5|CORTEX_V5)_[A-Z0-9_]+)`\s*⚠️", _DOC.read_text()))
        assert len(marked) >= 20, f"only {len(marked)} ⚠️ rows parsed from {_DOC.name}"
        unreachable = []
        for name in sorted(marked):
            if name not in Settings.model_fields:
                continue
            set_flag(name, _moved(name))
            if name not in set_but_unwired_flags():
                unreachable.append(name)
        assert unreachable == [], (
            "the page promises these appear under set_but_unwired_flags when set; they do not: "
            f"{unreachable}"
        )

    def test_the_page_still_makes_the_promise_this_gate_checks(self):
        assert "set_but_unwired_flags" in _DOC.read_text()


class TestSilentOnADefaultDeployment:

    def test_nothing_is_reported_when_nothing_was_changed(self):
        assert set_but_unwired_flags() == [], (
            "the shipped defaults report themselves as settings that did nothing"
        )

    def test_and_the_version_line_says_so_plainly(self):
        assert "set but NOT WIRED" not in version_line()
