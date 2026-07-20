"""Change-schedule + failure-domain budget (v5 P3, REQ-sysadmin-18)."""
from __future__ import annotations

from app.autonomy.failure_domain import (
    gate_disruption,
    in_maintenance_window,
    zone_disruption_ok,
)


class TestZoneCap:
    def test_within_cap_allows(self):
        # zone of 6, 1 already down, +1 = 2/6 = 33% ≤ 34%
        assert zone_disruption_ok(6, 1, max_unavailable_frac=0.34).allow is True

    def test_exceeding_cap_denies(self):
        # zone of 6, 2 already down, +1 = 3/6 = 50% > 34%
        d = zone_disruption_ok(6, 2, max_unavailable_frac=0.34)
        assert d.allow is False and "cap" in d.reason

    def test_single_node_zone_denied(self):
        # zone of 1, +1 = 100% > 34% ⇒ no redundancy, denied
        assert zone_disruption_ok(1, 0, max_unavailable_frac=0.34).allow is False

    def test_unknown_zone_fails_closed(self):
        assert zone_disruption_ok(0, 0).allow is False


class TestMaintenanceWindow:
    def test_no_windows_always_allowed(self):
        assert in_maintenance_window(1000.0, []) is True

    def test_inside_window(self):
        assert in_maintenance_window(150.0, [(100.0, 200.0)]) is True

    def test_outside_window(self):
        assert in_maintenance_window(250.0, [(100.0, 200.0)]) is False


class TestGate:
    def test_all_clear_allows(self):
        assert gate_disruption(zone_total=6, currently_unavailable=1).allow is True

    def test_outside_window_denies_first(self):
        d = gate_disruption(zone_total=6, currently_unavailable=0, now_epoch=250.0,
                            maintenance_windows=[(100.0, 200.0)])
        assert d.allow is False and "maintenance" in d.reason

    def test_domain_cap_denies(self):
        d = gate_disruption(zone_total=3, currently_unavailable=1)   # +1 = 2/3 = 67% > 34%
        assert d.allow is False and "unavailable" in d.reason
