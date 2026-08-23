"""Leader election — the gate that makes replicaCount > 1 safe.

Every background worker in the server is a singleton by assumption, and until this existed the
only thing enforcing that assumption was `replicaCount: 1` in the chart. These tests exist because
the failure mode of getting this wrong is not a crash: it is two watchtowers taking autonomous
action against the same production cluster, which looks exactly like the cluster having two
problems. Nothing else in the suite can catch that.

No Postgres required — the connection is faked, so acquisition, contention, connection loss and
handover are all exercised deterministically.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.leader import LeaderElection, lock_key


class FakeConn:
    """Minimal asyncpg-connection stand-in."""

    def __init__(self, *, acquires: bool = True, dies_after: int | None = None) -> None:
        self.acquires = acquires
        self.dies_after = dies_after
        self.calls = 0
        self.closed = False
        self.unlocked = False

    async def fetchval(self, sql: str, *args):
        self.calls += 1
        if self.dies_after is not None and self.calls > self.dies_after:
            raise ConnectionError("server closed the connection unexpectedly")
        if "pg_try_advisory_lock" in sql:
            return self.acquires
        return 1

    async def execute(self, sql: str, *args):
        if "pg_advisory_unlock" in sql:
            self.unlocked = True

    async def close(self):
        self.closed = True


def _election(conn, **kw) -> LeaderElection:
    el = LeaderElection("postgresql://fake/db", poll_seconds=0.01, **kw)
    el._connect = lambda: _wrap(conn)  # type: ignore[assignment]
    return el


async def _wrap(conn):
    return conn


# ── the lock key ──────────────────────────────────────────────────────────────────────────────

def test_lock_key_is_stable_and_fits_a_signed_bigint():
    # The column is a SIGNED bigint; a key with the high bit set would be rejected by Postgres.
    for scope in ("", "prod", "staging", "a" * 200):
        key = lock_key(scope)
        assert 0 <= key < 2**63
        assert key == lock_key(scope), "key must be reproducible across processes"


def test_different_clusters_get_different_keys():
    # Two deployments managing DIFFERENT clusters off ONE database must each elect a leader.
    # Sharing a key would leave the second permanently in standby, silently watching nothing.
    assert lock_key("cluster-a") != lock_key("cluster-b")


# ── acquisition ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_acquiring_the_lock_starts_the_singleton_workers():
    started = []
    el = _election(FakeConn(acquires=True), on_acquire=lambda: _record(started, "start"))
    await el.start()
    try:
        assert el.is_leader is True
        assert started == ["start"], "the leader must actually start the workers"
    finally:
        await el.stop()


@pytest.mark.asyncio
async def test_a_contended_lock_leaves_the_replica_in_standby():
    started = []
    el = _election(FakeConn(acquires=False), on_acquire=lambda: _record(started, "start"))
    await el.start()
    try:
        assert el.is_leader is False
        assert started == [], "a standby must NOT start a second watch stream"
    finally:
        await el.stop()


@pytest.mark.asyncio
async def test_a_database_failure_means_standby_not_leader():
    """Fail CLOSED. A duplicated watchtower acts on a live cluster; a missing one only degrades."""
    started = []

    class Dead:
        async def fetchval(self, *a, **k):
            raise ConnectionError("could not connect")

        async def close(self):
            pass

    el = _election(Dead(), on_acquire=lambda: _record(started, "start"))
    await el.start()
    try:
        assert el.is_leader is False
        assert started == []
    finally:
        await el.stop()


# ── losing leadership ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_losing_the_connection_stops_the_workers():
    """Postgres releases a session lock the moment the session ends, so a leader that lost its
    connection has ALREADY lost the lock. Continuing to act would duplicate a peer's work."""
    events = []
    conn = FakeConn(acquires=True, dies_after=1)   # survives the acquire, dies on the next check
    el = _election(
        conn,
        on_acquire=lambda: _record(events, "start"),
        on_lose=lambda: _record(events, "stop"),
    )
    await el.start()
    try:
        assert el.is_leader is True
        for _ in range(200):                        # let the poll loop observe the dead connection
            if not el.is_leader:
                break
            await asyncio.sleep(0.01)
        assert el.is_leader is False, "a leader with a dead connection must stand down"
        assert events == ["start", "stop"]
    finally:
        await el.stop()


@pytest.mark.asyncio
async def test_a_failing_on_acquire_callback_does_not_kill_the_election_loop():
    """A process that believes it leads but runs nothing is the worst of the three states."""
    async def boom():
        raise RuntimeError("sensorium exploded")

    el = _election(FakeConn(acquires=True), on_acquire=boom)
    await el.start()
    try:
        assert el.is_leader is True
        await asyncio.sleep(0.05)
        assert el._task is not None and not el._task.done(), "the loop must survive the callback"
    finally:
        await el.stop()


# ── handover ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_releases_the_lock_explicitly():
    """Graceful shutdown hands over immediately instead of waiting for the backend to notice a
    closed socket — otherwise every rolling update has a leaderless gap."""
    conn = FakeConn(acquires=True)
    el = _election(conn)
    await el.start()
    assert el.is_leader is True
    await el.stop()
    assert conn.unlocked is True
    assert conn.closed is True
    assert el.is_leader is False


@pytest.mark.asyncio
async def test_status_reports_enough_to_diagnose_a_silent_standby():
    el = _election(FakeConn(acquires=False))
    await el.start()
    try:
        st = el.status()
        # A standby serves the API normally while watching nothing. Without these fields that is
        # indistinguishable from a broken sensorium.
        assert st["enabled"] is True
        assert st["is_leader"] is False
        assert st["identity"]
        assert isinstance(st["lock_key"], int)
    finally:
        await el.stop()


async def _record(sink: list, label: str) -> None:
    sink.append(label)
