"""A server whose memory never started must not look like a cluster where nothing happened.

THE DEFECT
----------
`init_memory` caught every connection error, logged one `WARNING`, set `_pool = None` and
returned — **before starting any background task**. Nothing was left running that could try
again, so a pod scheduled before Postgres accepts connections (the ordinary rollout case) ran
for its entire life with no episodes, no knowledge graph, no consolidation, no preference
learning, no promotion and no prospective recheck. Only a restart could recover it.

Reproduced against a refused connect, with the healthy case as the control:

    memory_active()   False          vs  True
    background tasks  0              vs  3        ← nothing left to retry with
    observation queue None           vs  Queue    ← every observation dropped, silently
    /healthz          no memory field at all
    /v5/status        no memory field at all

An empty knowledge graph is indistinguishable from a cluster in which nothing has happened,
which makes this the most expensive silent failure available in this system: memory is the axis
the product is differentiated on.

WHAT IS ASSERTED
----------------
1. `memory_status()` distinguishes ready / flag / sqlite / unavailable and says why — in both
   directions, because a status that always reads "down" proves nothing.
2. A failed connect leaves a **reconnect loop** running, that loop wires the hierarchy up when
   Postgres arrives, and it does not linger once it has.
3. Discarded observations are counted — including the full-queue drop — and a queued one is not.
4. `/healthz` carries it, so the outage outlives the startup log.
"""

from __future__ import annotations

import asyncio

import pytest
from app.api.v1.endpoints.health import router as health_router
from app.core import readiness
from app.core.config import settings
from app.memory import service
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakePool:
    async def close(self): ...
    async def execute(self, *a, **k): return "INSERT 0 0"
    async def fetch(self, *a, **k): return []
    async def fetchrow(self, *a, **k): return None


class Obs:
    kind = "pod_status"


@pytest.fixture(autouse=True)
def _clean_module_state():
    saved = (service._pool, service._obs_queue, list(service._tasks),
             service._state, service._reason, service._dropped_observations)
    service._pool, service._obs_queue = None, None
    service._tasks.clear()
    service._state, service._reason, service._dropped_observations = "starting", "", 0
    yield
    for t in service._tasks:
        t.cancel()
    service._tasks.clear()
    (service._pool, service._obs_queue, tasks,
     service._state, service._reason, service._dropped_observations) = saved
    service._tasks.extend(tasks)


@pytest.fixture
def wired(mocker):
    """Postgres mode, with the L1/L2 initialisers and the consolidation workers stubbed out —
    this file is about the pool lifecycle, not about what the workers then do."""
    mocker.patch.object(settings, "USE_SQLITE", False)
    mocker.patch.object(settings, "MEMORY_HIERARCHY_ENABLED", True)
    mocker.patch.object(service.episodes, "init_episodes", lambda p: None)
    mocker.patch.object(service.kg, "init_kg", lambda p: None)
    mocker.patch.object(service.preferences, "init_preferences", lambda p: None)
    import app.memory.consolidation as consolidation
    mocker.patch.object(consolidation, "run_consolidation_once", mocker.AsyncMock())
    mocker.patch.object(consolidation, "consolidation_loop", mocker.AsyncMock())
    mocker.patch.object(service, "_drain_observations", mocker.AsyncMock())


def _connect_fails(mocker):
    return mocker.patch.object(
        service.asyncpg, "create_pool",
        mocker.AsyncMock(side_effect=OSError("Connect call failed ('10.0.0.5', 5432)")),
    )


def _connect_works(mocker, pool=None):
    pool = pool or FakePool()
    mocker.patch.object(service.asyncpg, "create_pool", mocker.AsyncMock(return_value=pool))
    return pool


# ── 1. the state is askable ───────────────────────────────────────────────────
class TestTheMemoryStateIsAskable:
    @pytest.mark.asyncio
    async def test_a_failed_connect_reports_disabled_and_why(self, wired, mocker):
        _connect_fails(mocker)
        await service.init_memory()
        st = service.memory_status()
        assert st["enabled"] is False, "the hierarchy is dead and status says it is enabled"
        assert st["state"] == "unavailable"
        assert "10.0.0.5" in st["reason"], (
            "'unavailable' with no cause sends an operator back to a startup log "
            "that has already rotated"
        )

    @pytest.mark.asyncio
    async def test_a_live_hierarchy_reports_enabled(self, wired, mocker):
        """Vacuity guard: a status that always says 'down' asserts nothing about the failure."""
        _connect_works(mocker)
        await service.init_memory()
        st = service.memory_status()
        assert st["enabled"] is True
        assert st["state"] == "ready"
        assert st["reason"] == ""

    @pytest.mark.asyncio
    async def test_off_by_flag_is_not_an_outage(self, mocker):
        mocker.patch.object(settings, "MEMORY_HIERARCHY_ENABLED", False)
        await service.init_memory()
        st = service.memory_status()
        assert st["state"] == "flag" and st["state"] != "unavailable"
        assert "MEMORY_HIERARCHY_ENABLED" in st["reason"]

    @pytest.mark.asyncio
    async def test_sqlite_mode_is_not_an_outage(self, mocker):
        mocker.patch.object(settings, "MEMORY_HIERARCHY_ENABLED", True)
        mocker.patch.object(settings, "USE_SQLITE", True)
        await service.init_memory()
        st = service.memory_status()
        assert st["state"] == "sqlite" and st["state"] != "unavailable"
        assert "Postgres" in st["reason"]

    @pytest.mark.asyncio
    async def test_shutdown_stops_claiming_enabled(self, wired, mocker):
        _connect_works(mocker)
        await service.init_memory()
        assert service.memory_status()["enabled"] is True
        await service.close_memory()
        assert service.memory_status()["enabled"] is False, (
            "a torn-down hierarchy still reporting 'enabled' is the same lie, one lifecycle later"
        )


# ── 2. the outage is not permanent ────────────────────────────────────────────
class TestTheOutageIsNotPermanent:
    @pytest.mark.asyncio
    async def test_a_failed_connect_leaves_something_running_to_retry(self, wired, mocker):
        """The defect in one assertion: it used to return with zero tasks, so nothing could
        ever try again and only a restart recovered the hierarchy."""
        _connect_fails(mocker)
        await service.init_memory()
        assert len(service._tasks) == 1, (
            f"{len(service._tasks)} tasks after a failed connect — with none, the hierarchy "
            f"is dead for the life of the process"
        )
        assert not service._tasks[0].done()

    @pytest.mark.asyncio
    async def test_a_startup_race_heals_without_a_restart(self, wired, mocker):
        mocker.patch.object(service, "_RETRY_INTERVAL_S", 0.01)
        _connect_fails(mocker)
        await service.init_memory()
        assert service.memory_active() is False

        pool = _connect_works(mocker)
        await asyncio.sleep(0.1)

        assert service.memory_active() is True, "the reconnect loop never reconnected"
        assert service.memory_status()["state"] == "ready"
        assert service._obs_queue is not None, (
            "reconnecting must finish startup — a pool with no workers on it is still a "
            "hierarchy that ingests nothing"
        )
        assert service._pool is pool

    @pytest.mark.asyncio
    async def test_the_reconnect_loop_stops_once_it_succeeds(self, wired, mocker):
        mocker.patch.object(service, "_RETRY_INTERVAL_S", 0.01)
        _connect_fails(mocker)
        await service.init_memory()
        loop_task = service._tasks[0]
        _connect_works(mocker)
        await asyncio.sleep(0.1)
        assert loop_task.done(), "the retry loop kept running after the pool came back"

    @pytest.mark.asyncio
    async def test_a_successful_connect_starts_no_retry_loop(self, wired, mocker):
        """Vacuity guard for the test above: the loop must be a response to failure, not
        something that is always there."""
        _connect_works(mocker)
        await service.init_memory()
        assert len(service._tasks) == 3, (
            f"expected the 3 hierarchy workers, got {len(service._tasks)}"
        )

    @pytest.mark.asyncio
    async def test_sqlite_mode_never_dials_postgres(self, mocker):
        connect = mocker.patch.object(service.asyncpg, "create_pool", mocker.AsyncMock())
        mocker.patch.object(settings, "MEMORY_HIERARCHY_ENABLED", True)
        mocker.patch.object(settings, "USE_SQLITE", True)
        await service.init_memory()
        assert connect.call_count == 0
        assert service._tasks == [], "nothing should be retrying a connection that is not wanted"


# ── 3. discarded observations are counted ─────────────────────────────────────
class TestDiscardedObservationsAreCounted:
    @pytest.mark.asyncio
    async def test_observations_arriving_with_no_queue_are_counted(self, wired, mocker):
        _connect_fails(mocker)
        await service.init_memory()
        for _ in range(5):
            service.enqueue_observation(Obs())
        assert service.memory_status()["observations_dropped"] == 5, (
            "'the knowledge graph is empty' is a guess; the count is the fact that replaces it"
        )

    @pytest.mark.asyncio
    async def test_a_queued_observation_is_not_counted_as_dropped(self, wired, mocker):
        """Vacuity guard: a counter that only goes up measures traffic, not loss."""
        _connect_works(mocker)
        await service.init_memory()
        service.enqueue_observation(Obs())
        assert service._obs_queue.qsize() == 1
        assert service.memory_status()["observations_dropped"] == 0

    @pytest.mark.asyncio
    async def test_a_full_queue_drop_is_also_counted(self, wired, mocker):
        """It had a queue and was still lost — the same loss, and it used to be `pass`."""
        _connect_works(mocker)
        await service.init_memory()
        service._obs_queue = asyncio.Queue(maxsize=1)
        service.enqueue_observation(Obs())
        service.enqueue_observation(Obs())
        assert service.memory_status()["observations_dropped"] == 1

    @pytest.mark.asyncio
    async def test_the_silence_is_broken_at_least_once(self, wired, mocker, caplog):
        _connect_fails(mocker)
        await service.init_memory()
        caplog.clear()
        with caplog.at_level("WARNING"):
            service.enqueue_observation(Obs())
        assert any("observation(s) discarded" in r.message for r in caplog.records), (
            f"nothing warned that an observation was thrown away: "
            f"{[r.message for r in caplog.records]}"
        )

    @pytest.mark.asyncio
    async def test_the_warning_does_not_flood(self, wired, mocker, caplog):
        """Vacuity guard for the test above — the sensorium is a firehose, and a per-observation
        warning would be its own outage."""
        _connect_fails(mocker)
        await service.init_memory()
        caplog.clear()
        with caplog.at_level("WARNING"):
            for _ in range(50):
                service.enqueue_observation(Obs())
        drops = [r for r in caplog.records if "observation(s) discarded" in r.message]
        assert len(drops) == 1, f"50 discarded observations produced {len(drops)} warnings"


# ── 4. the probe carries it ───────────────────────────────────────────────────
@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(health_router)
    readiness.set_ready(True)
    return TestClient(app)


class TestHealthzCarriesIt:
    def test_healthz_reports_a_dead_hierarchy(self, client, mocker):
        mocker.patch.object(service, "_state", "unavailable")
        mocker.patch.object(service, "_reason", "Connect call failed ('10.0.0.5', 5432)")
        mocker.patch.object(service, "_dropped_observations", 8123)
        body = client.get("/healthz").json()
        assert body["memory"]["enabled"] is False, (
            "/healthz answered 'ok' for a server with no memory at all — the exact signal "
            "an operator uses to decide the deployment is healthy"
        )
        assert body["memory"]["observations_dropped"] == 8123
        assert "10.0.0.5" in body["memory"]["reason"]

    def test_healthz_reports_a_live_hierarchy(self, client, mocker):
        """Vacuity guard: the field must track the state, not be a constant."""
        mocker.patch.object(service, "_state", "ready")
        body = client.get("/healthz").json()
        assert body["memory"]["enabled"] is True
        assert body["status"] == "ok"

    def test_healthz_still_answers_200_while_memory_is_down(self, client, mocker):
        """Liveness must not follow the hierarchy: a pod with no memory is degraded, not
        wedged, and failing liveness would restart-loop it through a database outage."""
        mocker.patch.object(service, "_state", "unavailable")
        assert client.get("/healthz").status_code == 200
