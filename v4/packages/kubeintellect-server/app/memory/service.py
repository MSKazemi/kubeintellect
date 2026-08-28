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
import time

import asyncpg

from app.core.config import settings
from app.memory import episodes, kg, liveness, preferences
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
_ingest_failures: int = 0

# A drain failure that repeats is not an incident, it is an outage: the queue may be bound to a
# dead loop, or Postgres may be gone. Retrying instantly cannot fix either, and once cost a run
# 16,690,848 identical log lines and 3 GB. Back off, and say so out loud after this many in a row.
_INGEST_FAILURES_BEFORE_DEGRADED: int = 3
_INGEST_BACKOFF_MIN_S: float = 0.05
_INGEST_BACKOFF_MAX_S: float = 30.0

#: Seconds between reconnect attempts while the pool is down.
_RETRY_INTERVAL_S = 30.0

#: A recorded verdict older than this many verify intervals is reported as stale. Two, not one,
#: so a single slow or skipped pass is not an alarm — what this catches is a verifier that has
#: stopped, which otherwise looks exactly like one that keeps agreeing with itself.
_CHAIN_STALE_INTERVALS = 2.5


def _chain_stale_after_s() -> float | None:
    """When a recorded chain verdict stops describing the store as it is now.

    ``None`` when the periodic verifier is off — a single startup verdict is the only verdict
    there will ever be, so calling it stale would report a fault the operator already chose.
    """
    interval = settings.MEMORY_CHAIN_VERIFY_INTERVAL_S
    if interval <= 0:
        return None
    return interval * _CHAIN_STALE_INTERVALS


def memory_active() -> bool:
    return _pool is not None


def memory_status() -> dict:
    """Shape reported on ``/healthz``. An operator must be able to see that memory is dead.

    ``observations_dropped`` is what turns "the knowledge graph is empty" from a guess into a
    fact — the sensorium keeps producing whether or not anything is there to receive.

    ``recall_*``/``episodes_written`` answer the question ``enabled`` cannot: whether memory is
    doing anything, as opposed to merely being reachable. ``symptoms`` is the short human-readable
    summary, and ``healthy`` is the single boolean a probe or a benchmark gate should key on —
    false whenever the process is connected but observably not working.

    ``chain`` is the memory audit chain's last recorded verdict. It reports what was checked and
    when, and it is the reason a *stale* verdict cannot be read as a current one.
    """
    chain = liveness.chain_status(
        enabled=settings.MEMORY_SECURITY_HARDENING,
        now=time.time(),
        stale_after_s=_chain_stale_after_s(),
    )
    symptoms = liveness.symptoms(
        state=_state, observations_dropped=_dropped_observations, chain=chain,
    )
    return {
        "enabled": _state == "ready",
        "state": _state,
        "reason": _reason,
        "observations_dropped": _dropped_observations,
        "ingest_failures": _ingest_failures,
        # What the tamper-evidence chain last SAID, and when — never a fresh verdict computed
        # inside a health probe. `state: "off"` and `state: "never-checked"` are deliberately
        # distinct from `intact`: neither is a clean bill of health.
        "chain": chain,
        # Observed behaviour. `enabled` says the pool is up; these say whether anything came
        # out of it. A run where `recall_attempts` is high and `recall_hits` is 0 is the exact
        # shape of the nine-hour dead-memory lane described at the top of this module.
        **liveness.counters(),
        # Empty means nothing observably wrong. Non-empty is the machine-readable version of
        # "do not trust memory-dependent results from this process".
        "symptoms": symptoms,
        "healthy": _state in {"ready", "flag", "sqlite"} and not symptoms,
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


async def init_memory_readonly() -> bool:
    """Bind the hierarchy for READING ONLY: pool + query modules, no workers, no consolidation.

    `init_memory` is a *server* startup. Two of the things it starts are writers: the
    observation drain, and the consolidation schedule — whose startup pass backfills
    `rca_outcomes` into `episodes` and whose later passes promote rules, build summaries and
    fire prospective rechecks. That is correct for the process that owns the cluster's memory,
    and wrong for any process that only wants to look at it.

    It cost a confirmatory run to learn that. On 2026-08-24 the OpsMemBench metric reader called
    `init_memory()` in-process to answer "which episode did the agent write for this fault?".
    On the No-memory control arm the reader's own startup consolidation pass then backfilled one
    row into an `episodes` table the arm gate requires to stay empty, and the gate voided the
    arm — correctly, because a control that holds an episode is not a control. The instrument
    was writing to the thing it was measuring, and the only reason it was caught is that the
    gate counted rows rather than trusting `/healthz` (which reported `episodes_written: 0`,
    since a backfill is deliberately not a live write).

    Returns True when the pool is bound. Never raises, never retries: a reader with no database
    reports nothing, which is the honest answer, whereas a reader that blocks or retries turns
    a measurement into an outage.
    """
    global _pool, _state, _reason
    if settings.USE_SQLITE:
        _state, _reason = "sqlite", "USE_SQLITE=true — hierarchy needs Postgres"
        logger.info("memory: SQLite mode — read-only reader not attached")
        return False
    try:
        _pool = await asyncpg.create_pool(
            settings.POSTGRES_DSN, min_size=1, max_size=2, command_timeout=5
        )
    except Exception as exc:
        _pool = None
        _state, _reason = "unavailable", str(exc)
        logger.warning(f"memory: read-only reader could not connect ({exc})")
        return False
    # Wire the query surfaces onto the pool — and stop. No `_obs_queue`, so `enqueue_observation`
    # cannot ingest; no tasks, so nothing consolidates. Deliberately NOT `_activate`.
    episodes.init_episodes(_pool)
    kg.init_kg(_pool)
    preferences.init_preferences(_pool)
    _state, _reason = "ready", ""
    logger.info("memory: read-only reader attached (no observation drain, no consolidation)")
    return True


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
    # One read, once, at the moment a pool first exists: is the database the shape this build
    # writes to? Never on the request path and never in /healthz itself (liveness must not touch
    # Postgres) -- the verdict is cached and reported. See `app/db/schema_version.py`.
    from app.db.schema_version import check_schema
    await check_schema(_pool)
    return True


def _activate(pool: asyncpg.Pool) -> None:
    """Wire the hierarchy onto a live pool. Split out of init_memory so the reconnect loop
    finishes startup properly rather than leaving a pool with no workers on it."""
    global _obs_queue, _state, _reason, _ingest_failures
    episodes.init_episodes(pool)
    kg.init_kg(pool)
    preferences.init_preferences(pool)

    # Activation must be idempotent. It is reached twice in the ordinary reconnect case, and the
    # previous generation of workers is still running when it is: leaving them alive left a drain
    # task awaiting a queue that had since been rebound on another loop, which is unrecoverable
    # and silent. Cancel the old generation before creating the new one.
    for task in _tasks:
        task.cancel()
    _tasks.clear()

    # Sized by MEMORY_OBS_QUEUE_MAXSIZE, not a literal: this is the twin of
    # SENSORIUM_QUEUE_MAXSIZE on the same observation stream, and widening one without the
    # other leaves the narrower queue dropping at the old depth.
    _obs_queue = asyncio.Queue(maxsize=settings.MEMORY_OBS_QUEUE_MAXSIZE)
    _ingest_failures = 0

    loop = asyncio.get_running_loop()
    # The queue is passed in, not read from the global: a worker must not be able to reach a
    # queue created after it was, no matter what reassigns the global while it runs.
    _tasks.append(loop.create_task(_drain_observations(_obs_queue)))

    from app.memory.consolidation import consolidation_loop, run_consolidation_once

    # One-shot startup pass: rca_outcomes backfill + initial maintenance.
    _tasks.append(loop.create_task(run_consolidation_once(startup=True)))
    _tasks.append(loop.create_task(consolidation_loop()))
    if settings.MEMORY_SECURITY_HARDENING and settings.MEMORY_CHAIN_VERIFY_INTERVAL_S >= 0:
        # The chain is only written when the hardening flag is on, so verifying it otherwise
        # would report "intact" about a chain nothing appends to — a green light for a feature
        # that is not running. See `_verify_chain_loop`.
        _tasks.append(loop.create_task(_verify_chain_loop()))
    _state, _reason = "ready", ""
    logger.info("memory: hierarchy active (L1 episodes, L2 kg, consolidation)")


async def verify_chain_once() -> None:
    """Ask the memory audit chain whether it still verifies, and record what it said.

    Deliberately records rather than returns. The verdict's consumer is `/healthz`, which is
    synchronous and probed every few seconds, while verifying reads every audit row for the
    cluster — so the surface reports the last recorded answer and how old it is, and never
    re-derives one on the probe path.

    Never raises. This runs in a background task inside a subsystem whose whole discipline is
    that a memory failure cannot break a user response; a verifier that crashed the task group
    would take the observation drain and the consolidation schedule with it.
    """
    from app.cluster_id import get_cluster_id
    from app.memory.security import verify_memory_chain

    try:
        verdict = await verify_memory_chain(_pool, get_cluster_id())
    except Exception as exc:
        # Not `valid=False`. An exception here is our failure to look, not a finding about
        # the rows — the same doctrine `verify_memory_chain` applies to its own fetch errors.
        logger.warning(f"memory: audit-chain verify raised: {exc} — recording as UNVERIFIED")
        liveness.record_chain_check(valid=True, verified=False, at=time.time())
        return
    liveness.record_chain_check(
        valid=verdict.valid, verified=verdict.verified, at=time.time()
    )
    if verdict.verified and not verdict.valid:
        # The one line in this module that is an accusation. It is logged at ERROR because the
        # only other place it appears is a `/healthz` field somebody has to be reading.
        logger.error(
            "memory: AUDIT CHAIN DOES NOT VERIFY — the recorded memory-audit rows no longer "
            "hash to what they carry, or the chain is shorter than its own head anchor. "
            "Treat memory-derived answers from this cluster as untrusted until reviewed."
        )
    elif not verdict.verified:
        logger.info("memory: audit chain could not be verified this pass (not a tamper signal)")


async def _verify_chain_loop() -> None:
    """One verify at startup, then every `MEMORY_CHAIN_VERIFY_INTERVAL_S` seconds.

    The startup pass matters on its own: a process that comes up against a database somebody
    edited while it was down should say so before it serves anything, not one interval later.
    An interval of 0 keeps that pass and skips the schedule.
    """
    await verify_chain_once()
    interval = settings.MEMORY_CHAIN_VERIFY_INTERVAL_S
    if interval <= 0:
        return
    while True:
        try:
            await asyncio.sleep(interval)
            await verify_chain_once()
        except asyncio.CancelledError:
            break
        except Exception as exc:      # the schedule itself must never die
            logger.warning(f"memory: audit-chain verify loop error: {exc}")


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
    global _pool, _obs_queue, _state, _reason, _ingest_failures
    _state, _reason = "starting", ""
    _ingest_failures = 0
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


async def _drain_observations(queue: asyncio.Queue) -> None:
    """Drain the observation queue onto the knowledge graph.

    Failures here used to be logged and retried immediately, forever. That is right for an
    incidental error and catastrophic for a persistent one, which is the case that actually
    occurs: the loop cannot make progress, cannot stop, and never tells anyone -- `_state` stayed
    "ready" while every observation was discarded. So: count consecutive failures, back off
    between them, and degrade the reported state once the failures are clearly not incidental.
    """
    global _ingest_failures, _state, _reason
    backoff = _INGEST_BACKOFF_MIN_S
    while True:
        try:
            obs = await queue.get()
            if obs.kind == "pod_status":
                await kg.ingest_pod_observation(obs)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            _ingest_failures += 1
            # Log the first, then thin out: the point of the counter is that the log does not
            # have to carry the volume.
            if _ingest_failures == 1 or _ingest_failures % 100 == 0:
                logger.warning(
                    f"memory: observation ingest error ({_ingest_failures} consecutive): {exc}"
                )
            if _ingest_failures >= _INGEST_FAILURES_BEFORE_DEGRADED and _state == "ready":
                _state = "degraded"
                _reason = f"observation ingest failing: {exc}"
                logger.error(
                    f"memory: ingest has failed {_ingest_failures} times in a row — the "
                    f"knowledge graph is no longer being updated ({exc})"
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _INGEST_BACKOFF_MAX_S)
            continue
        # Progress: this generation is healthy again.
        if _ingest_failures:
            _ingest_failures = 0
            backoff = _INGEST_BACKOFF_MIN_S
            if _state == "degraded":
                _state, _reason = "ready", ""
                logger.info("memory: observation ingest recovered")
