"""Fleet-wide signal pooling (v5 P5 Fleet HQ).

The differentiator that needs multi-cluster mass: a single agent-runaway or detector hit on one
cluster is a local event, but the SAME signal on many clusters in a tenant is a fleet-wide incident
— a bad agent version rolled out everywhere, a shared dependency failing, a poisoned image. This
pools per-cluster signals within a tenant and raises a fleet alert when a pattern crosses the
cluster-count threshold. STRICT tenant isolation: pooling never mixes signals across tenants.

Pure/deterministic — fully unit-testable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass(frozen=True)
class FleetSignal:
    tenant: str
    cluster_id: str
    kind: str                # e.g. "agent-runaway", "gpu-unhealthy", "OOMKilled"
    severity: str = "warning"


@dataclass(frozen=True)
class FleetAlert:
    tenant: str
    kind: str
    affected_clusters: list[str] = field(default_factory=list)
    severity: str = "warning"

    @property
    def cluster_count(self) -> int:
        return len(self.affected_clusters)


def detect_fleet_patterns(
    signals: list[FleetSignal], *, min_clusters: int = 3,
) -> list[FleetAlert]:
    """Raise a FleetAlert per (tenant, kind) seen on >= ``min_clusters`` DISTINCT clusters.

    Tenant-scoped by construction: signals are grouped by (tenant, kind), so an alert never spans
    tenants. Severity escalates to 'critical' if any contributing signal was critical.
    """
    # (tenant, kind) -> {clusters}, and whether any was critical
    clusters: dict[tuple[str, str], set[str]] = defaultdict(set)
    critical: dict[tuple[str, str], bool] = defaultdict(bool)
    for s in signals:
        key = (s.tenant, s.kind)
        clusters[key].add(s.cluster_id)
        if s.severity == "critical":
            critical[key] = True

    alerts: list[FleetAlert] = []
    for (tenant, kind), cset in clusters.items():
        if len(cset) >= min_clusters:
            alerts.append(FleetAlert(
                tenant=tenant, kind=kind, affected_clusters=sorted(cset),
                severity="critical" if critical[(tenant, kind)] else "warning",
            ))
    return sorted(alerts, key=lambda a: (-a.cluster_count, a.kind))
