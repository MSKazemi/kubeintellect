"""Blast-radius / spend budget gate (v5 P3 Trust plane) — fail-closed write brakes."""
from __future__ import annotations

import pytest
from app.autonomy.budget import (
    auto_write_permitted,
    check_spend,
    disengage_kill_switch,
    engage_kill_switch,
    gate_write,
    in_change_freeze,
    kill_switch_engaged,
)
from app.core.config import settings


@pytest.fixture(autouse=True)
def reset_kill():
    disengage_kill_switch()
    yield
    disengage_kill_switch()


class TestKillSwitch:
    def test_runtime_engage_disengage(self):
        assert kill_switch_engaged() is False
        engage_kill_switch()
        assert kill_switch_engaged() is True
        disengage_kill_switch()
        assert kill_switch_engaged() is False

    def test_settings_flag_engages(self, mocker):
        mocker.patch.object(settings, "KI_V5_KILL_SWITCH", True)
        assert kill_switch_engaged() is True


class TestCheckSpend:
    def test_no_cap_allows(self):
        assert check_spend(100, 100, None).allow is True
        assert check_spend(100, 100, 0).allow is True     # 0 = unlimited

    def test_under_cap_allows(self):
        assert check_spend(5.0, 3.0, 10.0).allow is True

    def test_deny_before_breach(self):
        d = check_spend(8.0, 3.0, 10.0)              # 11 > 10, denied BEFORE running
        assert d.allow is False and "projected spend" in d.reason

    def test_exact_cap_allows(self):
        assert check_spend(7.0, 3.0, 10.0).allow is True   # 10 == 10, not over


class TestChangeFreeze:
    def test_inside_window(self):
        assert in_change_freeze(150, [(100, 200)]) is True

    def test_outside_window(self):
        assert in_change_freeze(250, [(100, 200)]) is False

    def test_end_exclusive(self):
        assert in_change_freeze(200, [(100, 200)]) is False


class TestGateWrite:
    def test_all_clear_allows(self):
        assert gate_write().allow is True

    def test_governance_unreachable_fails_closed(self):
        d = gate_write(governance_ok=False)
        assert d.allow is False and "fail-closed" in d.reason

    def test_kill_switch_denies(self):
        engage_kill_switch()
        assert gate_write().allow is False

    def test_freeze_denies(self):
        d = gate_write(now_epoch=150, freeze_windows=[(100, 200)])
        assert d.allow is False and "freeze" in d.reason

    def test_spend_breach_denies(self):
        d = gate_write(current_spend=9, projected_spend=5, spend_cap=10)
        assert d.allow is False

    def test_precedence_governance_first(self):
        engage_kill_switch()
        d = gate_write(governance_ok=False)
        assert "governance" in d.reason        # governance denial wins over kill switch


class TestAutoWritePermitted:
    def test_flag_off_still_honours_the_kill_switch(self, mocker):
        # Was `test_flag_off_always_allows`, asserting allow==True here. That encoded the
        # 2026-08-20 defect: the experimental flag defaults to False, so the operator's
        # break-glass switch was inert in every default deployment while /v5/status reported
        # it engaged. A kill switch is not a feature to opt into.
        mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", False)
        engage_kill_switch()
        assert auto_write_permitted().allow is False

    def test_flag_off_with_no_brake_leaves_the_ladder_unchanged(self, mocker):
        # The invariant the old test was actually protecting, and it still holds.
        mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", False)
        mocker.patch.object(settings, "KI_V5_KILL_SWITCH", False)
        mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", False)
        assert auto_write_permitted().allow is True

    def test_flag_on_kill_switch_denies(self, mocker):
        mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", True)
        engage_kill_switch()
        assert auto_write_permitted().allow is False

    def test_flag_on_change_freeze_denies(self, mocker):
        mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", True)
        mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", True)
        assert auto_write_permitted().allow is False

    def test_flag_on_clear_allows(self, mocker):
        mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", True)
        mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", False)
        assert auto_write_permitted().allow is True


class TestWatchtowerWiring:
    def _finding(self):
        from app.detectors.models import Finding
        return Finding(playbook="CrashLoopBackOff", cluster_id="cl-1", namespace="dev",
                       object_name="pod/x", evidence="crashloop", severity="warning")

    def test_gate_blocks_a3_when_engaged(self, mocker):
        from app.autonomy import watchtower
        mocker.patch.object(watchtower, "a3_allowed", return_value=True)
        mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", True)
        engage_kill_switch()
        assert watchtower._should_auto_fix(self._finding(), "A3") is False

    def test_gate_off_still_blocks_a3_when_the_kill_switch_is_engaged(self, mocker):
        # Was `test_gate_off_preserves_a3`, asserting True. See the note above: with the flag at
        # its default the watchtower kept auto-fixing through an engaged kill switch.
        from app.autonomy import watchtower
        mocker.patch.object(watchtower, "a3_allowed", return_value=True)
        mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", False)
        engage_kill_switch()
        assert watchtower._should_auto_fix(self._finding(), "A3") is False

    def test_gate_off_with_no_brake_preserves_a3(self, mocker):
        from app.autonomy import watchtower
        mocker.patch.object(watchtower, "a3_allowed", return_value=True)
        mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", False)
        mocker.patch.object(settings, "KI_V5_KILL_SWITCH", False)
        mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", False)
        assert watchtower._should_auto_fix(self._finding(), "A3") is True
