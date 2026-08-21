"""Investigation write-back (v5 P2): confirm/contradict KG edges from investigation outcomes.

Learned-topology pattern (01-architecture §3.2): an investigation is cheap evidence about the
world model, so feed it back. When an investigation observes a failure pattern on a cluster, we
reconcile a confirming ``(cluster)-[exhibits]->(pattern)`` edge through the existing ADR-015
``reconcile_edge`` path — near-zero cost, and the topology self-corrects over time.

v0 derives signals from the structured fields already on the turn (cluster + matched playbooks).
Richer edges (subject→root_cause) need structured fact extraction, which P4 also deferred — the
write-back *mechanism* here is complete and takes whatever signals it is given.

``reconcile_edge`` is itself fire-and-forget safe; ``apply_writeback`` additionally never raises.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.utils.logger import get_logger

logger = get_logger(__name__)

# reconcile_edge(cluster_id, src, rel, dst, attrs, ...) -> decision string.
ReconcileFn = Callable[..., Awaitable[str]]

_REL_EXHIBITS = "exhibits"


@dataclass(frozen=True)
class EdgeSignal:
    src: str
    rel: str
    dst: str
    verdict: str = "confirm"                 # "confirm" | "contradict"
    attrs: dict[str, Any] = field(default_factory=dict)


def signals_from_investigation(cluster_id: str, playbooks: list[str]) -> list[EdgeSignal]:
    """Derive confirm signals for the failure patterns an investigation matched on this cluster."""
    seen: set[str] = set()
    signals: list[EdgeSignal] = []
    for pb in playbooks:
        name = str(pb).strip()
        if name and name not in seen:
            seen.add(name)
            signals.append(EdgeSignal(src=cluster_id, rel=_REL_EXHIBITS, dst=name, verdict="confirm"))
    return signals


async def apply_writeback(
    cluster_id: str, signals: list[EdgeSignal], *, reconcile: ReconcileFn | None = None,
) -> dict[str, int]:
    """Reconcile each edge signal. Returns a tally of reconcile decisions. Never raises out."""
    tally: dict[str, int] = {}
    if not signals:
        return tally
    if reconcile is None:
        from app.memory.kg import reconcile_edge
        reconcile = reconcile_edge
    for sig in signals:
        # Confirmation reinforces; contradiction records a counter-signal on the same edge.
        attrs = dict(sig.attrs)
        attrs["investigation_confirmed" if sig.verdict == "confirm" else "investigation_contradicted"] = True
        try:
            decision = await reconcile(cluster_id, sig.src, sig.rel, sig.dst, attrs,
                                       source_kind="investigation")
        except Exception as exc:  # reconcile_edge is safe, but stay defensive.
            logger.warning("writeback reconcile failed for %s-[%s]->%s: %s",
                           sig.src, sig.rel, sig.dst, exc)
            decision = "ERROR"
        tally[decision] = tally.get(decision, 0) + 1
    return tally
