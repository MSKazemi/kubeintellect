"""Blast-radius / spend budget gate (v5 P3 Trust plane; 04-trust §6).

An *additional* gate on autonomous writes, layered on top of the ADR-003 ladder + A3 allowlist —
never a replacement. Three fail-closed brakes:

- **Kill switch** — engaged ⇒ deny every autonomous write (settings flag OR a runtime toggle for an
  operator break-glass "stop the agent" without a redeploy).
- **Change freeze** — deny-by-default during a freeze window (maintenance/holiday change moratoria).
- **Spend cap** — deny-before-breach: an action whose *projected* cost would push the scope's spend
  over its cap is denied BEFORE it runs (REQ-security-finops-16), given an injected usage figure.

Asymmetry (04-trust §6): FAIL-CLOSED for agent *write authority* (governance unreachable ⇒ no
auto-write), but this gate never touches the data/cluster plane, so it can never block a running
workload or a human break-glass — those are independent of the agent stack.

Pure and deterministic (clock/usage injected) — fully unit-testable. Wired into the watchtower's
A3 decision behind ``KI_V5_BLAST_RADIUS_BUDGET``; off ⇒ the ladder is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings

# Runtime kill switch — an operator can engage/disengage without a redeploy (composed OR the flag).
_runtime_kill = False


@dataclass(frozen=True)
class BudgetDecision:
    allow: bool
    reason: str = ""


def engage_kill_switch() -> None:
    global _runtime_kill
    _runtime_kill = True


def disengage_kill_switch() -> None:
    global _runtime_kill
    _runtime_kill = False


def kill_switch_engaged() -> bool:
    return bool(_runtime_kill or settings.KI_V5_KILL_SWITCH)


def check_spend(current: float, projected: float, cap: float | None) -> BudgetDecision:
    """Deny-before-breach: block if the projected spend would push the scope over ``cap``.

    A non-positive/None cap means unlimited. Deterministic; ``current``/``projected`` are supplied
    by the caller (a real usage source is a separate concern, like the change ledger).
    """
    if cap is None or cap <= 0:
        return BudgetDecision(True)
    if current + projected > cap:
        return BudgetDecision(False, f"projected spend {current + projected:.4g} > cap {cap:.4g}")
    return BudgetDecision(True)


def in_change_freeze(now_epoch: float, windows: list[tuple[float, float]]) -> bool:
    """True iff ``now`` falls inside any [start, end) freeze window."""
    return any(start <= now_epoch < end for start, end in windows)


def gate_write(
    *,
    governance_ok: bool = True,
    current_spend: float = 0.0,
    projected_spend: float = 0.0,
    spend_cap: float | None = None,
    now_epoch: float | None = None,
    freeze_windows: list[tuple[float, float]] | None = None,
) -> BudgetDecision:
    """Full composable write gate. Fail-closed: any brake or unreachable governance ⇒ deny.

    Precedence: governance → kill switch → change freeze → spend. First denial wins.
    """
    if not governance_ok:
        return BudgetDecision(False, "governance unreachable — fail-closed (no write authority)")
    if kill_switch_engaged():
        return BudgetDecision(False, "kill switch engaged")
    if now_epoch is not None and freeze_windows and in_change_freeze(now_epoch, freeze_windows):
        return BudgetDecision(False, "change freeze in effect")
    return check_spend(current_spend, projected_spend, spend_cap)


def auto_write_permitted() -> BudgetDecision:
    """Settings-driven gate for the watchtower A3 path (no usage source needed).

    Off ⇒ always allow (ladder unchanged). On ⇒ deny on kill switch or a deny-by-default freeze.
    (The spend cap is enforced by ``gate_write`` where an actual usage figure is available.)
    """
    if not settings.KI_V5_BLAST_RADIUS_BUDGET:
        return BudgetDecision(True)
    if kill_switch_engaged():
        return BudgetDecision(False, "kill switch engaged")
    if settings.KI_V5_CHANGE_FREEZE:
        return BudgetDecision(False, "change freeze in effect")
    return BudgetDecision(True)
