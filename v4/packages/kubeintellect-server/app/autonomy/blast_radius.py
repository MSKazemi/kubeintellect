"""Blast-radius composite gate (v5 P3 Trust plane).

The single coherent decision the action path calls before a disruptive change: it composes ALL the
blast-radius controls — the spend/kill/freeze budget, staged propagation (never instant-global), and
the failure-domain + change-schedule budget — into one verdict. Fail-closed and first-denial-wins,
in escalating order of severity, so the strongest brake is reported.

Pure composition over the individual gates (each already tested) — deterministic, unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.autonomy.budget import BudgetDecision
from app.autonomy.failure_domain import DomainDecision
from app.autonomy.staged_propagation import StageDecision


@dataclass(frozen=True)
class BlastRadiusVerdict:
    allow: bool
    batch: list[str] = field(default_factory=list)   # targets cleared to change THIS stage
    reasons: list[str] = field(default_factory=list)  # every brake that fired (empty ⇒ allowed)


def compose(
    *,
    budget: BudgetDecision,
    stage: StageDecision,
    domain: DomainDecision | None = None,
) -> BlastRadiusVerdict:
    """Compose the blast-radius controls. Order: budget → domain → propagation.

    - budget denial (kill switch / freeze / spend / governance) ⇒ deny outright.
    - failure-domain denial (zone cap / maintenance window) ⇒ deny.
    - staged propagation waiting/done ⇒ nothing to apply this tick (allowed but empty batch).
    Otherwise ⇒ allow with the stage's batch.
    """
    reasons: list[str] = []
    if not budget.allow:
        return BlastRadiusVerdict(False, reasons=[f"budget: {budget.reason}"])
    if domain is not None and not domain.allow:
        return BlastRadiusVerdict(False, reasons=[f"failure-domain: {domain.reason}"])
    if stage.waiting:
        reasons.append(f"staged: {stage.reason}")
        return BlastRadiusVerdict(True, batch=[], reasons=reasons)
    if stage.done:
        return BlastRadiusVerdict(True, batch=[], reasons=["staged: all targets applied"])
    return BlastRadiusVerdict(True, batch=list(stage.batch),
                              reasons=[f"staged: {stage.reason}"] if stage.reason else [])
