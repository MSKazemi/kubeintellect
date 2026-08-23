"""The watch stream must not be able to grow memory without bound.

`_watch_loop` called `sink(obs)` inline, so the only thing limiting how fast the detector engine
and memory writer were driven was the event loop. `kubectl get pods -A --watch` replays the ENTIRE
cluster on every reconnect; at data-centre pod counts that is a burst large enough to OOM a
container with a 1 GiB limit — which restarts, relists everything again, and repeats.

A bounded queue does not shrink the firehose. It converts an invisible OOM into a counted loss.
These tests assert exactly that: that the loss is bounded, that it is COUNTED, and that what
survives is the CURRENT state of the cluster rather than its history.
"""
from __future__ import annotations

import asyncio

import pytest

from app.sensorium import k8s_watcher as kw


@pytest.fixture(autouse=True)
def _clean():
    kw.reset_queue_stats()
    yield
    kw.reset_queue_stats()


def test_nothing_is_shed_below_the_limit():
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    for i in range(10):
        kw._enqueue(q, f"obs-{i}")
    assert kw.queue_stats()["shed_total"] == 0
    assert q.qsize() == 10


def test_overflow_is_bounded_and_counted():
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    for i in range(1000):
        kw._enqueue(q, f"obs-{i}")
    # Bounded: memory cannot grow past the limit no matter how long the burst runs.
    assert q.qsize() == 10
    # Counted: 990 observations were dropped and the system can SAY so. Silent loss here would
    # mean the detector engine misses incidents while reporting itself healthy.
    assert kw.queue_stats()["shed_total"] == 990


def test_the_newest_observations_are_what_survive():
    """A pod's status is a LEVEL, not an edge — the newest observation supersedes the stale one.
    Shedding the newest would keep a queue full of history while discarding the current state."""
    q: asyncio.Queue = asyncio.Queue(maxsize=3)
    for i in range(10):
        kw._enqueue(q, f"obs-{i}")
    survived = [q.get_nowait() for _ in range(3)]
    assert survived == ["obs-7", "obs-8", "obs-9"]


def test_high_water_is_recorded_even_when_nothing_is_shed():
    """Depth without loss is the early warning — it says the consumer is falling behind while
    detection is still complete."""
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    for i in range(40):
        kw._enqueue(q, f"obs-{i}")
    assert kw.queue_stats()["shed_total"] == 0
    assert kw.queue_stats()["high_water"] == 40


@pytest.mark.asyncio
async def test_the_consumer_delivers_in_order_to_the_real_sink():
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    got: list = []
    task = asyncio.create_task(kw._drain_queue(q, got.append))
    try:
        for i in range(20):
            kw._enqueue(q, i)
        await asyncio.wait_for(q.join(), timeout=5)
        assert got == list(range(20))
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_one_failing_observation_does_not_stop_perception():
    """A sink that raises on a single malformed observation must not deafen the process."""
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    got: list = []

    def flaky(obs):
        if obs == 3:
            raise ValueError("bad observation")
        got.append(obs)

    task = asyncio.create_task(kw._drain_queue(q, flaky))
    try:
        for i in range(6):
            kw._enqueue(q, i)
        await asyncio.wait_for(q.join(), timeout=5)
        assert got == [0, 1, 2, 4, 5], "the consumer must survive one bad observation"
    finally:
        task.cancel()
