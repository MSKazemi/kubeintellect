"""An operator declared a change freeze; one of the two write gates never heard about it.

`KI_V5_CHANGE_FREEZE` is the second of the two brakes an operator sets to say *stop*. Both
brakes have two sources — a settings flag and a runtime/injected one — and both are consulted
by two gates: `auto_write_permitted` (the watchtower's A3 path) and `gate_write` (the ACI write
chokepoint, `aci/mutating.decide_write`).

The kill switch was given **one** reader, `kill_switch_engaged()`, that composes its two
sources, so the two gates cannot disagree about it. The change freeze was not: each gate read a
different source. Measured 2026-08-20 with `KI_V5_CHANGE_FREEZE=true` and nothing else set::

    auto_write_permitted()                      deny   "change freeze in effect"
    gate_write()                                ALLOW  ""                        <-- ⚠️
    decide_write("kubectl scale …", "L4")       auto   "earned L4 …"             <-- ⚠️
    decide_write("kubectl rollout restart …")   auto   "earned L4 …"             <-- ⚠️

    contrast, KI_V5_KILL_SWITCH=true:           deny / deny / deny  — the sibling brake works

`gate_write` checked only `now_epoch is not None and freeze_windows`, and its one caller
(`mutating.py:80`, `gate_write()`) passes neither, so the branch was unreachable from
production and the flag had no effect on that path at all. Its own docstring says *"Full
composable write gate … Precedence: governance → kill switch → change freeze → spend"* — true
of three brakes out of four for every real invocation.

**Scope, stated honestly.** `decide_write`/`plan_mutation` have no production caller yet — the
ACI mutating chokepoint is built and tested but not yet wired into the graph — so unlike the
kill-switch defect this one was latent rather than live. The live A3 path goes through
`auto_write_permitted`, which honoured the freeze throughout. What was live is the claim:
`GET /v1/v5/status` reports `change_freeze: true` and `kq v5-status` prints it, for a brake
that half the gate surface did not implement.

Both gates now read `change_freeze_active()`, the counterpart of `kill_switch_engaged()`.
"""
from __future__ import annotations

import itertools

import pytest
from app.autonomy.budget import (
    BudgetDecision,
    auto_write_permitted,
    change_freeze_active,
    disengage_kill_switch,
    engage_kill_switch,
    gate_write,
    in_change_freeze,
    kill_switch_engaged,
)
from app.core.config import settings
from app.tools.aci.mutating import decide_write

WINDOW = [(100.0, 200.0)]
INSIDE, OUTSIDE = 150.0, 250.0

# Commands the ACI chokepoint would auto-execute at L4 — every one of them a write.
AUTO_AT_L4 = [
    "kubectl scale deploy/web --replicas=3",
    "kubectl rollout restart deploy/api",
    "kubectl set image deploy/api api=nginx:1.27",
]


@pytest.fixture(autouse=True)
def _no_brakes(mocker):
    """Every test declares its own brakes; none inherits one."""
    disengage_kill_switch()
    mocker.patch.object(settings, "KI_V5_KILL_SWITCH", False)
    mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", False)
    yield
    disengage_kill_switch()


def _freeze(mocker, on: bool) -> None:
    mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", on)


class TestTheDeclaredFreezeReachesTheWriteChokepoint:
    """The defect itself: the flag an operator sets, on the gate that authorizes writes."""

    def test_gate_write_denies_on_a_declared_freeze(self, mocker):
        _freeze(mocker, True)
        d = gate_write()
        assert d.allow is False, "a declared change freeze left the ACI write gate open"
        assert "freeze" in d.reason

    @pytest.mark.parametrize("command", AUTO_AT_L4)
    def test_the_aci_chokepoint_denies_a_write_during_a_declared_freeze(self, mocker, command):
        _freeze(mocker, True)
        p = decide_write(command, earned_rung="L4")
        assert p.decision == "deny", f"{command!r} would have auto-executed during a freeze"
        assert "freeze" in p.reason

    @pytest.mark.parametrize("command", AUTO_AT_L4)
    def test_the_same_write_is_untouched_with_no_freeze(self, mocker, command):
        """A brake that denies when nobody engaged it is an outage, not a brake."""
        _freeze(mocker, False)
        assert decide_write(command, earned_rung="L4").decision == "auto"


class TestTheTwoGatesCannotDisagree:
    """The property, stated once, over every brake combination the two gates share."""

    @pytest.mark.parametrize(
        "kill_flag,kill_runtime,freeze_flag",
        list(itertools.product([False, True], repeat=3)),
    )
    def test_both_gates_reach_the_same_verdict(self, mocker, kill_flag, kill_runtime, freeze_flag):
        mocker.patch.object(settings, "KI_V5_KILL_SWITCH", kill_flag)
        mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", freeze_flag)
        if kill_runtime:
            engage_kill_switch()
        expected_denied = kill_flag or kill_runtime or freeze_flag
        assert auto_write_permitted().allow is (not expected_denied)
        assert gate_write().allow is (not expected_denied), (
            "the two gates disagree about a brake — which is how this bug existed"
        )

    @pytest.mark.parametrize(
        "kill_flag,kill_runtime,freeze_flag",
        list(itertools.product([False, True], repeat=3)),
    )
    def test_they_give_the_same_reason(self, mocker, kill_flag, kill_runtime, freeze_flag):
        mocker.patch.object(settings, "KI_V5_KILL_SWITCH", kill_flag)
        mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", freeze_flag)
        if kill_runtime:
            engage_kill_switch()
        assert auto_write_permitted().reason == gate_write().reason

    def test_both_gates_go_through_the_one_reader(self, mocker):
        """Patch the reader; both gates follow. That is the whole content of "one reader".

        Agreeing on today's two sources is not the property — the kill switch and the freeze
        agreed on the day the freeze was written too. The property is that there is nowhere
        for a third source, or a changed rule, to be added to one gate and not the other.
        """
        _freeze(mocker, False)
        mocker.patch("app.autonomy.budget.change_freeze_active", return_value=True)
        assert gate_write().allow is False, "gate_write does not consult the shared reader"
        assert auto_write_permitted().allow is False, (
            "auto_write_permitted does not consult the shared reader"
        )


class TestTheSharedReader:
    """`change_freeze_active` is to the freeze what `kill_switch_engaged` is to the switch."""

    def test_the_declared_flag_alone_is_enough(self, mocker):
        _freeze(mocker, True)
        assert change_freeze_active() is True

    def test_a_window_alone_is_enough(self, mocker):
        _freeze(mocker, False)
        assert change_freeze_active(INSIDE, WINDOW) is True

    def test_neither_source_means_no_freeze(self, mocker):
        _freeze(mocker, False)
        assert change_freeze_active() is False
        assert change_freeze_active(OUTSIDE, WINDOW) is False

    def test_the_flag_is_not_overridden_by_a_window_that_has_passed(self, mocker):
        """An operator's declared freeze outlives any window somebody injected."""
        _freeze(mocker, True)
        assert change_freeze_active(OUTSIDE, WINDOW) is True

    @pytest.mark.parametrize("now,windows", [
        (None, WINDOW),          # a window with no clock
        (INSIDE, None),          # a clock with no window
        (INSIDE, []),            # an empty window list
        (None, None),            # what `gate_write()` actually passes
    ])
    def test_a_half_supplied_window_is_not_a_freeze(self, mocker, now, windows):
        _freeze(mocker, False)
        assert change_freeze_active(now, windows) is False

    def test_it_returns_a_bool_not_a_truthy_list(self, mocker):
        """`windows and …` would otherwise hand back `[]`, and callers compare with `is`."""
        _freeze(mocker, False)
        assert change_freeze_active(INSIDE, []) is False

    def test_it_mirrors_the_kill_switch_reader(self, mocker):
        """Both brakes: one function, two sources, consulted by both gates."""
        mocker.patch.object(settings, "KI_V5_KILL_SWITCH", True)
        _freeze(mocker, True)
        assert kill_switch_engaged() is True and change_freeze_active() is True

    def test_the_pure_window_helper_is_unchanged(self):
        """`in_change_freeze` stays flag-blind — it is the deterministic half."""
        assert in_change_freeze(INSIDE, WINDOW) is True
        assert in_change_freeze(OUTSIDE, WINDOW) is False
        assert in_change_freeze(200.0, WINDOW) is False      # end-exclusive


class TestTheInjectedWindowPathStillWorks:
    """The behaviour that did work must not be traded away for the one that did not."""

    def test_gate_write_denies_inside_an_injected_window(self, mocker):
        _freeze(mocker, False)
        d = gate_write(now_epoch=INSIDE, freeze_windows=WINDOW)
        assert d.allow is False and "freeze" in d.reason

    def test_gate_write_allows_outside_it(self, mocker):
        _freeze(mocker, False)
        assert gate_write(now_epoch=OUTSIDE, freeze_windows=WINDOW).allow is True


class TestPrecedenceAndTheOtherBrakesAreUntouched:

    def test_governance_still_wins_over_a_freeze(self, mocker):
        _freeze(mocker, True)
        assert "governance" in gate_write(governance_ok=False).reason

    def test_the_kill_switch_still_wins_over_a_freeze(self, mocker):
        """Documented precedence: governance → kill switch → change freeze → spend."""
        mocker.patch.object(settings, "KI_V5_KILL_SWITCH", True)
        _freeze(mocker, True)
        assert gate_write().reason == "kill switch engaged"

    def test_a_freeze_wins_over_the_spend_cap(self, mocker):
        _freeze(mocker, True)
        assert "freeze" in gate_write(current_spend=9, projected_spend=5, spend_cap=10).reason

    def test_the_spend_cap_still_fires_with_no_freeze(self, mocker):
        _freeze(mocker, False)
        assert gate_write(current_spend=9, projected_spend=5, spend_cap=10).allow is False

    def test_no_brake_at_all_still_allows(self, mocker):
        _freeze(mocker, False)
        assert gate_write().allow is True and auto_write_permitted().allow is True


class TestWhatTheStatusEndpointClaims:
    """The reason this mattered while the ACI path is still unwired."""

    def test_the_reported_freeze_is_the_one_the_gates_enforce(self, mocker):
        """`/v1/v5/status` reports `settings.KI_V5_CHANGE_FREEZE` verbatim; so must the gates."""
        for declared in (True, False):
            mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", declared)
            reported = settings.KI_V5_CHANGE_FREEZE          # what v5_status.py:51 sends
            assert change_freeze_active() is reported
            assert gate_write().allow is (not reported)
            assert auto_write_permitted().allow is (not reported)


class TestAnInjectedBudgetStillBypassesTheGate:
    """Known and deliberate, recorded so it is a decision rather than a surprise.

    `decide_write(..., budget=...)` uses the caller's decision instead of calling `gate_write`
    (`mutating.py:80`). No production caller injects one — only tests do — but if one ever does,
    it opts that write out of *all* the brakes, not just the freeze. Pinned here so the next
    reader meets it as a documented property.
    """

    def test_an_injected_allow_overrides_a_declared_freeze(self, mocker):
        _freeze(mocker, True)
        p = decide_write("kubectl scale deploy/web --replicas=3", earned_rung="L4",
                         budget=BudgetDecision(True))
        assert p.decision == "auto"

    def test_an_injected_deny_is_honoured(self, mocker):
        _freeze(mocker, False)
        p = decide_write("kubectl scale deploy/web --replicas=3", earned_rung="L4",
                         budget=BudgetDecision(False, "change freeze in effect"))
        assert p.decision == "deny" and "freeze" in p.reason
