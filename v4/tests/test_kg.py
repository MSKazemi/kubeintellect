"""L2 temporal knowledge graph (ADR-002) — entities, valid-time edges, diff."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.memory import kg
from app.sensorium.observations import Observation

# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakePool:
    """Records every call; returns canned results per method (FIFO)."""

    def __init__(self, fetchrow_results=None, fetch_results=None):
        self.calls: list[tuple[str, str, tuple]] = []
        self.fetchrow_results = list(fetchrow_results or [])
        self.fetch_results = list(fetch_results or [])

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "OK"

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self.fetchrow_results.pop(0) if self.fetchrow_results else None

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self.fetch_results.pop(0) if self.fetch_results else []


class RaisingPool:
    """Every DB method raises — the module must swallow it."""

    async def execute(self, *args):
        raise RuntimeError("db down")

    async def fetchrow(self, *args):
        raise RuntimeError("db down")

    async def fetch(self, *args):
        raise RuntimeError("db down")


@pytest.fixture
def pool():
    fake = FakePool()
    kg.init_kg(fake)
    yield fake
    kg.close_kg()


def _edge_inserts(fake: FakePool) -> list[tuple[str, str, tuple]]:
    return [c for c in fake.calls if c[0] == "execute" and "INSERT INTO kg_edges" in c[1]]


def _edge_updates(fake: FakePool) -> list[tuple[str, str, tuple]]:
    return [c for c in fake.calls if c[0] == "execute" and "UPDATE kg_edges" in c[1]]


def _pod_obs(fields: dict, name: str = "web-1") -> Observation:
    return Observation(
        kind="pod_status", cluster_id="c1", namespace="default", name=name, fields=fields
    )


# ── upsert_entity ─────────────────────────────────────────────────────────────

class TestUpsertEntity:
    async def test_returns_id_as_str(self, pool):
        entity_id = uuid.uuid4()
        pool.fetchrow_results = [{"id": entity_id}]
        result = await kg.upsert_entity("c1", "Pod", "web-1", "default", {"a": 1})
        assert result == str(entity_id)
        method, sql, args = pool.calls[0]
        assert method == "fetchrow"
        assert "ON CONFLICT (cluster_id, kind, namespace, name)" in sql
        assert "kg_entities.attrs || EXCLUDED.attrs" in sql
        assert args[:4] == ("c1", "Pod", "web-1", "default")
        assert json.loads(args[4]) == {"a": 1}

    async def test_defaults_namespace_and_attrs(self, pool):
        pool.fetchrow_results = [{"id": uuid.uuid4()}]
        await kg.upsert_entity("c1", "Node", "worker-2")
        _, _, args = pool.calls[0]
        assert args[3] == ""
        assert json.loads(args[4]) == {}

    async def test_without_pool_returns_none(self):
        kg.close_kg()
        assert await kg.upsert_entity("c1", "Pod", "web-1") is None


# ── open_edge / close_edge ────────────────────────────────────────────────────

class TestOpenEdge:
    async def test_inserts_when_no_open_edge(self, pool):
        await kg.open_edge("c1", "src-1", "runs_on", "dst-1", {"w": 1}, source_id="ep-9")
        assert len(_edge_inserts(pool)) == 1
        _, _sql, args = _edge_inserts(pool)[0]
        assert args[:4] == ("c1", "src-1", "runs_on", "dst-1")
        assert json.loads(args[4]) == {"w": 1}
        # P2: trailing valid_from param; None when bitemporal off ⇒ SQL COALESCE → now()
        assert args[5:] == ("observation", "ep-9", None)

    async def test_idempotent_when_open_edge_exists(self, pool):
        pool.fetchrow_results = [{"id": uuid.uuid4()}]  # an open edge already exists
        await kg.open_edge("c1", "src-1", "runs_on", "dst-1")
        assert _edge_inserts(pool) == []
        # the existence probe filters on open edges only
        _, sql, _ = pool.calls[0]
        assert "valid_to IS NULL" in sql


class TestCloseEdge:
    async def test_sets_valid_to_on_open_edges(self, pool):
        await kg.close_edge("c1", "src-1", "runs_on")
        assert len(_edge_updates(pool)) == 1
        _, sql, args = _edge_updates(pool)[0]
        assert "SET valid_to = COALESCE($4, now())" in sql
        assert "valid_to IS NULL" in sql
        assert args == ("c1", "src-1", "runs_on", None)  # P2: trailing event-time param

    async def test_optional_dst_filter(self, pool):
        await kg.close_edge("c1", "src-1", "runs_on", "dst-1")
        _, sql, args = _edge_updates(pool)[0]
        assert "dst = $4" in sql
        assert args == ("c1", "src-1", "runs_on", "dst-1", None)  # P2: trailing event-time param


# ── ingest_pod_observation ────────────────────────────────────────────────────

class TestIngestPodObservation:
    async def test_creates_pod_entity_runs_on_and_owns_edges(self, pool):
        pod_id, node_id, rs_id = (uuid.uuid4() for _ in range(3))
        pool.fetchrow_results = [
            {"id": pod_id},   # upsert Pod
            {"id": node_id},  # upsert Node
            None,             # current open runs_on edge — none
            None,             # open_edge existence probe (runs_on)
            {"id": rs_id},    # upsert Workload
            None,             # open_edge existence probe (owns)
        ]
        await kg.ingest_pod_observation(
            _pod_obs({"status": "Running", "node": "worker-2", "owner": "ReplicaSet/web-7db6"})
        )

        # Pod entity carries last status + last_seen (consolidation's staleness signal)
        _, _, pod_args = pool.calls[0]
        assert pod_args[:4] == ("c1", "Pod", "web-1", "default")
        attrs = json.loads(pod_args[4])
        assert attrs["last_status"] == "Running"
        assert isinstance(attrs["last_seen"], float)
        # Node is cluster-scoped (empty namespace)
        _, _, node_args = pool.calls[1]
        assert node_args[:4] == ("c1", "Node", "worker-2", "")
        # Workload kind parsed from the owner prefix
        workload_args = [
            args for (m, sql, args) in pool.calls
            if m == "fetchrow" and "kg_entities" in sql and args[1] == "ReplicaSet"
        ]
        assert workload_args == [("c1", "ReplicaSet", "web-7db6", "default", "{}")]

        inserts = _edge_inserts(pool)
        assert [(c[2][1], c[2][2], c[2][3]) for c in inserts] == [
            (str(pod_id), "runs_on", str(node_id)),
            (str(rs_id), "owns", str(pod_id)),
        ]
        assert _edge_updates(pool) == []

    async def test_node_change_closes_then_reopens(self, pool):
        pod_id, new_node_id = uuid.uuid4(), uuid.uuid4()
        pool.fetchrow_results = [
            {"id": pod_id},          # upsert Pod
            {"id": new_node_id},     # upsert Node (worker-3)
            {"dst": uuid.uuid4()},   # current runs_on points at a DIFFERENT node
            None,                    # open_edge existence probe after close
        ]
        await kg.ingest_pod_observation(_pod_obs({"status": "Running", "node": "worker-3"}))

        updates, inserts = _edge_updates(pool), _edge_inserts(pool)
        assert len(updates) == 1 and len(inserts) == 1
        assert updates[0][2] == ("c1", str(pod_id), "runs_on", None)  # P2 event-time param (None, flag off)
        assert inserts[0][2][:4] == ("c1", str(pod_id), "runs_on", str(new_node_id))
        # close must happen before the reopen
        assert pool.calls.index(updates[0]) < pool.calls.index(inserts[0])

    async def test_same_node_is_noop(self, pool):
        pod_id, node_id = uuid.uuid4(), uuid.uuid4()
        pool.fetchrow_results = [
            {"id": pod_id},
            {"id": node_id},
            {"dst": node_id},  # current runs_on already points at this node
            {"id": uuid.uuid4()},  # open_edge probe finds the existing edge
        ]
        await kg.ingest_pod_observation(_pod_obs({"status": "Running", "node": "worker-2"}))
        assert _edge_updates(pool) == []
        assert _edge_inserts(pool) == []

    async def test_deleted_closes_all_edges_touching_pod(self, pool):
        pod_id = uuid.uuid4()
        pool.fetchrow_results = [{"id": pod_id}]
        await kg.ingest_pod_observation(
            _pod_obs({"status": "Terminating", "watch_type": "DELETED", "node": "worker-2"})
        )
        updates = _edge_updates(pool)
        assert len(updates) == 1
        _, sql, args = updates[0]
        assert "(src = $2 OR dst = $2)" in sql
        assert args == ("c1", str(pod_id))
        # no node/owner processing after deletion
        assert _edge_inserts(pool) == []
        assert len([c for c in pool.calls if c[0] == "fetchrow"]) == 1

    async def test_non_pod_observation_is_ignored(self, pool):
        obs = Observation(
            kind="event", cluster_id="c1", namespace="default", name="web-1",
            fields={"reason": "BackOff"},
        )
        await kg.ingest_pod_observation(obs)
        assert pool.calls == []

    async def test_no_node_no_owner_only_upserts_pod(self, pool):
        pool.fetchrow_results = [{"id": uuid.uuid4()}]
        await kg.ingest_pod_observation(_pod_obs({"status": "Pending"}))
        assert len(pool.calls) == 1
        assert _edge_inserts(pool) == []


# ── link_incident ─────────────────────────────────────────────────────────────

class TestLinkIncident:
    async def test_creates_incident_entity_and_edge(self, pool):
        pod_id, incident_id = uuid.uuid4(), uuid.uuid4()
        pool.fetchrow_results = [
            {"id": pod_id},       # upsert Pod
            {"id": incident_id},  # upsert Incident
            None,                 # open_edge existence probe
        ]
        await kg.link_incident("c1", "default", "web-1", "ep-42", source_id="ep-42")

        _, _, incident_args = pool.calls[1]
        assert incident_args[:4] == ("c1", "Incident", "ep-42", "")
        inserts = _edge_inserts(pool)
        assert len(inserts) == 1
        args = inserts[0][2]
        assert args[:4] == ("c1", str(pod_id), "crashed_with", str(incident_id))
        assert args[5:] == ("episode", "ep-42", None)  # P2: trailing valid_from param


# ── changes (time-travel diff) ────────────────────────────────────────────────

T0 = datetime(2026, 6, 12, 14, 0, 0, tzinfo=timezone.utc)


def _edge_row(valid_from, valid_to, rel="runs_on", attrs='{"k": 1}'):
    return {
        "rel": rel, "attrs": attrs, "valid_from": valid_from, "valid_to": valid_to,
        "src_kind": "Pod", "src_ns": "s", "src_name": "web-1",
        "dst_kind": "Node", "dst_ns": "", "dst_name": "worker-2",
    }


class TestChanges:
    async def test_opened_and_closed_rows_from_one_edge(self, pool):
        pool.fetch_results = [[
            _edge_row(T0 + timedelta(minutes=2, seconds=11), T0 + timedelta(minutes=5)),
        ]]
        out = await kg.changes("c1", T0.timestamp(), (T0 + timedelta(minutes=10)).timestamp())
        assert [c["change"] for c in out] == ["opened", "closed"]
        opened, closed = out
        assert opened["at"] == (T0 + timedelta(minutes=2, seconds=11)).timestamp()
        assert closed["at"] == (T0 + timedelta(minutes=5)).timestamp()
        for change in out:
            assert change["src"] == "Pod/s/web-1"
            assert change["rel"] == "runs_on"
            assert change["dst"] == "Node//worker-2"
            assert change["attrs"] == {"k": 1}  # JSONB-as-str parsed

    async def test_edge_opened_before_window_yields_only_closed(self, pool):
        pool.fetch_results = [[
            _edge_row(T0 - timedelta(hours=1), T0 + timedelta(minutes=3), rel="owns"),
        ]]
        out = await kg.changes("c1", T0.timestamp(), (T0 + timedelta(minutes=10)).timestamp())
        assert [c["change"] for c in out] == ["closed"]

    async def test_results_ordered_by_time(self, pool):
        pool.fetch_results = [[
            _edge_row(T0 + timedelta(minutes=2), T0 + timedelta(minutes=9)),
            _edge_row(T0 - timedelta(hours=1), T0 + timedelta(minutes=4), rel="owns"),
        ]]
        out = await kg.changes("c1", T0.timestamp(), (T0 + timedelta(minutes=10)).timestamp())
        assert [c["at"] for c in out] == sorted(c["at"] for c in out)
        assert [c["change"] for c in out] == ["opened", "closed", "closed"]

    async def test_window_params_are_tz_aware_datetimes(self, pool):
        await kg.changes("c1", T0.timestamp(), (T0 + timedelta(minutes=5)).timestamp())
        _, _, args = pool.calls[0]
        assert args[1] == T0 and args[1].tzinfo is timezone.utc
        assert args[2] == T0 + timedelta(minutes=5)

    async def test_without_pool_returns_empty(self):
        kg.close_kg()
        assert await kg.changes("c1", 0.0, 1.0) == []


# ── recent_changes_block ──────────────────────────────────────────────────────

def _change(at: datetime, i: int = 0) -> dict:
    return {
        "change": "opened", "at": at.timestamp(),
        "src": f"Pod/s/web-{i}", "rel": "runs_on", "dst": "Node//worker-2", "attrs": {},
    }


class TestRecentChangesBlock:
    async def test_formats_lines(self, pool, mocker):
        rows = [_change(datetime(2026, 6, 12, 14, 2, 11, tzinfo=timezone.utc), 1)]
        mocker.patch.object(kg, "changes", mocker.AsyncMock(return_value=rows))
        block = await kg.recent_changes_block("c1")
        assert block == "14:02:11 opened Pod/s/web-1 -runs_on-> Node//worker-2"

    async def test_caps_at_limit_with_more_tail(self, pool, mocker):
        rows = [_change(T0 + timedelta(seconds=i), i) for i in range(15)]
        mocker.patch.object(kg, "changes", mocker.AsyncMock(return_value=rows))
        block = await kg.recent_changes_block("c1", limit=12)
        lines = block.splitlines()
        assert len(lines) == 13
        assert lines[-1] == "(+3 more)"

    async def test_empty_when_no_changes(self, pool):
        assert await kg.recent_changes_block("c1") == ""

    async def test_queries_last_n_minutes(self, pool, mocker):
        spy = mocker.patch.object(kg, "changes", mocker.AsyncMock(return_value=[]))
        mocker.patch.object(kg.time, "time", return_value=1000000.0)
        await kg.recent_changes_block("c1", minutes=15)
        t1, t2 = spy.call_args.args[1:]
        assert t2 == 1000000.0 and t2 - t1 == 15 * 60


# ── Failure discipline — a raising pool must never leak an exception ─────────

class TestFailureDiscipline:
    @pytest.fixture(autouse=True)
    def _raising_pool(self):
        kg.init_kg(RaisingPool())
        yield
        kg.close_kg()

    async def test_upsert_entity_swallows(self):
        assert await kg.upsert_entity("c1", "Pod", "web-1") is None

    async def test_open_edge_swallows(self):
        assert await kg.open_edge("c1", "s", "runs_on", "d") is None

    async def test_close_edge_swallows(self):
        assert await kg.close_edge("c1", "s", "runs_on") is None

    async def test_ingest_pod_observation_swallows(self):
        obs = _pod_obs({"status": "Running", "node": "w", "owner": "ReplicaSet/x"})
        assert await kg.ingest_pod_observation(obs) is None

    async def test_link_incident_swallows(self):
        assert await kg.link_incident("c1", "default", "web-1", "ep-1") is None

    async def test_changes_swallows(self):
        assert await kg.changes("c1", 0.0, 1.0) == []

    async def test_recent_changes_block_swallows(self, mocker):
        mocker.patch.object(kg, "changes", mocker.AsyncMock(side_effect=RuntimeError("x")))
        assert await kg.recent_changes_block("c1") == ""
