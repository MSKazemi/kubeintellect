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
    # Whether the signals were sufficient to judge at all. `is_noop` cannot tell "assessed and
    # healthy" from "we have no observation of this container" — and before 2026-08-24 both
    # rendered the identical sentence at the identical confidence, as did a container with no
    # memory limit whatsoever. A recommender whose silence is ambiguous is worse than one that
    # says nothing: `is_noop` reads as an all-clear.
    assessed: bool = True

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

    # An absent limit is not a ratio of zero — it is the absence of a ratio, and zero is the
    # single most reassuring value the scale has. `None` keeps the two apart.
    ratio = (usage.peak_memory_bytes / usage.memory_limit_bytes) if usage.memory_limit_bytes else None
    # Likewise a peak of zero is "we never observed this container", not "it used nothing".
    observed = usage.peak_memory_bytes > 0

    if usage.oom_count > 0:
        mem_limit = int(usage.peak_memory_bytes * _HEADROOM)
        actions.append("increase_memory" if usage.memory_limit_bytes else "set_memory_limit")
        verb = "raise" if usage.memory_limit_bytes else "set"
        rationale.append(f"{usage.oom_count} OOMKill(s) observed; {verb} memory limit to ~{_HEADROOM:g}x peak")
        conf = 0.9
    elif not observed:
        rationale.append(
            "no peak-memory observation for this container — nothing here is a statement "
            "about whether its limits are right"
            + ("" if usage.memory_limit_bytes else ", and it has no memory limit set")
        )
    elif ratio is None:
        # Unbounded, with a peak to size against. This is the highest-risk memory configuration
        # there is — one leak evicts every pod on the node — and it used to land in the
        # "within healthy bands" bucket because 0.0 is below every threshold.
        mem_limit = int(usage.peak_memory_bytes * _HEADROOM)
        actions.append("set_memory_limit")
        rationale.append(
            f"no memory limit is set; the container is unbounded and can evict its node. "
            f"Observed peak {usage.peak_memory_bytes} B — set a limit at ~{_HEADROOM:g}x peak"
        )
        conf = 0.7
    elif ratio >= _MEM_HIGH:
        mem_limit = int(usage.peak_memory_bytes * _HEADROOM)
        actions.append("increase_memory")
        rationale.append(f"peak/limit {ratio:.0%} ≥ {_MEM_HIGH:.0%}; under-provisioned, bump before OOM")
        conf = 0.75
    elif ratio <= _MEM_LOW:
        # `observed` guarantees peak > 0 and this branch guarantees a limit, so ratio > 0.
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

    # "Assessed" is a claim about the evidence, not about the verdict. Acting proves we had
    # enough; so does an in-band ratio. Nothing else does.
    assessed = bool(actions) or (observed and ratio is not None)

    if not actions:
        if assessed:
            rationale.append("usage within healthy bands; no resize warranted")
            conf = 0.5
        else:
            # Deliberately NOT 0.5. The old value said "a considered judgement of no change";
            # there was no judgement, because there was nothing to judge.
            conf = 0.0

    return Recommendation(actions=actions, memory_limit_bytes=mem_limit,
                          cpu_limit_millicores=cpu_limit, rationale=rationale,
                          confidence=conf, assessed=assessed)
