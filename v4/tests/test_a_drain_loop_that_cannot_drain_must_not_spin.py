"""A memory hierarchy that started and then died must not report itself healthy — or spin.

THE DEFECT
----------
`_drain_observations` was an unbounded `while True` whose except arm logged and immediately
retried:

    while True:
        try:
            obs = await _obs_queue.get()
            ...
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"memory: observation ingest error: {exc}")   # then loop, at once

When the failure is *persistent* rather than incidental, that arm cannot recover and cannot stop.
Observed in the 2026-08-24 campaign: `_activate()` appends to `_tasks` without cancelling the
previous ones, so a second activation (the reconnect path) left the FIRST drain task alive while
rebinding the `_obs_queue` global. The old task then awaited a queue created on another loop:

    memory: observation ingest error: <Queue at 0x... maxsize=10000> is bound to a different
    event loop

which repeated **16,690,848 times**, grew one run log to 3 GB, and burned a core — while
`memory_status()` still said `state="ready"`, because nothing in the drain arm ever touches
`_state`. Every observation was being discarded and `/healthz` reported memory healthy.

That is the same class the startup case already guards (see
test_a_dead_memory_hierarchy_reports_itself) but at the other end of the lifecycle: there the
hierarchy never started, here it started and then stopped working.

WHAT IS ASSERTED
----------------
1. A persistent drain failure is COUNTED and SURFACED — `memory_status()` must stop claiming the
   hierarchy is healthy once ingest is reliably failing.
2. The drain loop does NOT spin on a persistent failure — bounded attempts, not thousands.
3. Activating twice does not leave two drain tasks fighting over one queue (the root cause).
4. The healthy path is unaffected — an observation still reaches the knowledge graph, and a
   single incidental error does not condemn the hierarchy. Without this, a fix that simply
   stops draining would pass 1-3.
"""

from __future__ import annotations

import asyncio

import pytest
from app.memory import service


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
    yield
    for t in service._tasks:
        t.cancel()
    service._tasks.clear()
    (service._pool, service._obs_queue, tasks,
     service._state, service._reason, service._dropped_observations) = saved
    service._tasks.extend(tasks)


def _silence_activate_extras(monkeypatch):
    """_activate also starts consolidation; this test is about the drain loop only."""
    async def _noop(*a, **k): return None
    monkeypatch.setattr(service.episodes, "init_episodes", lambda pool: None)
    monkeypatch.setattr(service.kg, "init_kg", lambda pool: None)
    monkeypatch.setattr(service.preferences, "init_preferences", lambda pool: None)
    import app.memory.consolidation as consolidation
    monkeypatch.setattr(consolidation, "run_consolidation_once", _noop)
    monkeypatch.setattr(consolidation, "consolidation_loop", _noop)


@pytest.mark.asyncio
async def test_a_persistent_ingest_failure_is_surfaced_not_swallowed(monkeypatch):
    """The campaign's exact failure: ingest fails every time, health must stop saying ready."""
    _silence_activate_extras(monkeypatch)

    async def _always_fails(obs):
        raise RuntimeError("<Queue at 0x0 maxsize=10000> is bound to a different event loop")

    monkeypatch.setattr(service.kg, "ingest_pod_observation", _always_fails)
    service._activate(FakePool())
    assert service.memory_status()["state"] == "ready"

    for _ in range(50):
        service.enqueue_observation(Obs())
    await asyncio.sleep(0.3)

    status = service.memory_status()
    assert status["state"] != "ready", (
        "ingest has failed persistently, yet memory_status() still reports 'ready' — "
        "/healthz is lying about a dead memory path exactly as it did in the campaign"
    )
    assert status.get("ingest_failures", 0) > 0, "persistent ingest failure is not counted"


@pytest.mark.asyncio
async def test_the_drain_loop_does_not_spin_on_a_persistent_failure(monkeypatch):
    """16,690,848 log lines and 3 GB came from retrying instantly, forever."""
    _silence_activate_extras(monkeypatch)

    attempts = 0

    async def _always_fails(obs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("bound to a different event loop")

    monkeypatch.setattr(service.kg, "ingest_pod_observation", _always_fails)
    service._activate(FakePool())
    for _ in range(500):
        service.enqueue_observation(Obs())

    await asyncio.sleep(0.4)
    assert attempts < 100, (
        f"drain loop attempted ingest {attempts} times in 0.4s — it is spinning on a failure "
        "it cannot recover from, which is what produced the 3 GB log"
    )


@pytest.mark.asyncio
async def test_activating_twice_does_not_leave_two_drain_tasks(monkeypatch):
    """The root cause: _activate appended tasks without cancelling the previous ones."""
    _silence_activate_extras(monkeypatch)
    monkeypatch.setattr(service.kg, "ingest_pod_observation", lambda obs: asyncio.sleep(0))

    service._activate(FakePool())
    first = [t for t in service._tasks if not t.done()]
    service._activate(FakePool())
    await asyncio.sleep(0.05)

    still_alive_from_first = [t for t in first if not t.done() and not t.cancelled()]
    assert not still_alive_from_first, (
        f"{len(still_alive_from_first)} task(s) from the first activation are still running "
        "after re-activation — they now await a queue bound to a different loop"
    )


@pytest.mark.asyncio
async def test_the_healthy_path_still_ingests_and_survives_one_bad_observation(monkeypatch):
    """Control: the fix must not 'stop the bleeding' by simply refusing to drain."""
    _silence_activate_extras(monkeypatch)

    seen, calls = [], {"n": 0}

    async def _flaky(obs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("one incidental failure")
        seen.append(obs)

    monkeypatch.setattr(service.kg, "ingest_pod_observation", _flaky)
    service._activate(FakePool())

    for _ in range(4):
        service.enqueue_observation(Obs())
    await asyncio.sleep(0.3)

    assert len(seen) >= 2, f"healthy observations stopped reaching the graph (got {len(seen)})"
    assert service.memory_status()["state"] == "ready", (
        "a single incidental ingest error condemned an otherwise healthy hierarchy"
    )
