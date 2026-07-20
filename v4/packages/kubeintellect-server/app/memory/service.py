"""Memory service — pool lifecycle, observation fan-in, consolidation schedule.

Owns the asyncpg pool shared by episodes (L1) and the knowledge graph (L2).
Observations from the sensorium arrive via enqueue_observation() (sync,
non-blocking, called from the watcher sink) and are drained into KG ingestion
by a background worker — the same fire-and-forget discipline as the flight
recorder.
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


def memory_active() -> bool:
    return _pool is not None


async def init_memory() -> None:
    """Create the pool, init L1/L2, backfill, start workers. Never raises."""
    global _pool, _obs_queue
    if not settings.MEMORY_HIERARCHY_ENABLED:
        logger.info("memory: disabled by flag")
        return
    if settings.USE_SQLITE:
        logger.info("memory: SQLite mode — hierarchy disabled")
        return
    try:
        _pool = await asyncpg.create_pool(
            settings.POSTGRES_DSN, min_size=1, max_size=4, command_timeout=5
        )
    except Exception as exc:
        logger.warning(f"memory: could not connect — hierarchy disabled ({exc})")
        _pool = None
        return

    episodes.init_episodes(_pool)
    kg.init_kg(_pool)
    preferences.init_preferences(_pool)
    _obs_queue = asyncio.Queue(maxsize=10_000)

    loop = asyncio.get_running_loop()
    _tasks.append(loop.create_task(_drain_observations()))

    from app.memory.consolidation import consolidation_loop, run_consolidation_once

    # One-shot startup pass: rca_outcomes backfill + initial maintenance.
    _tasks.append(loop.create_task(run_consolidation_once(startup=True)))
    _tasks.append(loop.create_task(consolidation_loop()))
    logger.info("memory: hierarchy active (L1 episodes, L2 kg, consolidation)")


async def close_memory() -> None:
    global _pool, _obs_queue
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
    if _obs_queue is None:
        return
    try:
        _obs_queue.put_nowait(obs)
    except Exception:
        pass  # full queue — drop; the graph self-heals from later observations


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
