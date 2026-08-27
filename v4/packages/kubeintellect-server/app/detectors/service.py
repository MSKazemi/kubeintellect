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
# DB-detector counts as of the last SUCCESSFUL refresh. Kept so a failed refresh can say what
# it is preserving, and so a set that drops to zero is reported rather than falling silent.
# `None` means "no refresh has completed in this process yet", which is NOT the same as
# "the last refresh found nothing". The difference is the whole point of the first log line
# below: a startup that loads zero detectors has to say so once, and a steady state of zero
# must not then repeat itself every refresh interval forever.
_last_db_counts: tuple[int, int] | None = None

# Why there is no engine. Four unrelated situations end at ``_engine is None`` and the
# perception surfaces used to describe all four with one sentence that named two causes as
# fact — so on a leader-election standby, and after a failed start, that sentence was simply
# false. Each path that can leave the engine absent records which one it was.
NOT_STARTED = "not_started"       # lifespan has not reached the sensorium yet
DISABLED_BY_FLAG = "disabled_by_flag"
NO_DETECTORS = "no_detectors"     # nothing compiled to watch with
START_FAILED = "start_failed"     # it tried and raised — an outage, not a configuration
STANDBY = "standby"               # another replica holds the singleton lock; normal
STOPPED = "stopped"               # shutting down
RUNNING = "running"

_absence: str = NOT_STARTED
_absence_detail: str = ""


def sensorium_absence() -> tuple[str, str]:
    """(reason, detail) for why the engine is absent — ``RUNNING`` when it is not."""
    return _absence, _absence_detail


def sensorium_status() -> dict:
    """Shape reported on ``/healthz``. An operator must be able to see that nothing is watching.

    `/healthz` already answers this for the audit log, the memory hierarchy, the flight recorder
    and the leader election — each added because an empty table is indistinguishable from a quiet
    cluster. Perception was the one subsystem missing from that list, and it is the one whose
    whole job is watching: until 2026-08-24 a sensorium in `START_FAILED` produced a `/healthz`
    of `status: ok` with no field mentioning perception at all, while `sensorium_absence()` had
    the cause recorded one call away. The reason was reachable on `/v1/findings` and in the
    digest — surfaces you consult once you already suspect something.

    `watching` is deliberately separate from `enabled`, and it is the one to alert on. An engine
    exists whether or not any `kubectl --watch` stream is connected, and `_watch_loop` returns
    permanently when kubectl is missing — so "running" can hold for the whole process lifetime
    while nothing is observed. Reporting only engine presence would have moved the same wrong
    answer up one level. `STANDBY` is the case that is *not* an outage: another replica holds the
    singleton lock and is watching, which is why the state constant is reported rather than a
    bare boolean.
    """
    reason, detail = _absence, _absence_detail
    if reason != RUNNING:
        return {"enabled": False, "state": reason, "reason": detail, "watching": False}
    try:
        from app.sensorium.k8s_watcher import any_stream_connected, stream_health

        watching = bool(stream_health()) and any_stream_connected()
    except Exception as exc:  # a liveness probe must never fail because a status read did
        return {"enabled": True, "state": RUNNING, "watching": False,
                "reason": f"stream health could not be read: {exc}"}
    return {
        "enabled": True,
        "state": RUNNING,
        "watching": watching,
        "reason": "" if watching else (
            "the detector engine is running but no kubectl watch stream is connected — "
            "nothing is being observed"
        ),
    }


def record_disabled_by_flag() -> None:
    global _absence, _absence_detail
    _absence, _absence_detail = DISABLED_BY_FLAG, "SENSORIUM_ENABLED=false"


def record_start_failure(exc: BaseException) -> None:
    """The sensorium raised on the way up. Called by the lifespan, which swallows the error so
    that perception failing never costs availability — but a swallowed start is still an outage
    and must not read as a configuration choice."""
    global _absence, _absence_detail
    _absence, _absence_detail = START_FAILED, str(exc)


def get_engine() -> DetectorEngine | None:
    return _engine


async def start_sensorium() -> None:
    global _engine
    from app.cluster_id import get_cluster_id
    from app.sensorium.k8s_watcher import start_watchers

    global _absence, _absence_detail
    detectors = load_detectors()
    if not detectors:
        _absence, _absence_detail = NO_DETECTORS, "no compiled detectors were loaded"
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
    _absence, _absence_detail = RUNNING, ""
    logger.info(
        f"sensorium: started — {len(detectors)} compiled detectors, cluster={cluster_id}"
    )


async def _refresh_db_detectors(cluster_id: str) -> None:
    """Reload promoted (active) + shadow detectors from the DB into the engine.

    Active DB detectors are merged with the playbook-compiled ones; shadow
    detectors fire only into the candidate buffer (never the watchtower).
    """
    global _last_db_counts
    if _engine is None:
        return
    try:
        active, shadow = await load_db_detectors(cluster_id)
    except Exception as exc:
        # Keep the set that is already loaded. A read that failed says nothing about which
        # detectors should be live, and this assignment is the live watchtower — replacing a
        # working set with the empty tuple a failed read returns is not failing open, it is
        # disarming. `load_db_detectors` raises here rather than returning empty tuples for
        # exactly that reason; before it did, this handler could not run.
        logger.warning(
            f"sensorium: db-detector refresh failed, KEEPING the "
            f"{(_last_db_counts or (0, 0))[0]} active / {(_last_db_counts or (0, 0))[1]} shadow "
            f"db detector(s) already loaded: {exc}"
        )
        return
    _engine.detectors = tuple(load_detectors()) + active
    _engine.shadow_detectors = shadow
    # Logged on the FIRST refresh and on every change thereafter — never on an unchanged steady
    # state, which would spam a line per refresh interval forever.
    #
    # The old guard was `if active or shadow or _last_db_counts != (0, 0)`, which reported the
    # arrival of coverage and its removal and said NOTHING in the one case that needed saying:
    # zero loaded, zero before, from startup. That is exactly what a cluster-id mismatch looks
    # like, and it is indistinguishable from "nobody has authored a detector" unless the line is
    # emitted at least once. It cost a 24-hour F3 shadow soak, which reported a perfect
    # false-positive rate of 0.0 while `shadow_detectors` was the empty tuple: 8 rows sat in the
    # DB under `cluster_id='global'` while this reader asked for `f3-shadow-soak-r2`. Two
    # silences compounded — a query that matched nothing, and a log line that only spoke when
    # something changed.
    counts = (len(active), len(shadow))
    if _last_db_counts is None or counts != _last_db_counts:
        was = "startup" if _last_db_counts is None else f"{_last_db_counts[0]}/{_last_db_counts[1]}"
        logger.info(
            f"sensorium: db detectors — {counts[0]} active, {counts[1]} shadow "
            f"(was {was}), cluster={cluster_id}",
            extra={"db_detectors_active": counts[0], "db_detectors_shadow": counts[1],
                   "cluster_id": cluster_id},
        )
    _last_db_counts = counts


async def _db_refresh_loop(cluster_id: str) -> None:
    from app.core.config import settings

    while True:
        await asyncio.sleep(float(settings.DB_DETECTOR_REFRESH_SECONDS))
        await _refresh_db_detectors(cluster_id)


async def stop_sensorium(reason: str = STOPPED, detail: str = "") -> None:
    global _engine, _absence, _absence_detail, _last_db_counts
    for task in _tasks:
        task.cancel()
    _tasks.clear()
    _engine = None
    _last_db_counts = None  # a restart must not inherit the previous run's counts
    _absence, _absence_detail = reason, detail
