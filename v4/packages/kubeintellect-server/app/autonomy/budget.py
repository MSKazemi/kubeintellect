"""Blast-radius / spend budget gate (v5 P3 Trust plane; 04-trust §6).

An *additional* gate on autonomous writes, layered on top of the ADR-003 ladder + A3 allowlist —
never a replacement. Three fail-closed brakes:

- **Kill switch** — engaged ⇒ deny every autonomous write (settings flag OR an in-process runtime
  toggle). ⚠️ The toggle has **no operator surface**: no API route, no `kq` command, no Helm value
  calls ``engage_kill_switch``, and ``_runtime_kill`` is a per-process global that N replicas would
  not share. Engaging a brake today means setting the env var and restarting — do not document it,
  or plan an incident around it, as a no-restart break-glass until both are fixed.
- **Change freeze** — deny-by-default during a maintenance/holiday change moratorium (settings flag
  OR an injected [start, end) window), read through one ``change_freeze_active`` so both gates agree.
- **Spend cap** — deny-before-breach: an action whose *projected* cost would push the scope's spend
  over its cap is denied BEFORE it runs (REQ-security-finops-16), given an injected usage figure.

Asymmetry (04-trust §6): FAIL-CLOSED for agent *write authority* (governance unreachable ⇒ no
auto-write), but this gate never touches the data/cluster plane, so it can never block a running
workload or a human break-glass — those are independent of the agent stack.

Pure and deterministic (clock/usage injected) — fully unit-testable. Wired into the watchtower's
A3 decision. The kill switch and change freeze always apply, because they are an operator saying
*stop* rather than a feature to opt into. With no brake engaged the ladder is unchanged, which is
every default deployment.
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


def change_freeze_active(
    now_epoch: float | None = None,
    windows: list[tuple[float, float]] | None = None,
) -> bool:
    """True iff a change freeze is in effect — declared by the operator, or by a window.

    The counterpart of ``kill_switch_engaged``, and it exists for the same reason. Both brakes
    have two sources (a settings flag an operator sets, and a runtime/injected one), and both
    are read by two gates. The kill switch got one function that composes its sources, so both
    gates could not disagree. The change freeze did not: ``auto_write_permitted`` read the flag
    and ``gate_write`` read the injected windows, so until 2026-08-20 a declared
    ``KI_V5_CHANGE_FREEZE`` stopped the watchtower and left ``gate_write`` — the ACI write
    chokepoint, which passes it no windows and no clock — returning *allow*.
    """
    if settings.KI_V5_CHANGE_FREEZE:
        return True
    return bool(now_epoch is not None and windows and in_change_freeze(now_epoch, windows))


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
    if change_freeze_active(now_epoch, freeze_windows):
        return BudgetDecision(False, "change freeze in effect")
    return check_spend(current_spend, projected_spend, spend_cap)


def auto_write_permitted() -> BudgetDecision:
    """Settings-driven gate for the watchtower A3 path (no usage source needed).

    Deny on an engaged kill switch or a declared change freeze; otherwise allow. (The spend cap
    is enforced by ``gate_write``, where an actual usage figure is available.)

    **Neither brake is gated on ``KI_V5_BLAST_RADIUS_BUDGET``, and that is deliberate.** A kill
    switch and a change freeze are not features to opt into — they are an operator saying *stop*.
    Until 2026-08-20 this function returned "allow" on the feature flag *before* consulting
    either, so with the flag at its default (`False`) an engaged kill switch did nothing while
    ``GET /v1/v5/status`` reported ``kill_switch_engaged: true`` and ``kq v5-status`` printed it
    in red. An operator breaking glass mid-incident was told the agent had stopped writing while
    it went on auto-fixing.

    Default behaviour is unchanged: both brakes are off unless somebody turns one on, so
    "flag off ⇒ the ladder is unchanged" still holds for every deployment that has not asked for
    a stop. ``KI_V5_BLAST_RADIUS_BUDGET`` is left with no consumer at all — ``gate_write`` never
    read it either (it takes the cap and windows from its caller), so the flag's only ever effect
    was the short-circuit removed here. It is recorded in ``UNWIRED_EXPERIMENTAL_FLAGS`` and
    ``/v1/v5/status`` now reports it under ``set_but_unwired_flags`` if an operator sets it.
    """
    if kill_switch_engaged():
        return BudgetDecision(False, "kill switch engaged")
    if change_freeze_active():
        return BudgetDecision(False, "change freeze in effect")
    return BudgetDecision(True)
