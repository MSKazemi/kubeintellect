"""Security-outcome eval (v5 P3, A-CH-04-20).

The gate that makes the write approaches shippable rather than claimed: a fix-PR is only
promotable if it introduces **zero new policy violations** and does not regress the security
posture. Deterministic set arithmetic over policy-violation identifiers (from Kyverno/VAP/kube-
bench/etc. reports) taken before and after the proposed change — no LLM, auditor-reproducible.

Pairs with the statistical promotion engine: a positive security-outcome (no new violations, net
reduction) is a promotion signal; any introduced violation is a hard promotion blocker.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SecurityOutcome:
    introduced: list[str] = field(default_factory=list)   # violations the change ADDED (bad)
    resolved: list[str] = field(default_factory=list)     # violations the change removed (good)

    @property
    def net_delta(self) -> int:
        """Change in violation count (negative = net improvement)."""
        return len(self.introduced) - len(self.resolved)

    @property
    def clean(self) -> bool:
        """No new violations introduced — the hard promotion precondition."""
        return not self.introduced


def score_security_outcome(before: set[str], after: set[str]) -> SecurityOutcome:
    """Diff the violation sets: what the change introduced vs resolved."""
    return SecurityOutcome(
        introduced=sorted(after - before),
        resolved=sorted(before - after),
    )


def gates_promotion(outcome: SecurityOutcome, *, require_net_improvement: bool = False) -> bool:
    """Whether a change class may be promoted given its security outcome.

    Always requires zero introduced violations. With ``require_net_improvement`` it additionally
    demands a net reduction (stricter gate for security-focused write classes).
    """
    if not outcome.clean:
        return False
    if require_net_improvement:
        return outcome.net_delta < 0
    return True


def aggregate(outcomes: list[SecurityOutcome]) -> SecurityOutcome:
    """Fold a corpus of outcomes into one (for a whole misconfig-fix eval run)."""
    introduced: list[str] = []
    resolved: list[str] = []
    for o in outcomes:
        introduced.extend(o.introduced)
        resolved.extend(o.resolved)
    return SecurityOutcome(introduced=sorted(introduced), resolved=sorted(resolved))
