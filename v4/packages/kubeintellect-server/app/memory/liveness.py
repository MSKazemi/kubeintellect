"""Observed memory behaviour — what memory *did*, not what it is configured to do.

`memory_status()["enabled"]` answers "is the pool up?". That is a different question from "is
memory working", and the distance between them is not hypothetical. An evaluation lane ran three
arms for nine hours with the memory state reporting `ready` the whole time while not one episode
was ever written or recalled: the pool was genuinely fine, so health was genuinely green, and the
arms were graded and their numbers written down before anyone noticed the subsystem under test had
never run. Every campaign-blocking defect that week had the same shape — the system reported
healthy while a path was dead — and this module exists to close that shape off for the one
subsystem the product is differentiated on.

The counters here are deliberately about *outcomes*: attempts, hits, failures, writes. A gauge
that reads "enabled: true" cannot distinguish a working store from a dead one, but "asked 40
times, answered 0 times, wrote 0 episodes" is not ambiguous.

This module imports nothing from the rest of `app.memory` on purpose. `service` owns the pool and
`episodes` owns recall, so either importing the other would be a cycle; both may import this.
"""
from __future__ import annotations

from threading import Lock

#: Attempts to see before "asked repeatedly, never answered" is reportable as a symptom. Below
#: this an all-miss run is just a cold store, which is the normal state of a new cluster and must
#: not be dressed up as a fault.
ATTEMPTS_BEFORE_SUSPICIOUS = 10

# Counters are touched from the request path and read from /healthz. CPython's GIL makes `+= 1`
# on an int effectively atomic, but the lock is cheap and makes the invariant explicit rather
# than dependent on an implementation detail.
_lock = Lock()
_recall_attempts = 0
_recall_hits = 0
_recall_failures = 0
_episodes_written = 0


def record_recall(*, hit: bool) -> None:
    """Note one completed recall attempt. `hit` means "returned at least one episode"."""
    global _recall_attempts, _recall_hits
    with _lock:
        _recall_attempts += 1
        if hit:
            _recall_hits += 1


def record_recall_failure() -> None:
    """Note one recall attempt that raised instead of returning rows."""
    global _recall_attempts, _recall_failures
    with _lock:
        _recall_attempts += 1
        _recall_failures += 1


def record_episode_written() -> None:
    """Note one episode successfully persisted to L1."""
    global _episodes_written
    with _lock:
        _episodes_written += 1


def reset() -> None:
    """Zero every counter. For tests; nothing in the request path calls this."""
    global _recall_attempts, _recall_hits, _recall_failures, _episodes_written
    with _lock:
        _recall_attempts = _recall_hits = _recall_failures = _episodes_written = 0


def counters() -> dict[str, int]:
    with _lock:
        return {
            "recall_attempts": _recall_attempts,
            "recall_hits": _recall_hits,
            "recall_failures": _recall_failures,
            "episodes_written": _episodes_written,
        }


def symptoms(*, state: str, observations_dropped: int = 0) -> list[str]:
    """Plain statements about what is observably wrong, or an empty list.

    Phrased as observations, not diagnoses. Each is a fact the process can prove about itself;
    what it *means* needs context the process does not have, since a brand new cluster legitimately
    recalls nothing. Empty is the healthy answer, and a non-empty list is the machine-readable
    form of "do not trust memory-dependent results from this process" — which is precisely the
    check an evaluation gate should make before it grades anything.
    """
    c = counters()
    attempts, hits = c["recall_attempts"], c["recall_hits"]
    out: list[str] = []

    if state == "ready" and attempts >= ATTEMPTS_BEFORE_SUSPICIOUS:
        if hits == 0:
            out.append(
                f"memory is connected and was queried {attempts} times but has never returned "
                f"an episode — recall is producing nothing"
            )
        if c["episodes_written"] == 0:
            out.append(
                f"memory is connected and was queried {attempts} times but no episode has ever "
                f"been written — the store cannot fill, so recall can never improve"
            )
    if c["recall_failures"]:
        out.append(f"{c['recall_failures']} of {attempts} recall attempts failed outright")
    if observations_dropped:
        out.append(
            f"{observations_dropped} observations were dropped before reaching the knowledge graph"
        )
    return out
