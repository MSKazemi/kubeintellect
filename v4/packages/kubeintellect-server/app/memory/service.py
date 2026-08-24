"""Memory service — pool lifecycle, observation fan-in, consolidation schedule.

Owns the asyncpg pool shared by episodes (L1) and the knowledge graph (L2).
Observations from the sensorium arrive via enqueue_observation() (sync,
non-blocking, called from the watcher sink) and are drained into KG ingestion
by a background worker — the same fire-and-forget discipline as the flight
recorder.

WHY THE POOL RETRIES, AND WHY THE OUTAGE IS REPORTED
----------------------------------------------------
A failed connect at startup is the *expected* case on Kubernetes — the API pod is routinely
scheduled before Postgres accepts connections. `init_memory` used to log one WARNING, set
`_pool = None` and return **before starting any background task**, so nothing was left running
that could ever try again: episodes, the knowledge graph, consolidation, preference learning,
promotion and prospective recheck were all dead for the life of the process, and only a restart
could bring them back. Nothing said so afterwards — `/healthz`, `/v5/status` and `kubeintellect
status` reported no memory state at all, `enqueue_observation` dropped every observation
silently, and an empty knowledge graph looks exactly like a cluster nothing has happened in.
That is the worst place in this system for a silent failure: memory is the axis the product is
differentiated on.

So the reconnect loop below is the piece that was missing, and `memory_status()` is the
machine-readable answer, surfaced on `/healthz` next to `leader` and `audit`.
"""
from __future__ import annotations

import asyncio

import asyncpg

from app.core.config import settings
from app.memory import episodes, kg, preferences
from app.utils.logger import get_logger

logger = get_logger(__name__)

_pool: asyncpg.Pool | None = None
_obs_queue: asyncio.Queue | None = None
_tasks: list[asyncio.Task] = []

# "starting" until init runs; "flag"/"sqlite" when the hierarchy is off by configuration;
# "ready" once the pool is live; "unavailable" when Postgres refused us and we are retrying.
_state: str = "starting"
_reason: str = ""
_dropped_observations: int = 0

#: Seconds between reconnect attempts while the pool is down.
_RETRY_INTERVAL_S = 30.0


def memory_active() -> bool:
    return _pool is not None


def memory_status() -> dict:
    """Shape reported on ``/healthz``. An operator must be able to see that memory is dead.

    ``observations_dropped`` is what turns "the knowledge graph is empty" from a guess into a
    fact — the sensorium keeps producing whether or not anything is there to receive.
    """
    return {
        "enabled": _state == "ready",
        "state": _state,
        "reason": _reason,
        "observations_dropped": _dropped_observations,
    }


async def init_memory() -> None:
    """Create the pool, init L1/L2, backfill, start workers. Never raises."""
    global _state, _reason
    if not settings.MEMORY_HIERARCHY_ENABLED:
        _state, _reason = "flag", "MEMORY_HIERARCHY_ENABLED=false"
        logger.info("memory: disabled by flag")
        return
    if settings.USE_SQLITE:
        _state, _reason = "sqlite", "USE_SQLITE=true — hierarchy needs Postgres"
        logger.info("memory: SQLite mode — hierarchy disabled")
        return
    if await _try_connect():
        return
    # Not fatal and, crucially, not final: keep one task alive whose only job is to try again.
    _tasks.append(asyncio.get_running_loop().create_task(_reconnect_loop()))


async def _try_connect() -> bool:
    """One connect attempt. On success the hierarchy is wired up; never raises."""
    global _pool, _state, _reason
    try:
        _pool = await asyncpg.create_pool(
            settings.POSTGRES_DSN, min_size=1, max_size=4, command_timeout=5
        )
    except Exception as exc:
        _pool = None
        _state, _reason = "unavailable", str(exc)
        logger.warning(
            f"memory: could not connect — hierarchy is NOT running (no episodes, no knowledge "
            f"graph, no consolidation); retrying every {_RETRY_INTERVAL_S:.0f}s ({exc})"
        )
        return False
    _activate(_pool)
    return True


def _activate(pool: asyncpg.Pool) -> None:
    """Wire the hierarchy onto a live pool. Split out of init_memory so the reconnect loop
    finishes startup properly rather than leaving a pool with no workers on it."""
    global _obs_queue, _state, _reason
    episodes.init_episodes(pool)
    kg.init_kg(pool)
    preferences.init_preferences(pool)
    _obs_queue = asyncio.Queue(maxsize=10_000)

    loop = asyncio.get_running_loop()
    _tasks.append(loop.create_task(_drain_observations()))

    from app.memory.consolidation import consolidation_loop, run_consolidation_once

    # One-shot startup pass: rca_outcomes backfill + initial maintenance.
    _tasks.append(loop.create_task(run_consolidation_once(startup=True)))
    _tasks.append(loop.create_task(consolidation_loop()))
    _state, _reason = "ready", ""
    logger.info("memory: hierarchy active (L1 episodes, L2 kg, consolidation)")


async def _reconnect_loop() -> None:
    """Retry until Postgres accepts us, then stop. A rollout race must not cost a restart."""
    while True:
        try:
            await asyncio.sleep(_RETRY_INTERVAL_S)
            if await _try_connect():
                logger.info(
                    f"memory: hierarchy recovered after {_dropped_observations} dropped "
                    f"observation(s)"
                )
                return
        except asyncio.CancelledError:
            break
        except Exception as exc:      # the retry loop itself must never die
            logger.warning(f"memory: reconnect attempt failed: {exc}")


async def close_memory() -> None:
    global _pool, _obs_queue, _state, _reason
    _state, _reason = "starting", ""
    for task in _tasks:
        task.cancel()
    _tasks.clear()
    episodes.close_episodes()
    kg.close_kg()
    preferences.close_preferences()
    if _pool:
        await _pool.close()
        _pool = None
    _obs_queue = None


def enqueue_observation(obs) -> None:
    """Sensorium sink hook — sync, non-blocking, never raises."""
    global _dropped_observations
    if _obs_queue is None:
        # No queue means the hierarchy never started. Counting is the whole point: an empty
        # knowledge graph is otherwise indistinguishable from a cluster where nothing happened.
        _dropped_observations += 1
        if _dropped_observations == 1 or _dropped_observations % 1000 == 0:
            logger.warning(
                f"memory: {_dropped_observations} observation(s) discarded — {_state}: {_reason}"
            )
        return
    try:
        _obs_queue.put_nowait(obs)
    except Exception:
        # full queue — drop; the graph self-heals from later observations, but the loss is
        # still a loss and belongs in the same count.
        _dropped_observations += 1


async def _drain_observations() -> None:
    assert _obs_queue is not None
    while True:
        try:
            obs = await _obs_queue.get()
            if obs.kind == "pod_status":
                await kg.ingest_pod_observation(obs)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"memory: observation ingest error: {exc}")
