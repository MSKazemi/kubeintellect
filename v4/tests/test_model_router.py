"""Heterogeneous model routing + air-gap floor (v5 P4, ADR-103)."""
from __future__ import annotations

from app.cortex.model_router import (
    FRONTIER,
    RCA_SYNTHESIS,
    SMALL,
    TOOL_FORMAT,
    TRIAGE,
    degraded,
    edge_write_allowed,
    route_tier,
)


class TestRouteConnected:
    def test_triage_uses_small(self):
        assert route_tier(TRIAGE) == SMALL

    def test_tool_format_uses_small(self):
        assert route_tier(TOOL_FORMAT) == SMALL

    def test_rca_uses_frontier(self):
        assert route_tier(RCA_SYNTHESIS) == FRONTIER

    def test_unknown_defaults_frontier(self):
        assert route_tier("mystery") == FRONTIER


class TestAirGapFloor:
    def test_disconnected_degrades_rca_to_small(self):
        assert route_tier(RCA_SYNTHESIS, connected=False) == SMALL

    def test_disconnected_triage_still_small(self):
        assert route_tier(TRIAGE, connected=False) == SMALL

    def test_frontier_reachable_overrides_connected(self):
        # connected but frontier explicitly unreachable ⇒ still degrade
        assert route_tier(RCA_SYNTHESIS, connected=True, frontier_reachable=False) == SMALL
        # disconnected but frontier reachable (e.g. local frontier) ⇒ use it
        assert route_tier(RCA_SYNTHESIS, connected=False, frontier_reachable=True) == FRONTIER


class TestDegradedFlag:
    def test_degraded_when_frontier_unreachable(self):
        assert degraded(RCA_SYNTHESIS, connected=False) is True
        assert degraded(TRIAGE, connected=False) is False   # triage never wanted frontier

    def test_not_degraded_connected(self):
        assert degraded(RCA_SYNTHESIS, connected=True) is False


class TestEdgeWrites:
    def test_writes_blocked_when_disconnected(self):
        assert edge_write_allowed(connected=False) is False

    def test_writes_allowed_when_connected(self):
        assert edge_write_allowed(connected=True) is True
