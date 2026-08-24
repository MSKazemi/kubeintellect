"""Whether a consolidation pass actually ran — the counter it returns cannot say.

Every pass in the consolidation worker returns an ``int``, and every one of them returns ``0``
for two very different things: *it ran and there was nothing to do*, and *it raised and its own
guard caught it*. Measured 2026-08-24 by driving `run_consolidation_once` against a pool whose
every statement raises, with a healthy-but-idle pool as the control: both produced

    {"backfilled": 0, "stale_edges_closed": 0, "detector_candidates": 0,
     "prefs_inferred": 0, "prefs_forgotten": 0}

— byte-identical. Each pass does log its own ``WARNING``, so this is not a silent outage in the
log; it is a silent outage in the **machine-readable** result, which is the part that is returned
to callers and described as being "for tests/digest". And because the worker's own summary line
is gated on ``if any(stats.values())``, the one line that could have said *the pass completed and
here is what it did* fires for neither state.

The failure discipline itself is right and is unchanged: a pass that raises must not stop the
loop or the passes after it. This module only makes the guard **say so**, so that "nothing needed
doing" and "nothing worked" stop being the same answer.

Not thread-safe by design and does not need to be: the consolidation worker is a single asyncio
task, and every recorder runs inside it.
"""

from __future__ import annotations

_failures: list[tuple[str, str]] = []


def record_failure(pass_name: str, exc: BaseException | str) -> None:
    """Called from a pass's own ``except`` block, next to its ``logger.warning``.

    Deliberately takes the exception rather than a bool: a caller that has to summarise the
    outage needs to say *which* pass and *why*, and a count alone would reproduce the defect
    one level up.
    """
    _failures.append((pass_name, str(exc)[:200]))


def drain() -> list[tuple[str, str]]:
    """Return the failures recorded since the last drain, and forget them.

    Draining is what makes the register per-pass rather than cumulative — the worker runs every
    600s and a stale failure reported forever is its own kind of untrue signal.
    """
    out = list(_failures)
    _failures.clear()
    return out


def reset() -> None:
    """Drop anything recorded. For tests, and for a caller that wants a clean slate."""
    _failures.clear()
