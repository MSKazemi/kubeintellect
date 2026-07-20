"""Fleet memory exchange (v5 P5 Fleet HQ, ADR-105).

Cross-cluster learning: a resolution learned on one cluster becomes available to sibling clusters
in the SAME tenant — the learning flywheel that needs multi-cluster mass. The load-bearing
invariant is **strict tenant isolation**: a cluster in tenant A must never read tenant B's fleet
knowledge. That isolation is enforced here at the read boundary, not left to callers.

v0 is an in-process, bounded, tenant-partitioned store (Postgres-backed fleet store + consistency
model per ADR-105 is the durability follow-up; losing the in-process cache degrades to per-cluster
memory, never to a cross-tenant leak). Pure/deterministic — fully unit-testable.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Optional

_MAX_PER_TENANT = 500


@dataclass(frozen=True)
class FleetEntry:
    tenant: str
    cluster_id: str
    signature: str          # the pattern/theme key (e.g. "OOMKilled|payments")
    summary: str            # the shareable resolution digest


# tenant → ring buffer of entries. Partitioned by tenant so a read can NEVER cross tenants.
_store: dict[str, Deque[FleetEntry]] = defaultdict(lambda: deque(maxlen=_MAX_PER_TENANT))


def publish(entry: FleetEntry) -> None:
    """Contribute a resolution to its tenant's fleet knowledge."""
    _store[entry.tenant].append(entry)


def read_fleet(tenant: str, *, exclude_cluster: Optional[str] = None,
               signature: Optional[str] = None) -> list[FleetEntry]:
    """Read a tenant's fleet knowledge. STRICT isolation: only ``tenant``'s partition is ever
    consulted. Optionally drop the reading cluster's own entries and filter by signature."""
    items = list(_store.get(tenant, ()))
    if exclude_cluster is not None:
        items = [e for e in items if e.cluster_id != exclude_cluster]
    if signature is not None:
        items = [e for e in items if e.signature == signature]
    return items


def tenants() -> list[str]:
    return sorted(_store.keys())


def _clear() -> None:
    """Test helper: wipe the exchange."""
    _store.clear()
