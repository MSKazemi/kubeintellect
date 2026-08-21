"""Predictive pre-capture (v5 P4, A-CH-20-16).

When a prediction says a workload is about to die, arm the recorders BEFORE death — raise log
verbosity, take a CRIU checkpoint, capture a heap dump — so the post-mortem has the evidence that
would otherwise be lost when the pod terminates. Converts the CH-09 "always-on high-verbosity" cost
collision into TARGETED spend: only imminent (short-ETA) predictions arm capture, never realized
findings (too late) or far-off ones (wasteful).

Pure planning logic + world-model types — unit-testable; the actual recorder-arming dispatch is a
pluggable side effect (like prospective memory / watchdog dispatch).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# pre-capture actions (what to arm before the predicted death)
LOG_VERBOSITY = "raise_log_verbosity"
CRIU_CHECKPOINT = "arm_criu_checkpoint"   # KEP-2008 container checkpoint
HEAP_DUMP = "capture_heap_dump"

_DEFAULT_ACTIONS = [LOG_VERBOSITY, CRIU_CHECKPOINT]


@dataclass(frozen=True)
class PreCapturePlan:
    target: str
    namespace: str
    actions: list[str] = field(default_factory=list)
    reason: str = ""


def plan_pre_capture(
    finding: Any, *, eta_threshold_min: float = 15.0, actions: list[str] | None = None,
) -> PreCapturePlan | None:
    """Plan pre-capture for an imminent predicted finding, else None (targeted spend).

    Only fires for a ``severity=="predicted"`` finding whose ETA is within ``eta_threshold_min`` —
    a realized finding is too late, and a far-off prediction would waste capture budget.
    """
    if getattr(finding, "severity", "warning") != "predicted":
        return None
    eta = getattr(finding, "eta_minutes", None)
    if not isinstance(eta, (int, float)) or eta <= 0 or eta > eta_threshold_min:
        return None
    return PreCapturePlan(
        target=getattr(finding, "object_name", "?"),
        namespace=getattr(finding, "namespace", "?"),
        actions=list(actions or _DEFAULT_ACTIONS),
        reason=(f"predicted {getattr(finding, 'playbook', 'failure')} in ~{eta:.0f}m — arm recorders "
                "before death so the post-mortem has evidence"),
    )
