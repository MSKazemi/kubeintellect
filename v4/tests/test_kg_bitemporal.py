"""Memory V5 P2 (ADR-013) — bi-temporal KG: retract, as_of, current_edges.

The failure discipline (memory never breaks a request) and the read projections
are exercised with a fake pool; the SQL itself is validated separately (sqlglot)
and against a live Postgres at the P2 ship-gate.
"""
from __future__ import annotations

from app.memory import kg


class FakePool:
    def __init__(self, rows=None, row=None, execute_result="UPDATE 0", fetchrow_results=None):
        self.rows = rows or []
        self.row = row
        self.execute_result = execute_result
        self.fetchrow_results = list(fetchrow_results) if fetchrow_results else None
        self.calls: list[tuple] = []
        self.raise_on = None

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        if self.raise_on == "fetch":
            raise RuntimeError("db down")
        return self.rows

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        if self.raise_on == "fetchrow":
            raise RuntimeError("db down")
        if self.fetchrow_results is not None:          # sequential lookups (reconcile)
            return self.fetchrow_results.pop(0) if self.fetchrow_results else None
        return self.row

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        if self.raise_on == "execute":
            raise RuntimeError("db down")
        return self.execute_result


def _edge_row(rel="runs_on", attrs=None):
    return {
        "rel": rel, "attrs": attrs or {"note": "x"},
        "src_kind": "Pod", "src_ns": "payments", "src_name": "web-1",
        "dst_kind": "Node", "dst_ns": "", "dst_name": "worker-2",
    }


class TestRetractEdge:
    async def test_returns_rows_retracted(self):
        pool = FakePool(execute_result="UPDATE 2")
        kg.init_kg(pool)
        try:
            n = await kg.retract_edge("c1", "src-id", "runs_on", "dst-id")
            assert n == 2
            # it must set retracted_at, never DELETE (review F4 / R1.2)
            _, sql, _ = pool.calls[0]
            assert "retracted_at" in sql and "DELETE" not in sql.upper()
        finally:
            kg.close_kg()

    async def test_dst_optional_filter(self):
        pool = FakePool(execute_result="UPDATE 1")
        kg.init_kg(pool)
        try:
            await kg.retract_edge("c1", "src-id", "runs_on")  # no dst
            _, sql, args = pool.calls[0]
            assert "dst" not in sql and args == ("c1", "src-id", "runs_on")
        finally:
            kg.close_kg()

    async def test_never_raises(self):
        pool = FakePool()
        pool.raise_on = "execute"
        kg.init_kg(pool)
        try:
            assert await kg.retract_edge("c1", "s", "runs_on") == 0
        finally:
            kg.close_kg()

    async def test_uninitialised_returns_zero(self):
        kg.close_kg()
        assert await kg.retract_edge("c1", "s", "runs_on") == 0


class TestAsOf:
    async def test_projects_edges(self):
        pool = FakePool(rows=[_edge_row(), _edge_row(rel="owns")])
        kg.init_kg(pool)
        try:
            out = await kg.as_of("c1", valid_t=1000.0, tx_t=2000.0)
            assert len(out) == 2
            assert out[0]["src"] == "Pod/payments/web-1"
            assert out[0]["dst"] == "Node//worker-2"
            assert {e["rel"] for e in out} == {"runs_on", "owns"}
            # both temporal axes must be bounded in the query
            _, sql, _ = pool.calls[0]
            assert "valid_from" in sql and "ingested_at" in sql and "retracted_at" in sql
        finally:
            kg.close_kg()

    async def test_tx_defaults_to_now(self):
        pool = FakePool(rows=[_edge_row()])
        kg.init_kg(pool)
        try:
            out = await kg.as_of("c1", valid_t=1000.0)  # tx_t omitted
            assert len(out) == 1
        finally:
            kg.close_kg()

    async def test_never_raises(self):
        pool = FakePool()
        pool.raise_on = "fetch"
        kg.init_kg(pool)
        try:
            assert await kg.as_of("c1", valid_t=1.0) == []
        finally:
            kg.close_kg()

    async def test_uninitialised_returns_empty(self):
        kg.close_kg()
        assert await kg.as_of("c1", valid_t=1.0) == []


class TestCurrentEdges:
    async def test_default_belief_filter(self):
        pool = FakePool(rows=[_edge_row()])
        kg.init_kg(pool)
        try:
            out = await kg.current_edges("c1")
            assert len(out) == 1 and out[0]["rel"] == "runs_on"
            _, sql, _ = pool.calls[0]
            # S1 default = currently true AND currently believed
            assert "valid_to IS NULL" in sql and "retracted_at IS NULL" in sql
        finally:
            kg.close_kg()

    async def test_never_raises(self):
        pool = FakePool()
        pool.raise_on = "fetch"
        kg.init_kg(pool)
        try:
            assert await kg.current_edges("c1") == []
        finally:
            kg.close_kg()


class TestWritePathEventTime:
    async def test_open_edge_stamps_event_time_when_flag_on(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_BITEMPORAL_ENABLED", True)
        pool = FakePool(row=None)  # no existing open edge → proceeds to INSERT
        kg.init_kg(pool)
        try:
            await kg.open_edge("c1", "s", "runs_on", "d", event_time=1000.0)
            insert = next(c for c in pool.calls if c[0] == "execute")
            # last INSERT arg is valid_from = event-time datetime (review F3)
            assert insert[2][-1] == kg._ts(1000.0)
        finally:
            kg.close_kg()

    async def test_open_edge_falls_back_to_now_when_flag_off(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_BITEMPORAL_ENABLED", False)
        pool = FakePool(row=None)
        kg.init_kg(pool)
        try:
            await kg.open_edge("c1", "s", "runs_on", "d", event_time=1000.0)
            insert = next(c for c in pool.calls if c[0] == "execute")
            # flag off ⇒ valid_from param is None ⇒ SQL COALESCE → now() (pre-P2 behaviour)
            assert insert[2][-1] is None
        finally:
            kg.close_kg()

    async def test_close_edge_uses_event_time_when_flag_on(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_BITEMPORAL_ENABLED", True)
        pool = FakePool()
        kg.init_kg(pool)
        try:
            await kg.close_edge("c1", "s", "runs_on", event_time=1000.0)
            _, _, args = pool.calls[0]
            assert args[-1] == kg._ts(1000.0)
        finally:
            kg.close_kg()


def _ppr_edge(src, dst, rel="depends_on"):
    return {
        "src": src, "dst": dst, "rel": rel,
        "src_kind": "Svc", "src_ns": "prod", "src_name": src,
        "dst_kind": "Svc", "dst_ns": "prod", "dst_name": dst,
    }


class TestPPRBlastRadius:
    # Graph: S-A, S-B, A-C, B-C, C-D  (seed = S). A/B are 1-hop, C 2-hop (two paths), D 3-hop.
    ROWS = [_ppr_edge("S", "A"), _ppr_edge("S", "B"),
            _ppr_edge("A", "C"), _ppr_edge("B", "C"), _ppr_edge("C", "D")]

    async def test_ranks_neighbours_and_excludes_seed(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_KG_PPR", True)
        pool = FakePool(rows=self.ROWS)
        kg.init_kg(pool)
        try:
            out = await kg.ppr_blast_radius("c1", ["S"], top_k=10)
            names = [r["entity"].split("/")[-1] for r in out]
            assert "S" not in names                       # seed excluded
            assert set(names) == {"A", "B", "C", "D"}     # rest of the blast radius
            scores = {r["entity"].split("/")[-1]: r["score"] for r in out}
            assert scores["A"] > scores["D"]              # nearer ⇒ higher PPR
            assert out == sorted(out, key=lambda r: r["score"], reverse=True)  # ranked
        finally:
            kg.close_kg()

    async def test_top_k_caps_results(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_KG_PPR", True)
        pool = FakePool(rows=self.ROWS)
        kg.init_kg(pool)
        try:
            out = await kg.ppr_blast_radius("c1", ["S"], top_k=2)
            assert len(out) == 2
        finally:
            kg.close_kg()

    async def test_returns_empty_when_flag_off(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_KG_PPR", False)
        pool = FakePool(rows=self.ROWS)
        kg.init_kg(pool)
        try:
            assert await kg.ppr_blast_radius("c1", ["S"]) == []
            assert pool.calls == []  # short-circuits without querying
        finally:
            kg.close_kg()

    async def test_no_seeds_returns_empty(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_KG_PPR", True)
        pool = FakePool(rows=self.ROWS)
        kg.init_kg(pool)
        try:
            assert await kg.ppr_blast_radius("c1", []) == []
        finally:
            kg.close_kg()

    async def test_never_raises(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_KG_PPR", True)
        pool = FakePool()
        pool.raise_on = "fetch"
        kg.init_kg(pool)
        try:
            assert await kg.ppr_blast_radius("c1", ["S"]) == []
        finally:
            kg.close_kg()

    def test_power_iteration_direct(self):
        # pure-function check: closer nodes outrank farther ones from the seed
        scores = kg._power_iteration_ppr(
            [("S", "A"), ("A", "B"), ("B", "C")], seeds={"S"}
        )
        assert scores["A"] > scores["C"]


class TestSalienceScore:
    def test_high_value_relation_outranks_structural(self):
        assert kg._salience_score("crashed_with", None) > kg._salience_score("owns", None)

    def test_severity_and_verified_bump(self):
        base = kg._salience_score("runs_on", None)
        assert kg._salience_score("runs_on", {"severity": "critical"}) > base
        assert kg._salience_score("runs_on", {"verified": True}) > base

    def test_bounded_0_1(self):
        s = kg._salience_score("crashed_with", {"severity": "critical", "verified": True})
        assert 0.0 <= s <= 1.0


class TestReconcileEdge:
    async def test_flag_off_behaves_like_open_edge_add(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_WRITE_RECONCILE", False)
        pool = FakePool(row=None)  # open_edge existence probe → None ⇒ INSERT
        kg.init_kg(pool)
        try:
            assert await kg.reconcile_edge("c1", "s", "owns", "d") == "ADD"
            assert any(c[0] == "execute" and "INSERT INTO kg_edges" in c[1] for c in pool.calls)
        finally:
            kg.close_kg()

    async def test_low_salience_is_noop(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_WRITE_RECONCILE", True)
        pool = FakePool()
        kg.init_kg(pool)
        try:
            assert await kg.reconcile_edge("c1", "s", "owns", "d", salience=0.05) == "NOOP"
            assert pool.calls == []  # gated before any DB work
        finally:
            kg.close_kg()

    async def test_new_edge_is_add(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_WRITE_RECONCILE", True)
        # exact-match probe → None, then open_edge existence probe → None ⇒ INSERT
        pool = FakePool(fetchrow_results=[None, None])
        kg.init_kg(pool)
        try:
            assert await kg.reconcile_edge("c1", "s", "owns", "d") == "ADD"
        finally:
            kg.close_kg()

    async def test_redundant_reassertion_is_noop(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_WRITE_RECONCILE", True)
        pool = FakePool(fetchrow_results=[{"id": "e1", "attrs": {"w": 1}}])
        kg.init_kg(pool)
        try:
            # same attrs already present ⇒ NOOP, no UPDATE issued
            assert await kg.reconcile_edge("c1", "s", "owns", "d", {"w": 1}) == "NOOP"
            assert not any(c[0] == "execute" for c in pool.calls)
        finally:
            kg.close_kg()

    async def test_changed_attrs_is_update(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_WRITE_RECONCILE", True)
        pool = FakePool(fetchrow_results=[{"id": "e1", "attrs": {"w": 1}}])
        kg.init_kg(pool)
        try:
            assert await kg.reconcile_edge("c1", "s", "owns", "d", {"w": 2}) == "UPDATE"
            assert any(c[0] == "execute" and "UPDATE kg_edges SET attrs" in c[1] for c in pool.calls)
        finally:
            kg.close_kg()

    async def test_functional_contradiction_high_salience_retracts(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_WRITE_RECONCILE", True)
        # exact→None; functional 'other' open edge points at a DIFFERENT dst; open_edge probe→None
        pool = FakePool(fetchrow_results=[None, {"dst": "node-B"}, None])
        kg.init_kg(pool)
        try:
            decision = await kg.reconcile_edge(
                "c1", "pod-1", "runs_on", "node-A", salience=0.9
            )
            assert decision == "RETRACT"
            # a retract (retracted_at, not DELETE) and then the new INSERT were issued
            assert any(c[0] == "execute" and "retracted_at" in c[1] for c in pool.calls)
            assert any(c[0] == "execute" and "INSERT INTO kg_edges" in c[1] for c in pool.calls)
        finally:
            kg.close_kg()

    async def test_functional_contradiction_low_salience_defaults_to_add(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_WRITE_RECONCILE", True)
        # runs_on baseline salience 0.4 < _RETRACT_FLOOR ⇒ no supersede, plain ADD
        pool = FakePool(fetchrow_results=[None, None])
        kg.init_kg(pool)
        try:
            assert await kg.reconcile_edge("c1", "pod-1", "runs_on", "node-A") == "ADD"
            assert not any(c[0] == "execute" and "retracted_at" in c[1] for c in pool.calls)
        finally:
            kg.close_kg()

    async def test_never_raises(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_WRITE_RECONCILE", True)
        pool = FakePool()
        pool.raise_on = "fetchrow"
        kg.init_kg(pool)
        try:
            assert await kg.reconcile_edge("c1", "s", "owns", "d") == "NOOP"
        finally:
            kg.close_kg()


class TestIngestLag:
    async def test_returns_none_when_flag_off(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_BITEMPORAL_ENABLED", False)
        pool = FakePool(row={"lag": 3.5})
        kg.init_kg(pool)
        try:
            assert await kg.mean_ingest_lag_seconds("c1") is None
            assert pool.calls == []  # short-circuits without querying
        finally:
            kg.close_kg()

    async def test_returns_lag_when_flag_on(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_BITEMPORAL_ENABLED", True)
        pool = FakePool(row={"lag": 3.5})
        kg.init_kg(pool)
        try:
            assert await kg.mean_ingest_lag_seconds("c1") == 3.5
        finally:
            kg.close_kg()

    async def test_never_raises(self, mocker):
        mocker.patch.object(kg.settings, "MEMORY_BITEMPORAL_ENABLED", True)
        pool = FakePool()
        pool.raise_on = "fetchrow"
        kg.init_kg(pool)
        try:
            assert await kg.mean_ingest_lag_seconds("c1") is None
        finally:
            kg.close_kg()
