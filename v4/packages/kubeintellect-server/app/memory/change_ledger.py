"""Change ledger (v5 P1 evidence substrate) — the source that feeds change-first RCA.

The change-first RCA prior (P2 slice 8) reads recent changes from a pluggable source; this module
is the first real implementation of that source. v0 captures the changes **KubeIntellect itself
applies** — its mutating kubectl commands, which the agent already detects via `_ran_mutation`. That
is reliable and needs no cluster-watching. Cluster-wide capture (deploys/config edits from other
actors, off the sensorium event stream) is the richer follow-up; the store and the RCA seam do not
change when it lands.

In-process, bounded (ring buffer per cluster). Postgres persistence is a later concern — the prior
only needs *recent* changes, and a restart losing them degrades gracefully (empty prior = no-op).
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque, Optional

from app.cortex.change_rca import ChangeRecord, _empty_source, set_change_source

_MAX_PER_CLUSTER = 200
_ledger: dict[str, Deque[ChangeRecord]] = defaultdict(lambda: deque(maxlen=_MAX_PER_CLUSTER))
_installed = False

# kubectl mutating verb → change kind.
_VERB_KIND = {
    "apply": "apply", "create": "create", "delete": "delete", "scale": "scale",
    "patch": "config", "edit": "config", "replace": "config", "annotate": "config",
    "label": "config", "rollout": "rollout", "cordon": "node", "uncordon": "node",
    "drain": "node", "taint": "node",
}
# `kubectl set <sub> ...` → the sub is the kind (set image / set env / set resources).
_SET_SUBS = {"image", "env", "resources", "serviceaccount", "selector"}


def parse_kubectl_change(cmd: str, ts_epoch: float, namespace: str = "") -> Optional[ChangeRecord]:
    """Parse a mutating kubectl command into a ChangeRecord, or None if not a recognized change."""
    toks = cmd.strip().split()
    if toks and toks[0] == "kubectl":
        toks = toks[1:]
    if not toks:
        return None
    verb = toks[0].lower()
    rest = toks[1:]
    if verb == "set" and rest:
        sub = rest[0].lower()
        kind = sub if sub in _SET_SUBS else "config"
        rest = rest[1:]
    elif verb in _VERB_KIND:
        kind = _VERB_KIND[verb]
    else:
        return None
    # First non-flag token after the verb is the target (e.g. deploy/web, pod, -f file).
    target = next((t for t in rest if not t.startswith("-")), "")
    return ChangeRecord(kind=kind, target=target, ts_epoch=ts_epoch,
                        namespace=namespace, detail=cmd.strip()[:120])


def record_change(cluster_id: str, record: ChangeRecord) -> None:
    _ledger[cluster_id].append(record)


def recent(cluster_id: str, namespace: Optional[str] = None) -> list[ChangeRecord]:
    """The change-source reader registered with change_rca (most-recent-first is handled there)."""
    items = list(_ledger.get(cluster_id, ()))
    if namespace:
        items = [c for c in items if not c.namespace or c.namespace == namespace]
    return items


def install_as_change_source() -> None:
    """Register this ledger as change_rca's source (idempotent)."""
    global _installed
    if not _installed:
        set_change_source(recent)
        _installed = True


def record_from_commands(cluster_id: str, commands: list[str], ts_epoch: float,
                         namespace: str = "") -> int:
    """Record every recognized mutating command; ensures the source is wired. Returns count."""
    install_as_change_source()
    n = 0
    for cmd in commands:
        rec = parse_kubectl_change(cmd, ts_epoch, namespace)
        if rec is not None:
            record_change(cluster_id, rec)
            n += 1
    return n


def _clear() -> None:
    """Test helper: reset the ledger and the install flag."""
    global _installed
    _ledger.clear()
    _installed = False
    set_change_source(_empty_source)
