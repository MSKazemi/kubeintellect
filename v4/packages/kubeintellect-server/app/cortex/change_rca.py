"""Change-first RCA policy (v5 P2, A-CH-02-07).

~79% of outages follow a change (ch-02; precedent RCACopilot). So the agent's *search prior*
should rank recent changes from the change ledger before any other hypothesis class. This module
is the **policy** (rank + inject the prior into the investigation prompt); it is Cognition-plane.

The change **ledger** that feeds it — capturing deploys / image bumps / config edits off the live
cluster — is a **P1 deliverable and does not exist yet**. So ``recent_changes`` reads a *pluggable
source* that defaults to empty: with no P1 ledger the prior is a no-op (prompt unchanged), and when
P1 lands it registers a real source via ``set_change_source`` and the prior lights up unchanged.
The ranking/rendering here is pure and fully tested against injected changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ChangeRecord:
    kind: str            # e.g. "deployment", "image", "config", "scale", "rollout"
    target: str          # the object that changed (e.g. "deploy/web")
    ts_epoch: float      # when the change was observed (unix seconds)
    namespace: str = ""
    detail: str = ""     # short human description (e.g. "image :v2 -> :v3")


# Pluggable ledger source (P1 registers the real one). Signature: (cluster_id, namespace|None).
ChangeSource = Callable[[str, str | None], list[ChangeRecord]]


def _empty_source(cluster_id: str, namespace: str | None = None) -> list[ChangeRecord]:
    return []


_change_source: ChangeSource = _empty_source


def set_change_source(fn: ChangeSource) -> None:
    """Register the change-ledger reader (P1). Default is empty (prior is a no-op)."""
    global _change_source
    _change_source = fn


def recent_changes(cluster_id: str, namespace: str | None = None) -> list[ChangeRecord]:
    """Read recent changes from the registered source. Never raises (fails to no changes)."""
    try:
        return list(_change_source(cluster_id, namespace))
    except Exception as exc:
        logger.warning("change_rca source failed: %s", exc)
        return []


def rank_by_recency(changes: list[ChangeRecord]) -> list[ChangeRecord]:
    """Most-recent change first — the search-prior order."""
    return sorted(changes, key=lambda c: c.ts_epoch, reverse=True)


def render_change_prior(
    changes: list[ChangeRecord], *, max_items: int = 5, now: float | None = None,
) -> str:
    """Recency-ranked 'consider these changes first' prompt block. '' when there are no changes."""
    ranked = rank_by_recency(changes)[:max_items]
    if not ranked:
        return ""
    lines = ["## Recent changes (consider these FIRST — ~79% of outages follow a change)"]
    for c in ranked:
        ns = f" in {c.namespace}" if c.namespace else ""
        age = ""
        if now is not None:
            mins = max(0, int((now - c.ts_epoch) // 60))
            age = f" ({mins}m ago)"
        detail = f" — {c.detail}" if c.detail else ""
        lines.append(f"- {c.kind} {c.target}{ns}{age}{detail}")
    return "\n".join(lines)
