"""Single-writer election, so the control plane can run more than one replica.

WHY THIS EXISTS
---------------
Every background worker in this process is a *singleton by assumption*: the sensorium opens
`kubectl get pods -A --watch` and feeds a detector engine; the watchtower turns detector firings
into investigations and, at A3, into cluster writes; consolidation rewrites the memory graph.
None of them coordinate with a peer, and none of them ever asked whether a peer exists.

That assumption is enforced by nothing but `replicaCount: 1` in the chart. Scale the Deployment to
2 and the second pod does not share the work — it *repeats* it: two watch streams over the same
cluster, two engines firing the same finding, two watchtowers opening an investigation for it, and
two consolidation passes writing the same episodes. The failure is not a crash. It is duplicated
autonomous action against a production cluster, and it looks exactly like the cluster genuinely
having two problems. A single replica is therefore a hard availability floor: the one pod is a
single point of failure for all perception, and it cannot be scaled out of.

WHY AN ADVISORY LOCK, NOT A KUBERNETES LEASE
--------------------------------------------
A `coordination.k8s.io/Lease` is the idiomatic answer and it is the wrong one *here*: this process
reaches Kubernetes by shelling out to `kubectl`, so a Lease would mean a renewal loop built on
subprocesses, a new RBAC grant, and a clock-skew-sensitive expiry to tune.

Postgres is already this deployment's system of record — checkpoints, memory and the audit log
all live in it — and `pg_try_advisory_lock` is **session-scoped**: the lock lives exactly as long
as the connection that took it. Kill the pod, sever the network, OOM the container — the backend
notices the connection is gone and the lock is released with no lease to expire and no TTL to
mistune. There is no renewal loop to get wrong because there is no renewal.

The cost is honest and worth stating: leadership is now tied to a Postgres session, so a database
failover drops it. That is the correct trade — on failover the workers stop rather than double up,
and the standby loop below reacquires within `poll_seconds`.

FAILURE POSTURE: closed, deliberately
-------------------------------------
Anything that is not a *confirmed* acquisition means NOT leader. A duplicated watchtower can take
real actions against a real cluster; a missing one degrades to no autonomous action, which is
visible on `/healthz` and safe. When the two error directions are unequal, the gate fails toward
the recoverable one.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import zlib
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# Postgres advisory locks are namespaced by a single bigint. Derive it from a stable string so two
# unrelated applications sharing a database cannot collide, and so the value is reproducible rather
# than a magic number nobody can re-derive.
_LOCK_NAMESPACE = "kubeintellect/singleton-workers"


def lock_key(scope: str = "") -> int:
    """Stable 63-bit advisory-lock key for a scope.

    Scoped per cluster_id when one is set: two KubeIntellect deployments managing DIFFERENT
    clusters off ONE database must each get a leader, or the second deployment would sit
    permanently in standby and silently watch nothing.
    """
    raw = f"{_LOCK_NAMESPACE}:{scope}" if scope else _LOCK_NAMESPACE
    # crc32 is not a hash for security purposes — it is a collision-resistant-enough spreader for
    # a namespace we control. Masked to 63 bits: the column is a SIGNED bigint.
    return zlib.crc32(raw.encode()) & 0x7FFF_FFFF_FFFF_FFFF


class LeaderElection:
    """Holds leadership for as long as its dedicated connection lives.

    Deliberately owns a connection of its own rather than borrowing from a pool: a pooled
    connection is returned after each query, and an advisory lock returned to a pool is a lock
    handed to whichever caller draws that connection next.
    """

    def __init__(
        self,
        dsn: str,
        *,
        scope: str = "",
        poll_seconds: float = 10.0,
        on_acquire: Callable[[], Awaitable[None]] | None = None,
        on_lose: Callable[[], Awaitable[None]] | None = None,
        identity: str | None = None,
    ) -> None:
        self._dsn = dsn
        self._key = lock_key(scope)
        self._poll = poll_seconds
        self._on_acquire = on_acquire
        self._on_lose = on_lose
        # The pod name, so an operator reading a log can tell WHICH replica is leading. Falling
        # back to the PID keeps this useful outside Kubernetes.
        self._identity = identity or os.environ.get("HOSTNAME") or f"pid-{os.getpid()}"
        # asyncpg ships no py.typed marker, so the connection is Any rather than a real type.
        self._conn: Any | None = None
        self._task: asyncio.Task | None = None
        self._is_leader = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    @property
    def identity(self) -> str:
        return self._identity

    def status(self) -> dict:
        """Shape reported on /healthz. An operator must be able to see WHY nothing is watching."""
        return {
            "enabled": True,
            "is_leader": self._is_leader,
            "identity": self._identity,
            "lock_key": self._key,
        }

    async def _connect(self):
        import asyncpg

        return await asyncpg.connect(self._dsn)

    async def _try_acquire(self) -> bool:
        """One acquisition attempt. Never raises — a failed attempt is 'not leader', not an error."""
        try:
            if self._conn is None:
                self._conn = await self._connect()
            got = await self._conn.fetchval("SELECT pg_try_advisory_lock($1)", self._key)
            return bool(got)
        except Exception as exc:
            logger.warning(f"leader_election: acquisition attempt failed ({exc}) — remaining standby")
            # Drop the connection so the next attempt reconnects rather than reusing a dead socket.
            await self._close_conn()
            return False

    async def _close_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()

    async def _still_held(self) -> bool:
        """Cheap liveness check on the session that owns the lock.

        A leader that lost its connection has ALREADY lost the lock — Postgres released it the
        moment the session ended. Detecting that promptly is what stops a partitioned pod from
        continuing to act as leader while a peer legitimately takes over.
        """
        if self._conn is None:
            return False
        try:
            await self._conn.fetchval("SELECT 1")
            return True
        except Exception as exc:
            logger.warning(f"leader_election: lost the connection holding the lock ({exc})")
            await self._close_conn()
            return False

    async def _run(self) -> None:
        while True:
            try:
                if self._is_leader:
                    if not await self._still_held():
                        self._is_leader = False
                        logger.warning(
                            f"leader_election: {self._identity} LOST leadership — stopping "
                            f"singleton workers so a peer can take over without duplicating them"
                        )
                        if self._on_lose is not None:
                            with contextlib.suppress(Exception):
                                await self._on_lose()
                elif await self._try_acquire():
                    self._is_leader = True
                    logger.info(f"leader_election: {self._identity} ACQUIRED leadership (key={self._key})")
                    if self._on_acquire is not None:
                        try:
                            await self._on_acquire()
                        except Exception as exc:
                            # The callback starting workers must not kill the election loop; a
                            # process that believes it leads but runs nothing is the worst state.
                            logger.error(f"leader_election: on_acquire failed: {exc}")
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the loop itself must never die
                logger.error(f"leader_election: loop error {exc}")
                await asyncio.sleep(self._poll)

    async def start(self) -> None:
        """Attempt acquisition once synchronously, then keep trying in the background.

        The first attempt is awaited so the common case — one replica, uncontended lock — is
        already leading by the time startup finishes, instead of idling for a poll interval.
        """
        if await self._try_acquire():
            self._is_leader = True
            logger.info(f"leader_election: {self._identity} ACQUIRED leadership (key={self._key})")
            if self._on_acquire is not None:
                try:
                    await self._on_acquire()
                except Exception as exc:
                    logger.error(f"leader_election: on_acquire failed: {exc}")
        else:
            logger.info(
                f"leader_election: {self._identity} is STANDBY — another replica holds the lock. "
                f"The API serves normally; singleton workers stay idle here."
            )
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # Explicit unlock so a graceful shutdown hands over immediately instead of waiting for the
        # backend to notice a closed socket.
        if self._is_leader and self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.execute("SELECT pg_advisory_unlock($1)", self._key)
        self._is_leader = False
        await self._close_conn()
