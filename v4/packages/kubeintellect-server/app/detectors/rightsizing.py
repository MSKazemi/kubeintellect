"""Evidence-grounded rightsizing recommendations (v5 P4, A-CH-08).

Recommendations are commodity; *being believed* is the product. So this produces a resource-limit
recommendation strictly grounded in observed signals (OOMKills, peak-memory-vs-limit, CPU throttle)
with an explicit rationale and a confidence, rather than a black-box number. A resize the operator
(or, once earned, the autonomy ladder) applies would go through the P3 mutating chokepoint.

Pure/deterministic — fully unit-testable, no cluster.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# tuning knobs (conservative defaults)
_HEADROOM = 1.25           # target peak/limit after a memory bump
_MEM_HIGH = 0.90           # peak/limit above this ⇒ under-provisioned
_MEM_LOW = 0.40            # peak/limit below this ⇒ over-provisioned (rightsize down)
_THROTTLE_HIGH = 0.25      # CPU throttled >25% of periods ⇒ CPU-starved


@dataclass(frozen=True)
class Usage:
    peak_memory_bytes: int
    memory_limit_bytes: int
    oom_count: int = 0
    cpu_throttle_pct: float = 0.0       # fraction of periods throttled [0..1]
    cpu_limit_millicores: int = 0


@dataclass(frozen=True)
class Recommendation:
    actions: list[str] = field(default_factory=list)   # e.g. ["increase_memory", "increase_cpu"]
    memory_limit_bytes: int = 0                          # 0 ⇒ leave unchanged
    cpu_limit_millicores: int = 0                        # 0 ⇒ leave unchanged
    rationale: list[str] = field(default_factory=list)
    confidence: float = 0.0

    @property
    def is_noop(self) -> bool:
        return not self.actions


def recommend(usage: Usage) -> Recommendation:
    """Ground a resize recommendation in the observed signals. is_noop when nothing warrants change."""
    actions: list[str] = []
    rationale: list[str] = []
    mem_limit = 0
    cpu_limit = 0
    conf = 0.5

    ratio = (usage.peak_memory_bytes / usage.memory_limit_bytes) if usage.memory_limit_bytes else 0.0

    if usage.oom_count > 0:
        mem_limit = int(usage.peak_memory_bytes * _HEADROOM)
        actions.append("increase_memory")
        rationale.append(f"{usage.oom_count} OOMKill(s) observed; raise memory limit to ~{_HEADROOM:g}x peak")
        conf = 0.9
    elif ratio >= _MEM_HIGH:
        mem_limit = int(usage.peak_memory_bytes * _HEADROOM)
        actions.append("increase_memory")
        rationale.append(f"peak/limit {ratio:.0%} ≥ {_MEM_HIGH:.0%}; under-provisioned, bump before OOM")
        conf = 0.75
    elif 0 < ratio <= _MEM_LOW:
        mem_limit = int(usage.peak_memory_bytes * _HEADROOM)
        actions.append("decrease_memory")
        rationale.append(f"peak/limit {ratio:.0%} ≤ {_MEM_LOW:.0%}; over-provisioned, rightsize down")
        conf = 0.6

    if usage.cpu_throttle_pct >= _THROTTLE_HIGH and usage.cpu_limit_millicores:
        # bump CPU proportionally to the throttling pressure
        cpu_limit = int(usage.cpu_limit_millicores * (1.0 + usage.cpu_throttle_pct))
        actions.append("increase_cpu")
        rationale.append(f"CPU throttled {usage.cpu_throttle_pct:.0%} of periods; raise the CPU limit")
        conf = max(conf, 0.8)

    if not actions:
        rationale.append("usage within healthy bands; no resize warranted")
        conf = 0.5

    return Recommendation(actions=actions, memory_limit_bytes=mem_limit,
                          cpu_limit_millicores=cpu_limit, rationale=rationale, confidence=conf)
