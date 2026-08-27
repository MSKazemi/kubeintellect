"""An NL-authored detector must be evaluated by the cluster it was authored on.

The write path and the read path never agreed on which cluster a detector belongs to:

    app/detectors/authoring.py   stage_candidate(..., cluster_id="global")
    app/detectors/review.py      list/promote/demote_candidate(..., cluster_id="global")
    app/detectors/engine.py      load_db_detectors(cluster_id)   <- get_cluster_id()

So on any deployment that sets `CLUSTER_ID` — which the Helm chart does — an authored detector
was stored, listed as `shadow`, promotable and demotable, and never loaded and never evaluated.
The whole NL-authoring feature (ADR-012) was inert wherever it was actually deployed, and inert
in the only way that cannot be seen: the API kept answering about it.

Measured on the campaign's soak cluster 2026-08-25: 8 rows under `cluster_id='global'`, the
engine loading for `f3-shadow-soak-r2`, zero rows matched. A 24-hour shadow soak against that
cluster reported `false_positive_rate: 0.0` — a perfect score for evaluating nothing. Its driver
had a guard for exactly this and the guard passed, because the only thing the API exposed was the
DB status, which was correct.

These tests pin the query, not the response, because the response was never wrong.
"""
from __future__ import annotations

import json

import pytest

from app.detectors import engine as engine_mod


class _FakePool:
    """Records the SQL and the bound arguments, and answers from a fixed row set."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.sql: str = ""
        self.args: tuple = ()

    async def fetch(self, sql: str, *args):
        self.sql, self.args = sql, args
        # Emulate the WHERE clause the way Postgres would, so a test cannot pass by ignoring it.
        wanted = {args[0], "global"} if "'global'" in sql else {args[0]}
        return [r for r in self.rows
                if r["cluster_id"] in wanted and r["status"] in ("active", "shadow")]


CRASHLOOP = json.dumps({"watch_predicates": [
    {"kind": "Event", "reason_regex": "^BackOff$", "involved_kind": "Pod"}]})


def _row(name: str, cluster_id: str, status: str) -> dict:
    return {"name": name, "predicate": CRASHLOOP, "status": status, "cluster_id": cluster_id}


@pytest.fixture
def pool(monkeypatch):
    def _install(rows):
        from app.memory import service as mem_service
        p = _FakePool(rows)
        monkeypatch.setattr(mem_service, "_pool", p, raising=False)
        return p
    return _install


async def test_a_global_detector_loads_on_a_named_cluster(pool):
    """The case that was broken. `global` is the write path's word for "everywhere"."""
    p = pool([_row("nl:soak-crashloop", "global", "shadow")])
    active, shadow = await engine_mod.load_db_detectors("f3-shadow-soak-r2")
    assert [d.playbook for d in shadow] == ["nl:soak-crashloop"], (
        f"a detector authored through the API (which stores cluster_id='global') was not loaded "
        f"by the cluster running it. SQL was: {p.sql}"
    )
    assert active == ()


async def test_the_query_asks_for_both(pool):
    p = pool([])
    await engine_mod.load_db_detectors("some-cluster")
    assert "'global'" in p.sql, (
        "the reader must accept global rows; asking only for the deployment's own cluster id is "
        "what made every authored detector inert"
    )
    assert p.args[0] == "some-cluster"


async def test_a_detector_scoped_to_one_cluster_stays_there(pool):
    """Widening the read must not turn every detector into a fleet-wide one."""
    pool([_row("nl:only-here", "cluster-a", "shadow")])
    _active, shadow = await engine_mod.load_db_detectors("cluster-b")
    assert shadow == (), "a row scoped to cluster-a must not load on cluster-b"

    pool([_row("nl:only-here", "cluster-a", "shadow")])
    _active, shadow = await engine_mod.load_db_detectors("cluster-a")
    assert [d.playbook for d in shadow] == ["nl:only-here"]


async def test_active_and_shadow_stay_separate(pool):
    pool([_row("nl:promoted", "global", "active"),
          _row("nl:candidate", "global", "shadow")])
    active, shadow = await engine_mod.load_db_detectors("prod-1")
    assert [d.playbook for d in active] == ["nl:promoted"]
    assert [d.playbook for d in shadow] == ["nl:candidate"]


async def test_a_demoted_detector_is_still_not_loaded(pool):
    pool([_row("nl:rejected", "global", "demoted")])
    active, shadow = await engine_mod.load_db_detectors("prod-1")
    assert (active, shadow) == ((), ())


async def test_no_pool_is_still_no_detectors(pool, monkeypatch):
    from app.memory import service as mem_service
    monkeypatch.setattr(mem_service, "_pool", None, raising=False)
    assert await engine_mod.load_db_detectors("prod-1") == ((), ())
