"""Detector service — wires the sensorium watchers to a singleton engine.

Started from app lifespan when SENSORIUM_ENABLED. Degrades gracefully:
missing kubectl, RBAC failures, or watch errors disable perception but
never affect request handling (same discipline as the flight recorder).
"""
from __future__ import annotations

import asyncio

from app.detectors.engine import DetectorEngine, load_db_detectors, load_detectors
from app.utils.logger import get_logger

logger = get_logger(__name__)

_engine: DetectorEngine | None = None
_tasks: list[asyncio.Task] = []


def get_engine() -> DetectorEngine | None:
    return _engine


async def start_sensorium() -> None:
    global _engine
    from app.cluster_id import get_cluster_id
    from app.sensorium.k8s_watcher import start_watchers

    detectors = load_detectors()
    if not detectors:
        logger.info("sensorium: no compiled detectors — not starting")
        return
    try:
        cluster_id = get_cluster_id()
    except Exception:
        cluster_id = "unknown"

    from app.autonomy.watchtower import on_finding as watchtower_on_finding

    _engine = DetectorEngine(
        detectors=detectors, cluster_id=cluster_id, on_finding=watchtower_on_finding
    )
    loop = asyncio.get_running_loop()
    _tasks.append(loop.create_task(_engine.run()))

    from app.core.config import settings

    if settings.PREDICTIVE_DETECTION_ENABLED:
        interval = float(settings.PREDICTIVE_TREND_INTERVAL_SECONDS)
        _tasks.append(loop.create_task(_engine.run_trends(interval=interval)))
        logger.info(f"sensorium: predictive detection on (trend interval {interval:.0f}s)")

    if settings.NL_DETECTOR_AUTHORING_ENABLED:
        await _refresh_db_detectors(cluster_id)
        _tasks.append(loop.create_task(_db_refresh_loop(cluster_id)))

    from app.memory.service import enqueue_observation

    def _sink(obs):
        _engine.process(obs)
        enqueue_observation(obs)

    _tasks.extend(await start_watchers(cluster_id, _sink))
    logger.info(
        f"sensorium: started — {len(detectors)} compiled detectors, cluster={cluster_id}"
    )


async def _refresh_db_detectors(cluster_id: str) -> None:
    """Reload promoted (active) + shadow detectors from the DB into the engine.

    Active DB detectors are merged with the playbook-compiled ones; shadow
    detectors fire only into the candidate buffer (never the watchtower).
    """
    if _engine is None:
        return
    try:
        active, shadow = await load_db_detectors(cluster_id)
    except Exception as exc:
        logger.warning(f"sensorium: db-detector refresh failed: {exc}")
        return
    _engine.detectors = tuple(load_detectors()) + active
    _engine.shadow_detectors = shadow
    if active or shadow:
        logger.info(f"sensorium: db detectors — {len(active)} active, {len(shadow)} shadow")


async def _db_refresh_loop(cluster_id: str) -> None:
    from app.core.config import settings

    while True:
        await asyncio.sleep(float(settings.DB_DETECTOR_REFRESH_SECONDS))
        await _refresh_db_detectors(cluster_id)


async def stop_sensorium() -> None:
    global _engine
    for task in _tasks:
        task.cancel()
    _tasks.clear()
    _engine = None
