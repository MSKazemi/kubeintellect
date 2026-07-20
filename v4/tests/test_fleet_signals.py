"""Fleet-wide signal pooling (v5 P5) — cross-cluster pattern detection, tenant-scoped."""
from __future__ import annotations

from app.memory.fleet_signals import FleetSignal, detect_fleet_patterns


def _sigs(tenant, kind, clusters, severity="warning"):
    return [FleetSignal(tenant, c, kind, severity) for c in clusters]


class TestPatterns:
    def test_pattern_across_enough_clusters(self):
        sig = _sigs("acme", "agent-runaway", ["c1", "c2", "c3"])
        alerts = detect_fleet_patterns(sig, min_clusters=3)
        assert len(alerts) == 1 and alerts[0].kind == "agent-runaway"
        assert alerts[0].affected_clusters == ["c1", "c2", "c3"] and alerts[0].cluster_count == 3

    def test_below_threshold_no_alert(self):
        assert detect_fleet_patterns(_sigs("acme", "OOMKilled", ["c1", "c2"]), min_clusters=3) == []

    def test_distinct_clusters_required_not_duplicates(self):
        # same cluster firing 3x is NOT a fleet pattern
        sig = _sigs("acme", "agent-runaway", ["c1", "c1", "c1"])
        assert detect_fleet_patterns(sig, min_clusters=3) == []

    def test_critical_escalates_severity(self):
        sig = (_sigs("acme", "gpu-unhealthy", ["c1", "c2"])
               + _sigs("acme", "gpu-unhealthy", ["c3"], severity="critical"))
        alerts = detect_fleet_patterns(sig, min_clusters=3)
        assert alerts[0].severity == "critical"


class TestTenantIsolation:
    def test_pooling_never_crosses_tenants(self):
        # 2 acme + 1 globex of the same kind ⇒ neither reaches 3 within its own tenant
        sig = _sigs("acme", "agent-runaway", ["c1", "c2"]) + _sigs("globex", "agent-runaway", ["c9"])
        assert detect_fleet_patterns(sig, min_clusters=3) == []

    def test_each_tenant_pattern_separate(self):
        sig = (_sigs("acme", "agent-runaway", ["a1", "a2", "a3"])
               + _sigs("globex", "agent-runaway", ["g1", "g2", "g3"]))
        alerts = detect_fleet_patterns(sig, min_clusters=3)
        assert {a.tenant for a in alerts} == {"acme", "globex"}
        assert all(all(c.startswith("a") for c in a.affected_clusters)
                   for a in alerts if a.tenant == "acme")
