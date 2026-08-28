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

# The last verdict from `security.verify_memory_chain`, recorded rather than recomputed. The
# chain is the memory subsystem's tamper-evidence, and until 2026-08-28 nothing in a running
# server ever asked it: the only callers were the test suite and an offline probe script. A
# verifier that never runs is not weaker evidence, it is none.
#
# `/healthz` reports what was RECORDED here and when, never a fresh check. Verifying reads every
# audit row for the cluster, and the kubelet probes health every few seconds — a surface that
# re-verified on demand would be a self-inflicted load problem and would make the probe's latency
# a function of how much history the cluster has.
_chain_checks = 0
_chain_checked_at: float | None = None
_chain_valid: bool | None = None
_chain_verified: bool | None = None


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


def record_chain_check(*, valid: bool, verified: bool, at: float) -> None:
    """Note one completed verification of the memory audit chain.

    `at` is passed in rather than read from the clock so the recorder stays a pure function of
    its inputs, which is the property that makes the staleness classification testable.
    """
    global _chain_checks, _chain_checked_at, _chain_valid, _chain_verified
    with _lock:
        _chain_checks += 1
        _chain_checked_at = at
        _chain_valid = valid
        _chain_verified = verified


def chain_status(*, enabled: bool, now: float | None = None,
                 stale_after_s: float | None = None) -> dict:
    """What the memory audit chain last said, and when — or why nothing said anything.

    `state` is the one field to read, and it separates four things a boolean cannot:

    * ``off`` — the feature that writes the chain is disabled, so there is nothing to check.
      Not a fault, and not a clean bill of health either.
    * ``never-checked`` — enabled, but no verification has completed yet. This is what the
      whole surface used to report implicitly, by reporting nothing at all.
    * ``unverified`` — a check ran and could not reach a conclusion (an unreachable database,
      a head row it could not read). Deliberately NOT ``tampered``: a detector that cries
      tamper whenever its own storage is down teaches operators to ignore it.
    * ``intact`` / ``TAMPERED`` — a check ran and reached a conclusion.

    A verdict older than `stale_after_s` is reported as ``stale`` in `age_s`'s company rather
    than being silently presented as current.
    """
    with _lock:
        checks, at = _chain_checks, _chain_checked_at
        valid, verified = _chain_valid, _chain_verified
    if not enabled:
        state = "off"
    elif checks == 0:
        state = "never-checked"
    elif valid is False:
        state = "TAMPERED"
    elif not verified:
        state = "unverified"
    else:
        state = "intact"
    age = None if at is None or now is None else max(0.0, now - at)
    return {
        "state": state,
        "checks": checks,
        "checked_at": at,
        "age_s": age,
        "valid": valid,
        "verified": verified,
        "stale": bool(
            stale_after_s is not None and age is not None and age > stale_after_s
        ),
    }


def reset_chain_state() -> None:
    """Tests only — the recorder is process-global, like every other counter here."""
    global _chain_checks, _chain_checked_at, _chain_valid, _chain_verified
    with _lock:
        _chain_checks = 0
        _chain_checked_at = None
        _chain_valid = None
        _chain_verified = None


def symptoms(*, state: str, observations_dropped: int = 0,
             chain: dict | None = None) -> list[str]:
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
    if chain and chain.get("state") == "TAMPERED":
        # `valid is False` is a performed check with a positive finding, so it belongs here.
        # `unverified` deliberately does NOT: nobody looked is not evidence of anything, and
        # putting it here would make an unreachable database read as an integrity alarm.
        out.append(
            "the memory audit chain does not verify — its recorded rows no longer hash to what "
            "they carry, or the chain is shorter than its own head anchor says"
        )
    if chain and chain.get("stale"):
        out.append(
            f"the memory audit chain has not been verified for {chain['age_s']:.0f}s — the last "
            f"verdict is too old to describe the store as it is now"
        )
    return out
