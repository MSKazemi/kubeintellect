"""Per-change ephemeral watchdog (v5 P4) — scheduler + TTL + dedup + dispatch."""
from __future__ import annotations

import pytest

from app.cortex.change_rca import ChangeRecord
from app.sensorium.change_watchdog import (
    WatchdogTask,
    _log_dispatch,
    fire,
    plan_watchdogs,
    set_dispatch,
)


def _c(target, kind="image", ns="demo", ts=100.0):
    return ChangeRecord(kind=kind, target=target, ts_epoch=ts, namespace=ns)


@pytest.fixture(autouse=True)
def reset_dispatch():
    set_dispatch(_log_dispatch)
    yield
    set_dispatch(_log_dispatch)


class TestPlan:
    def test_one_watchdog_per_change_bounded(self):
        changes = [_c(f"deploy/d{i}", ts=100 + i) for i in range(8)]
        tasks = plan_watchdogs(changes, now_epoch=200, max_active=3)
        assert len(tasks) == 3
        # most recent first
        assert tasks[0].target == "deploy/d7"

    def test_objective_reads_the_change(self):
        tasks = plan_watchdogs([_c("deploy/web")], now_epoch=200)
        assert "deploy/web" in tasks[0].objective and "degrade health" in tasks[0].objective

    def test_dedup_by_target(self):
        seen = set()
        t1 = plan_watchdogs([_c("deploy/web", ts=100)], now_epoch=200, seen=seen)
        t2 = plan_watchdogs([_c("deploy/web", ts=101)], now_epoch=201, seen=seen)  # same target
        assert len(t1) == 1 and len(t2) == 0

    def test_since_filter(self):
        changes = [_c("deploy/old", ts=50), _c("deploy/new", ts=150)]
        tasks = plan_watchdogs(changes, now_epoch=200, since_epoch=100)
        assert [t.target for t in tasks] == ["deploy/new"]


class TestTtl:
    def test_expired(self):
        t = WatchdogTask("x", "image", "obj", created_epoch=100, ttl_seconds=300, dedup_key="k")
        assert t.expired(now_epoch=350) is False
        assert t.expired(now_epoch=500) is True


class TestFire:
    def test_dispatches_each(self):
        fired = []
        set_dispatch(lambda t: fired.append(t.target))
        n = fire(plan_watchdogs([_c("deploy/a"), _c("deploy/b")], now_epoch=200))
        assert n == 2 and set(fired) == {"deploy/a", "deploy/b"}

    def test_dispatch_error_is_isolated(self):
        def boom(t):
            raise RuntimeError("investigation failed to launch")
        set_dispatch(boom)
        # one failing dispatch must not raise out of fire()
        assert fire(plan_watchdogs([_c("deploy/a")], now_epoch=200)) == 0


class TestConsolidationSweep:
    async def test_sweep_fires_then_dedups(self, mocker):
        from app.cortex import change_rca
        from app.cortex.change_rca import ChangeRecord
        from app.memory import consolidation as cons
        from app.sensorium import change_watchdog as cw

        cons._last_watchdog_sweep = 0.0
        change_rca.set_change_source(lambda cid, ns=None: [ChangeRecord("image", "deploy/web", 100.0, "demo")])
        mocker.patch("app.sensorium.watchdog_dispatch.install")   # don't register the real fan-out dispatch
        fired = []
        cw.set_dispatch(lambda t: fired.append(t.target))
        try:
            n1 = await cons._sweep_change_watchdogs()
            assert n1 == 1 and fired == ["deploy/web"]
            # second sweep: since_epoch is now (wallclock) >> change ts=100 ⇒ nothing new
            n2 = await cons._sweep_change_watchdogs()
            assert n2 == 0
        finally:
            change_rca.set_change_source(change_rca._empty_source)
            cw.set_dispatch(cw._log_dispatch)
            cons._last_watchdog_sweep = 0.0
