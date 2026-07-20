"""Staged propagation (v5 P3 blast-radius; roadmap §5 verification gate).

A change that touches many targets must NEVER apply to all of them at once — it is released in
bounded stages (``stage_size`` targets each) with a mandatory wait window between stages, so a bad
change is caught on the first stage's blast radius, not the whole fleet. This is the deterministic
controller: which targets are eligible for the next stage, and whether the window has elapsed.

Pure/deterministic (clock injected) — fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class StageDecision:
    batch: list[str] = field(default_factory=list)   # targets to apply THIS stage ([] ⇒ wait/done)
    waiting: bool = False                             # True ⇒ window not elapsed yet
    done: bool = False                               # True ⇒ all targets applied
    reason: str = ""


def next_stage(
    targets: list[str],
    applied: list[str],
    *,
    stage_size: int = 1,
    window_seconds: float = 300.0,
    last_stage_epoch: Optional[float] = None,
    now_epoch: float = 0.0,
) -> StageDecision:
    """Decide the next propagation stage.

    - all targets applied ⇒ done.
    - a prior stage happened and the window has NOT elapsed ⇒ waiting (empty batch).
    - otherwise ⇒ the next ``stage_size`` not-yet-applied targets (order preserved).
    """
    applied_set = set(applied)
    remaining = [t for t in targets if t not in applied_set]
    if not remaining:
        return StageDecision(done=True, reason="all targets applied")
    if last_stage_epoch is not None and (now_epoch - last_stage_epoch) < window_seconds:
        wait = window_seconds - (now_epoch - last_stage_epoch)
        return StageDecision(waiting=True, reason=f"stage window: {wait:.0f}s until next stage")
    size = max(1, stage_size)
    return StageDecision(batch=remaining[:size],
                         reason=f"releasing {min(size, len(remaining))} of {len(remaining)} remaining")


def is_instant_global(targets: list[str], stage_size: int) -> bool:
    """True iff the config would apply everything in one stage (the thing we forbid for >1 target)."""
    return len(targets) > 1 and stage_size >= len(targets)
