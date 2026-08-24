"""
L2 semantic memory — temporal knowledge graph (ADR-002, memory-hierarchy.md).

Entities (kg_entities) + valid-time edges (kg_edges): an edge with
valid_to IS NULL is currently true; closing an edge stamps valid_to.
This turns "what changed between 14:02 and 14:07" into one indexed query
(`changes()`) instead of an LLM archaeology dig.

Pool ownership: the memory service owns the asyncpg pool lifecycle and hands
it to this module via init_kg(pool). close_kg() drops the reference only —
it never closes the pool.

Failure discipline (V2 rule kept, same as flight_recorder.py): no memory-path
failure may ever break a user-facing response. Every public function catches
Exception, logs a warning, and returns a harmless fallback (None / [] / "").
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app.core.config import settings
from app.sensorium.observations import Observation
from app.utils.logger import get_logger

logger = get_logger(__name__)

_pool: asyncpg.Pool | None = None


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def init_kg(pool: asyncpg.Pool) -> None:
    """Attach an existing pool. The caller (memory service) owns its lifecycle."""
    global _pool
    _pool = pool
    logger.info("kg: ready")


def close_kg() -> None:
    """Detach the pool reference. Never closes the pool — the owner does."""
    global _pool
    _pool = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts(value: float) -> datetime:
    """Unix float → tz-aware datetime for asyncpg TIMESTAMPTZ params."""
    return datetime.fromtimestamp(value, tz=UTC)


def _attrs_json(attrs: dict[str, Any] | None) -> str:
    return json.dumps(attrs or {}, default=str)


def _parse_attrs(raw: Any) -> dict:
    """asyncpg returns JSONB as str by default — accept both."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return raw if isinstance(raw, dict) else {}


# ── Entities ──────────────────────────────────────────────────────────────────

async def upsert_entity(
    cluster_id: str,
    kind: str,
    name: str,
    namespace: str = "",
    attrs: dict[str, Any] | None = None,
) -> str | None:
    """Insert or merge-update one entity. Returns its id, or None on failure."""
    if _pool is None:
        return None
    try:
        row = await _pool.fetchrow(
            """
            INSERT INTO kg_entities (cluster_id, kind, name, namespace, attrs)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (cluster_id, kind, namespace, name)
            DO UPDATE SET attrs = kg_entities.attrs || EXCLUDED.attrs
            RETURNING id
            """,
            cluster_id,
            kind,
            name,
            namespace,
            _attrs_json(attrs),
        )
        return str(row["id"]) if row else None
    except Exception as exc:
        logger.warning(f"kg: upsert_entity failed ({kind}/{namespace}/{name}): {exc}")
        return None


# ── Edges ─────────────────────────────────────────────────────────────────────

def _event_time(event_time: float | None) -> datetime | None:
    """Event-time → valid_from/valid_to, but only when bi-temporality is on.

    Memory V5 P2 (ADR-013, review F3): freshness (ingested_at − valid_from) is only
    meaningful if valid_from is the real-world event time. When the flag is off, or no
    event time is supplied, we return None so the SQL falls back to now() — i.e. the
    exact pre-P2 behaviour, keeping the change additive.
    """
    if event_time is None or not settings.MEMORY_BITEMPORAL_ENABLED:
        return None
    return _ts(event_time)


async def open_edge(
    cluster_id: str,
    src_id: str,
    rel: str,
    dst_id: str,
    attrs: dict[str, Any] | None = None,
    source_kind: str = "observation",
    source_id: str | None = None,
    event_time: float | None = None,
) -> None:
    """Open a valid-time edge. Idempotent: a matching open edge means no-op.

    ``event_time`` (when bi-temporality is on) sets valid_from to the real-world event
    time; ingested_at defaults to now(), so ingested_at − valid_from = the ingest lag.
    """
    if _pool is None:
        return
    try:
        existing = await _pool.fetchrow(
            "SELECT id FROM kg_edges"
            " WHERE cluster_id = $1 AND src = $2 AND rel = $3 AND dst = $4"
            " AND valid_to IS NULL",
            cluster_id,
            src_id,
            rel,
            dst_id,
        )
        if existing is not None:
            return
        await _pool.execute(
            "INSERT INTO kg_edges"
            " (cluster_id, src, rel, dst, attrs, source_kind, source_id, valid_from)"
            " VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, COALESCE($8, now()))",
            cluster_id,
            src_id,
            rel,
            dst_id,
            _attrs_json(attrs),
            source_kind,
            source_id,
            _event_time(event_time),
        )
    except Exception as exc:
        logger.warning(f"kg: open_edge failed ({rel}): {exc}")


async def close_edge(
    cluster_id: str,
    src_id: str,
    rel: str,
    dst_id: str | None = None,
    event_time: float | None = None,
) -> None:
    """Stamp valid_to on matching open edges (dst is an optional filter).

    ``event_time`` (when bi-temporality is on) records *when the fact stopped being
    true* in the world, not when we noticed; otherwise valid_to = now() (pre-P2).
    """
    if _pool is None:
        return
    try:
        vt = _event_time(event_time)
        if dst_id is None:
            await _pool.execute(
                "UPDATE kg_edges SET valid_to = COALESCE($4, now())"
                " WHERE cluster_id = $1 AND src = $2 AND rel = $3 AND valid_to IS NULL",
                cluster_id,
                src_id,
                rel,
                vt,
            )
        else:
            await _pool.execute(
                "UPDATE kg_edges SET valid_to = COALESCE($5, now())"
                " WHERE cluster_id = $1 AND src = $2 AND rel = $3 AND dst = $4"
                " AND valid_to IS NULL",
                cluster_id,
                src_id,
                rel,
                dst_id,
                vt,
            )
    except Exception as exc:
        logger.warning(f"kg: close_edge failed ({rel}): {exc}")


async def retract_edge(
    cluster_id: str,
    src_id: str,
    rel: str,
    dst_id: str | None = None,
) -> int:
    """Retract edges on the TRANSACTION-time axis (Memory V5 P2, ADR-013).

    Sets ``retracted_at = now()`` — i.e. "we no longer believe we ever should have
    recorded this" — as opposed to ``close_edge`` which stamps ``valid_to`` ("the
    fact stopped being true in the world"). Never hard-deletes, so time-travel and
    audit stay intact (ADR-013 R1.2, review F4). Returns the number of rows retracted.
    """
    if _pool is None:
        return 0
    try:
        if dst_id is None:
            result = await _pool.execute(
                "UPDATE kg_edges SET retracted_at = now()"
                " WHERE cluster_id = $1 AND src = $2 AND rel = $3 AND retracted_at IS NULL",
                cluster_id, src_id, rel,
            )
        else:
            result = await _pool.execute(
                "UPDATE kg_edges SET retracted_at = now()"
                " WHERE cluster_id = $1 AND src = $2 AND rel = $3 AND dst = $4"
                " AND retracted_at IS NULL",
                cluster_id, src_id, rel, dst_id,
            )
        return int(result.split()[-1]) if result else 0
    except Exception as exc:
        logger.warning(f"kg: retract_edge failed ({rel}): {exc}")
        return 0


# ── Write reconciliation + salience (ADR-015 / P4) ────────────────────────────

_SALIENCE_FLOOR = 0.2      # below this, a candidate write is NOOP'd
_RETRACT_FLOOR = 0.5       # supersede a functional relation only above this (else ADD — F4)
_FUNCTIONAL_RELS = frozenset({"runs_on", "has_status"})       # src has ≤1 valid dst
_HIGH_VALUE_RELS = frozenset({"crashed_with", "fixed_by", "caused_by"})


def _salience_score(rel: str, attrs: dict[str, Any] | None) -> float:
    """Query-independent heuristic value of a candidate fact (ADR-015, roadmap Q3).

    Cheap and conservative (no model): incident-linking relations and higher-severity /
    verified facts score higher; structural observation edges get a passing baseline.
    Returns 0..1. To be calibrated from verified outcomes later (heuristic → learned).
    """
    attrs = attrs or {}
    score = 0.4  # structural baseline (runs_on/owns keep the graph connected)
    if rel in _HIGH_VALUE_RELS:
        score += 0.4
    sev = str(attrs.get("severity", "")).lower()
    if sev in ("critical", "error", "fatal"):
        score += 0.3
    elif sev in ("warning", "warn"):
        score += 0.1
    if attrs.get("verified") is True:
        score += 0.1
    return min(1.0, score)


async def reconcile_edge(
    cluster_id: str,
    src_id: str,
    rel: str,
    dst_id: str,
    attrs: dict[str, Any] | None = None,
    *,
    salience: float | None = None,
    event_time: float | None = None,
    source_kind: str = "observation",
    source_id: str | None = None,
) -> str:
    """Mem0-style write reconciliation for one candidate edge (ADR-015).

    Decides ADD / UPDATE / RETRACT / NOOP against existing memory, gated by a salience
    value function. RETRACT sets ``retracted_at`` (never hard-deletes, review F4) and fires
    only for *corrections* of a functional relation when confidence is high — a real-world
    change is still a valid-time ``close_edge``, not a retraction. Every supersede is logged
    for audit. Returns the decision. When the flag is off, behaves like ``open_edge`` (ADD).

    Intended for the extracted/LLM fact write path, NOT sensor ingest (where observations
    are ground truth and changes are valid-time). Fire-and-forget safe (never raises).
    """
    if _pool is None:
        return "NOOP"
    if not settings.MEMORY_WRITE_RECONCILE:
        await open_edge(cluster_id, src_id, rel, dst_id, attrs, source_kind, source_id, event_time)
        return "ADD"
    try:
        score = salience if salience is not None else _salience_score(rel, attrs)
        if score < _SALIENCE_FLOOR:
            return "NOOP"
        exact = await _pool.fetchrow(
            "SELECT id, attrs FROM kg_edges"
            " WHERE cluster_id = $1 AND src = $2 AND rel = $3 AND dst = $4"
            " AND valid_to IS NULL AND retracted_at IS NULL",
            cluster_id, src_id, rel, dst_id,
        )
        if exact is not None:
            old_attrs = _parse_attrs(exact["attrs"])
            if not attrs or {**old_attrs, **attrs} == old_attrs:
                return "NOOP"                     # redundant re-assertion
            await _pool.execute(
                "UPDATE kg_edges SET attrs = attrs || $2::jsonb WHERE id = $1",
                exact["id"], _attrs_json(attrs),
            )
            return "UPDATE"
        if rel in _FUNCTIONAL_RELS and score >= _RETRACT_FLOOR:
            other = await _pool.fetchrow(
                "SELECT dst FROM kg_edges"
                " WHERE cluster_id = $1 AND src = $2 AND rel = $3"
                " AND valid_to IS NULL AND retracted_at IS NULL LIMIT 1",
                cluster_id, src_id, rel,
            )
            if other is not None and str(other["dst"]) != dst_id:
                await retract_edge(cluster_id, src_id, rel, str(other["dst"]))
                logger.info(
                    f"kg: SUPERSEDE cluster={cluster_id} src={src_id} rel={rel}"
                    f" old_dst={other['dst']} new_dst={dst_id} salience={score:.2f}"
                )
                await open_edge(
                    cluster_id, src_id, rel, dst_id, attrs, source_kind, source_id, event_time
                )
                return "RETRACT"
        await open_edge(cluster_id, src_id, rel, dst_id, attrs, source_kind, source_id, event_time)
        return "ADD"
    except Exception as exc:
        logger.warning(f"kg: reconcile_edge failed ({rel}): {exc}")
        return "NOOP"


async def _close_all_edges(cluster_id: str, entity_id: str) -> None:
    """Close every open edge touching an entity (src or dst). Internal."""
    if _pool is None:
        return
    await _pool.execute(
        "UPDATE kg_edges SET valid_to = now()"
        " WHERE cluster_id = $1 AND (src = $2 OR dst = $2) AND valid_to IS NULL",
        cluster_id,
        entity_id,
    )


async def _open_runs_on_dst(cluster_id: str, pod_id: str) -> str | None:
    """Return the dst entity id of the pod's currently open runs_on edge."""
    if _pool is None:
        return None
    row = await _pool.fetchrow(
        "SELECT dst FROM kg_edges"
        " WHERE cluster_id = $1 AND src = $2 AND rel = 'runs_on' AND valid_to IS NULL",
        cluster_id,
        pod_id,
    )
    return str(row["dst"]) if row else None


# ── Sensorium ingestion ───────────────────────────────────────────────────────

def observation_ref(obs: Observation) -> str | None:
    """A checkable handle for the object version an edge was derived from, or None.

    Every edge carries ``source_kind`` (``NOT NULL DEFAULT 'observation'``) and that string
    is the sole input to ``memory/security.trust_score`` — ``'observation'`` scores 1.0. Until
    now the accompanying ``source_id`` was NULL on every edge the ingest path wrote, so the
    graph asserted a provenance class it could not resolve: *which* observation was
    unanswerable, and an operator auditing why the agent believes a fact had nowhere to go.

    An id for the Observation itself would not fix that — observations are an in-memory
    stream with no table behind them, so it would be a pointer to nothing. The apiserver's
    ``uid`` + ``resourceVersion`` is the real evidence handle: it names the exact object
    version the fact came from and can be checked against the cluster.

    Honest about its limits: ``resourceVersion`` is not retained indefinitely and the object
    may be deleted, so an old ref may no longer resolve. It still says *what* to look for,
    which NULL never did.
    """
    fields = obs.fields or {}
    uid = str(fields.get("uid") or "").strip()
    if not uid:
        return None
    rv = str(fields.get("resource_version") or "").strip()
    return f"{obs.kind}:{uid}@{rv}" if rv else f"{obs.kind}:{uid}"



async def ingest_pod_observation(obs: Observation) -> None:
    """Maintain the graph from one pod_status observation.

    - Pod entity upserted with its last status.
    - fields["node"] present  → Pod -runs_on-> Node (close + reopen on change).
    - fields["owner"] present → Workload(kind from prefix) -owns-> Pod.
    - fields["watch_type"] == "DELETED" → close ALL open edges touching the pod.
    """
    try:
        if obs.kind != "pod_status":
            return
        fields = obs.fields or {}
        pod_id = await upsert_entity(
            obs.cluster_id,
            "Pod",
            obs.name,
            obs.namespace,
            # last_seen drives consolidation's stale-edge pass.
            {"last_status": fields.get("status", ""), "last_seen": obs.ts},
        )
        if pod_id is None:
            return

        if fields.get("watch_type") == "DELETED":
            await _close_all_edges(obs.cluster_id, pod_id)
            return

        node = fields.get("node")
        if node:
            node_id = await upsert_entity(obs.cluster_id, "Node", node)
            if node_id is not None:
                current_dst = await _open_runs_on_dst(obs.cluster_id, pod_id)
                if current_dst is not None and current_dst != node_id:
                    # The pod moved nodes: the old fact stopped being true at obs time.
                    await close_edge(obs.cluster_id, pod_id, "runs_on", event_time=obs.ts)
                await open_edge(
                    obs.cluster_id, pod_id, "runs_on", node_id,
                    source_id=observation_ref(obs), event_time=obs.ts,
                )

        owner = fields.get("owner")
        if owner and "/" in owner:
            owner_kind, owner_name = owner.split("/", 1)
            workload_id = await upsert_entity(
                obs.cluster_id, owner_kind, owner_name, obs.namespace
            )
            if workload_id is not None:
                await open_edge(
                    obs.cluster_id, workload_id, "owns", pod_id,
                    source_id=observation_ref(obs), event_time=obs.ts,
                )
    except Exception as exc:
        logger.warning(f"kg: ingest_pod_observation failed ({obs.name}): {exc}")


# ── Incident linking (findings / episodes) ────────────────────────────────────

async def link_incident(
    cluster_id: str,
    namespace: str,
    pod_name: str,
    incident_name: str,
    rel: str = "crashed_with",
    source_id: str | None = None,
) -> None:
    """Link a pod to an Incident entity (Pod -crashed_with-> Incident)."""
    try:
        pod_id = await upsert_entity(cluster_id, "Pod", pod_name, namespace)
        incident_id = await upsert_entity(cluster_id, "Incident", incident_name)
        if pod_id is None or incident_id is None:
            return
        await open_edge(
            cluster_id,
            pod_id,
            rel,
            incident_id,
            source_kind="episode",
            source_id=source_id,
        )
    except Exception as exc:
        logger.warning(f"kg: link_incident failed ({incident_name}): {exc}")


# ── Time-travel diff ──────────────────────────────────────────────────────────

class KGUnavailable(RuntimeError):
    """A graph read was attempted and could not be answered — distinct from "nothing matched".

    The twin of `episodes.MemoryUnavailable`, and it exists for the same measured reason. A
    failed `changes()` returned `[]`, `recent_changes_block` rendered that as `""`, and the
    triage prompt simply omitted its "Recent cluster changes (last 15m)" section — byte for
    byte what a genuinely calm cluster produces. "What changed in the last fifteen minutes" is
    the first question of an incident, and a Postgres outage answered it with *nothing did*.

    Deliberately narrower than the write path: `upsert_entity`/`open_edge`/`close_edge` and the
    ingest helpers still swallow, because a failed observation write must never kill a turn.
    A read that feeds the model is the opposite case — its silence is an assertion.
    """


async def changes(cluster_id: str, t1: float, t2: float) -> list[dict]:
    """Edges that opened or closed in (t1, t2], joined to entity identities.

    Each dict: {"change": "opened"|"closed", "at": <unix float>, "src":
    "Kind/ns/name", "rel": ..., "dst": "Kind/ns/name", "attrs": {...}}.
    An edge both opened and closed inside the window yields two rows.

    Raises `KGUnavailable` if the query could not be answered. An empty list means the window
    held no changes, and nothing else. No pool is a configuration state, not a failure, and
    still returns `[]` — the same split `episodes.recall_episodes` makes.
    """
    if _pool is None:
        return []
    try:
        lo, hi = _ts(t1), _ts(t2)
        rows = await _pool.fetch(
            """
            SELECT e.rel, e.attrs, e.valid_from, e.valid_to,
                   s.kind AS src_kind, s.namespace AS src_ns, s.name AS src_name,
                   d.kind AS dst_kind, d.namespace AS dst_ns, d.name AS dst_name
            FROM kg_edges e
            JOIN kg_entities s ON s.id = e.src
            JOIN kg_entities d ON d.id = e.dst
            WHERE e.cluster_id = $1
              AND ((e.valid_from > $2 AND e.valid_from <= $3)
                OR (e.valid_to IS NOT NULL AND e.valid_to > $2 AND e.valid_to <= $3))
            ORDER BY e.valid_from
            """,
            cluster_id,
            lo,
            hi,
        )
        out: list[dict] = []
        for row in rows:
            base = {
                "src": f"{row['src_kind']}/{row['src_ns']}/{row['src_name']}",
                "rel": row["rel"],
                "dst": f"{row['dst_kind']}/{row['dst_ns']}/{row['dst_name']}",
                "attrs": _parse_attrs(row["attrs"]),
            }
            valid_from, valid_to = row["valid_from"], row["valid_to"]
            if valid_from is not None and lo < valid_from <= hi:
                out.append({"change": "opened", "at": valid_from.timestamp(), **base})
            if valid_to is not None and lo < valid_to <= hi:
                out.append({"change": "closed", "at": valid_to.timestamp(), **base})
        out.sort(key=lambda change: change["at"])
        return out
    except Exception as exc:
        logger.warning(f"kg: changes failed: {exc}")
        raise KGUnavailable(f"the cluster change log could not be read: {exc}") from exc


def _edge_identity(row) -> dict:
    """Shared src/rel/dst/attrs projection for edge-returning queries."""
    return {
        "src": f"{row['src_kind']}/{row['src_ns']}/{row['src_name']}",
        "rel": row["rel"],
        "dst": f"{row['dst_kind']}/{row['dst_ns']}/{row['dst_name']}",
        "attrs": _parse_attrs(row["attrs"]),
    }


async def as_of(cluster_id: str, valid_t: float, tx_t: float | None = None) -> list[dict]:
    """Bi-temporal point-in-time query (Memory V5 P2, ADR-013).

    Returns the edges that, **as the agent believed at transaction-time ``tx_t``**
    (default: now), were **valid in the world at event-time ``valid_t``**. This is the
    "what did we believe at T vs what was actually true" query that a single valid-time
    axis cannot answer. ``tx_t=None`` ⇒ current belief about the historical state valid_t.
    """
    if _pool is None:
        return []
    try:
        vt = _ts(valid_t)
        tt = _ts(tx_t) if tx_t is not None else _ts(time.time())
        rows = await _pool.fetch(
            """
            SELECT e.rel, e.attrs,
                   s.kind AS src_kind, s.namespace AS src_ns, s.name AS src_name,
                   d.kind AS dst_kind, d.namespace AS dst_ns, d.name AS dst_name
            FROM kg_edges e
            JOIN kg_entities s ON s.id = e.src
            JOIN kg_entities d ON d.id = e.dst
            WHERE e.cluster_id = $1
              AND e.valid_from <= $2 AND (e.valid_to IS NULL OR e.valid_to > $2)
              AND e.ingested_at <= $3 AND (e.retracted_at IS NULL OR e.retracted_at > $3)
            ORDER BY e.valid_from
            """,
            cluster_id, vt, tt,
        )
        return [_edge_identity(row) for row in rows]
    except Exception as exc:
        logger.warning(f"kg: as_of failed: {exc}")
        return []


async def current_edges(cluster_id: str, limit: int = 200) -> list[dict]:
    """The default read (S1): edges currently true and currently believed.

    ``valid_to IS NULL AND retracted_at IS NULL`` — index-backed (idx_kg_edges_current).
    This is the common case; ``as_of()`` is the explicit historical opt-in.
    """
    if _pool is None:
        return []
    try:
        rows = await _pool.fetch(
            """
            SELECT e.rel, e.attrs,
                   s.kind AS src_kind, s.namespace AS src_ns, s.name AS src_name,
                   d.kind AS dst_kind, d.namespace AS dst_ns, d.name AS dst_name
            FROM kg_edges e
            JOIN kg_entities s ON s.id = e.src
            JOIN kg_entities d ON d.id = e.dst
            WHERE e.cluster_id = $1
              AND e.valid_to IS NULL AND e.retracted_at IS NULL
            ORDER BY e.valid_from DESC
            LIMIT $2
            """,
            cluster_id, limit,
        )
        return [_edge_identity(row) for row in rows]
    except Exception as exc:
        logger.warning(f"kg: current_edges failed: {exc}")
        return []


async def mean_ingest_lag_seconds(cluster_id: str, minutes: int = 60) -> float | None:
    """LOFA-L1 freshness signal (Memory V5 P2): mean ``ingested_at − valid_from`` over
    edges ingested in the last ``minutes``. High lag ⇒ the KG trails reality (the risk
    ADR-002 flags at scale). Meaningful only once valid_from is event-time (F3); returns
    None when bi-temporality is off, nothing recent, or on any failure.
    """
    if _pool is None or not settings.MEMORY_BITEMPORAL_ENABLED:
        return None
    try:
        row = await _pool.fetchrow(
            """
            SELECT avg(extract(epoch FROM (ingested_at - valid_from))) AS lag
            FROM kg_edges
            WHERE cluster_id = $1
              AND ingested_at > now() - make_interval(mins => $2)
              AND valid_from IS NOT NULL AND ingested_at IS NOT NULL
            """,
            cluster_id, minutes,
        )
        return float(row["lag"]) if row and row["lag"] is not None else None
    except Exception as exc:
        logger.warning(f"kg: mean_ingest_lag_seconds failed: {exc}")
        return None


# ── Multi-hop blast radius (Personalized PageRank, ADR-014 / P3) ──────────────

# Bounded ≤N-hop induced subgraph around the seed entities (currently-true, currently-
# believed edges only), capped at $4 edges. A recursive CTE gives k-hop REACHABILITY —
# PPR *scores* are then computed in-process, not in SQL (review F1). Neighbour = the
# other endpoint of any incident edge (undirected traversal via CASE).
_SQL_PPR_SUBGRAPH = """
    WITH RECURSIVE nodes AS (
        SELECT n AS node_id, 0 AS hop FROM unnest($2::uuid[]) AS n
        UNION
        SELECT CASE WHEN e.src = nodes.node_id THEN e.dst ELSE e.src END, nodes.hop + 1
        FROM nodes
        JOIN kg_edges e
          ON (e.src = nodes.node_id OR e.dst = nodes.node_id)
         AND e.cluster_id = $1 AND e.valid_to IS NULL AND e.retracted_at IS NULL
        WHERE nodes.hop < $3
    ),
    node_set AS (SELECT DISTINCT node_id FROM nodes)
    SELECT e.src::text AS src, e.dst::text AS dst, e.rel,
           s.kind AS src_kind, s.namespace AS src_ns, s.name AS src_name,
           d.kind AS dst_kind, d.namespace AS dst_ns, d.name AS dst_name
    FROM kg_edges e
    JOIN kg_entities s ON s.id = e.src
    JOIN kg_entities d ON d.id = e.dst
    WHERE e.cluster_id = $1 AND e.valid_to IS NULL AND e.retracted_at IS NULL
      AND e.src IN (SELECT node_id FROM node_set)
      AND e.dst IN (SELECT node_id FROM node_set)
    LIMIT $4
"""


def _power_iteration_ppr(
    edges: list[tuple[str, str]],
    seeds: set[str],
    damping: float = 0.85,
    max_iter: int = 50,
    tol: float = 1e-6,
) -> dict[str, float]:
    """Personalized PageRank over an undirected bounded subgraph (in-process).

    Row-normalised power iteration with restart on the seed set. The subgraph is small
    (≤ a few hundred edges), so this converges well within the §8 latency cap without a
    scipy/networkx dependency. Returns node_id → score.
    """
    adj: dict[str, list[str]] = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    nodes = list(adj.keys())
    if not nodes:
        return {}
    seed_nodes = [n for n in seeds if n in adj] or nodes  # fall back to uniform restart
    restart = 1.0 / len(seed_nodes)
    seed_set = set(seed_nodes)
    scores = {n: (restart if n in seed_set else 0.0) for n in nodes}
    for _ in range(max_iter):
        nxt = {n: (1.0 - damping) * (restart if n in seed_set else 0.0) for n in nodes}
        for n in nodes:
            s = scores[n]
            if s == 0.0:
                continue
            share = damping * s / len(adj[n])
            for nb in adj[n]:
                nxt[nb] += share
        delta = sum(abs(nxt[n] - scores[n]) for n in nodes)
        scores = nxt
        if delta < tol:
            break
    return scores


async def ppr_blast_radius(
    cluster_id: str,
    seeds: list[str],
    max_hops: int = 3,
    top_k: int = 10,
    max_edges: int = 500,
) -> list[dict]:
    """Rank the entities most related to ``seeds`` — their multi-hop blast radius.

    Pulls a bounded ≤``max_hops`` induced subgraph (currently-true edges) with a recursive
    CTE, then ranks nodes by Personalized PageRank in-process (review F1). Returns up to
    ``top_k`` entities (excluding the seeds) as ``{"entity": "Kind/ns/name", "score": float,
    "rel_sample": str}``. Empty when the flag is off, no seeds, or on any failure.
    """
    if _pool is None or not settings.MEMORY_KG_PPR or not seeds:
        return []
    try:
        rows = await _pool.fetch(_SQL_PPR_SUBGRAPH, cluster_id, seeds, max_hops, max_edges)
        if not rows:
            return []
        edges = [(row["src"], row["dst"]) for row in rows]
        label: dict[str, str] = {}
        rel_of: dict[str, str] = {}
        for row in rows:
            label[row["src"]] = f"{row['src_kind']}/{row['src_ns']}/{row['src_name']}"
            label[row["dst"]] = f"{row['dst_kind']}/{row['dst_ns']}/{row['dst_name']}"
            rel_of.setdefault(row["dst"], row["rel"])
            rel_of.setdefault(row["src"], row["rel"])
        seed_set = set(seeds)
        scores = _power_iteration_ppr(edges, seed_set)
        ranked = sorted(
            ((nid, sc) for nid, sc in scores.items() if nid not in seed_set),
            key=lambda kv: kv[1],
            reverse=True,
        )[:top_k]
        return [
            {"entity": label.get(nid, nid), "score": round(sc, 6),
             "rel_sample": rel_of.get(nid, "")}
            for nid, sc in ranked
        ]
    except Exception as exc:
        logger.warning(f"kg: ppr_blast_radius failed: {exc}")
        return []


async def recent_changes_block(
    cluster_id: str,
    minutes: int = 15,
    limit: int = 12,
) -> str:
    """Render changes() of the last N minutes as a compact prompt block.

    One line per change ("14:02:11 opened Pod/s/web-1 -runs_on-> Node//worker-2"),
    capped at `limit` lines plus a "(+N more)" tail. Empty string when nothing changed —
    and *only* then. Propagates `KGUnavailable` when the window could not be read, because
    the caller renders this into a prompt and "" is already the model's evidence of calm.
    """
    now = time.time()
    rows = await changes(cluster_id, now - minutes * 60, now)
    if not rows:
        return ""
    lines = [
        f"{_ts(row['at']).strftime('%H:%M:%S')} {row['change']}"
        f" {row['src']} -{row['rel']}-> {row['dst']}"
        for row in rows[:limit]
    ]
    overflow = len(rows) - limit
    if overflow > 0:
        lines.append(f"(+{overflow} more)")
    return "\n".join(lines)
