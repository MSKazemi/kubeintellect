"""Postgres-backed shadow-agreement outcome store (v5 P3, ADR-102).

Closes the promotion loop: the statistical engine (`promotion_engine`) needs per-action-class
shadow outcomes, and this is the durable source for them. When an action class runs in shadow, each
attempt's agreement/success is recorded here; the engine reads them back to decide promote/hold/
demote. Purpose-built table (not the hash-chained flight recorder — these are statistical samples).

``decide_from_store`` is the real wiring: async-fetch a class's outcomes from Postgres, then run the
pure engine decision. The engine's sync ``set_outcome_source`` stays for in-memory/cache use.

``record_autonomous_attempt`` is the table's first production writer. Until 2026-08-28 nothing
outside the tests called ``record_outcome``, so ``outcomes_from_store`` returned ``[]`` for every
class, ``decide`` answered ``hold`` with the honest reason *"n 0 < n_min 20"*, and every action
class sat at its configured rung permanently — a correct decision function fed by an empty store.
The demotion direction is the sharp end of that: ADR-102 is *fast down, slow up*, so a class whose
agreement had collapsed could never be demoted either.
"""

from __future__ import annotations

from typing import Any

from app.autonomy.promotion_engine import EngineDecision, decide
from app.autonomy.promotion_stats import (
    WINDOW_MAX_DAYS,
    WINDOW_MAX_EVENTS,
    Event,
)


async def record_outcome(pool: Any, action_class: str, event: Event) -> None:
    """Persist one shadow-agreement outcome for ``action_class``."""
    await pool.execute(
        "INSERT INTO promotion_outcomes (action_class, ts_days, success, incident_id, "
        "incident_type, critical) VALUES ($1, $2, $3, $4, $5, $6)",
        action_class, event.ts_days, event.success, event.incident_id,
        event.incident_type, event.critical,
    )


async def outcomes_from_store(pool: Any, action_class: str, *, limit: int = 500) -> list[Event]:
    """Read a class's shadow outcomes (most recent first, then chronological) as Events."""
    rows = await pool.fetch(
        "SELECT ts_days, success, incident_id, incident_type, critical FROM promotion_outcomes "
        "WHERE action_class = $1 ORDER BY ts_days DESC LIMIT $2",
        action_class, limit,
    )
    events = [Event(ts_days=r["ts_days"], success=r["success"], incident_id=r["incident_id"],
                    incident_type=r["incident_type"], critical=r["critical"]) for r in rows]
    events.reverse()   # chronological for the windowing math
    return events


async def decide_from_store(
    pool: Any, action_class: str, transition: str, current_rung: str, now_days: float, **kw: Any,
) -> EngineDecision:
    """Fetch a class's real recorded outcomes, then run the pure promotion decision."""
    events = await outcomes_from_store(pool, action_class)
    return decide(action_class, transition, current_rung, now_days, events=events, **kw)


# ── The first production writer ──────────────────────────────────────────────

# One action class, because that is what the earned rung would actually govern: *may the
# watchtower write to the cluster without asking a human?* Per-command classes (ADR-008
# rollback classes) belong to the ACI chokepoint in `tools/aci/mutating.py`, which has no
# production caller yet either — splitting the samples across classes none of which can be
# read would only make each one too small to earn anything.
WATCHTOWER_AUTOFIX = "watchtower-autofix"

# The labels `coordinator._verify_resolution` produces after re-snapshotting the cluster.
# `report_only` is not in this set: an investigation that changed nothing is not an attempt.
GRADED_OUTCOMES = {"resolved", "partial", "regression"}

_SECONDS_PER_DAY = 86400.0


async def record_autonomous_attempt(
    pool: Any,
    *,
    episode_id: str,
    trigger_kind: str,
    outcome: str | None,
    verified: bool | None,
    playbooks: list[str] | None,
    at_seconds: float,
) -> bool:
    """Record one autonomously-attempted, cluster-verified fix. Returns whether a row was written.

    Four conditions, and each rejection is a sample this store must NOT invent:

    * ``trigger_kind == "detector"`` — only the watchtower's own investigations are attempts by
      this action class. A human asking the agent to fix something is not the class earning a rung.
    * ``verified is not None`` — ``_verify_resolution`` returns ``None`` both when verification is
      switched off and when the post-fix cluster read *failed*, and its own comment records what
      happens if that is treated as an answer: an unverified fix recorded as verified. A read is
      most likely to fail right after a disruptive change, which is exactly when this runs, so
      "we could not look" must stay out of the numerator and out of the denominator.
    * ``outcome`` is one of :data:`GRADED_OUTCOMES` — a report-only run attempted nothing.
    * the episode was actually persisted, so ``incident_id`` points at a row an auditor can read.

    ``critical`` is always ``False``: nothing in this path attributes a Sev-1/Sev-2 to an action,
    so the M4 demotion trigger is not fed from here. That is a gap, not a claim of safety — it is
    recorded in the note rather than papered over with a guess.
    """
    if trigger_kind != "detector" or verified is None:
        return False
    if (outcome or "").strip().lower() not in GRADED_OUTCOMES:
        return False
    event = Event(
        ts_days=at_seconds / _SECONDS_PER_DAY,
        success=bool(verified),
        incident_id=episode_id,
        # The fault the detector fired on — the ADR's stratification axis, so `min_types`
        # counts something real rather than a constant this writer chose.
        incident_type=(playbooks or ["generic"])[0] or "generic",
        critical=False,
    )
    await record_outcome(pool, WATCHTOWER_AUTOFIX, event)
    return True


# ── The first production reader ───────────────────────────────────────────────

# The transition whose θ the anti-flap band is measured against. The A3 allowlist grant is an
# operator saying *write without asking* — L4 behaviour in ADR-102 terms — but the gate fires
# before any command exists, so the L3→L4 sub-transition cannot be picked by rollback class.
# `declarative-revert` is the strictest *reversible* one (θ 0.99); `irreversible` is not usable
# here because its θ is None (hard ceiling), which `decide` substitutes with 1.0 — not an
# agreement threshold anyone measured. Strictest-reversible is the fail-safe reading of a blanket
# grant, and this path only ever revokes.
AUTOFIX_TRANSITION = "L3->L4:declarative-revert"
AUTOFIX_GRANTED_RUNG = "L4"


async def autofix_revocation(pool: Any, now_days: float) -> str | None:
    """Has the recorded record revoked the watchtower's unattended-write authority?

    Returns the demotion reason, or ``None`` if the class still holds it. **Revocation only, and
    that asymmetry is not a shortcut — it is the only direction these samples can honestly
    support.** Every sample in `promotion_outcomes` comes from a fix the watchtower was *already*
    allowed to make (`_should_auto_fix`: A3 + allowlist). Promoting on them would be circular —
    the grant produces the evidence that justifies the grant — and it would also deadlock: with
    promotion gating the write, a class with no samples could never take the action that produces
    one. ADR-102 earns rungs from *shadow* agreement, which this system does not yet run. So the
    allowlist stays the only way up; the record is only allowed to take authority away.

    ``sev_attributed`` / ``m4_at_l4`` are left False because nothing on this path attributes a
    Sev-1/Sev-2 to an action — the same gap `record_autonomous_attempt` records rather than
    guesses. The two triggers that do fire are CUSUM (2 postcondition failures within 24 h, at any
    sample size) and the hysteresis band (only where a breach is attributable to failures rather
    than to sample size — see `promotion_stats.hysteresis_breach`).
    """
    decision = await decide_from_store(
        pool, WATCHTOWER_AUTOFIX, AUTOFIX_TRANSITION, AUTOFIX_GRANTED_RUNG, now_days)
    if decision.action != "demote":
        return None
    reason = "; ".join(decision.reasons) or "demotion trigger fired"
    return f"{WATCHTOWER_AUTOFIX} demoted {AUTOFIX_GRANTED_RUNG}→{decision.to_rung}: {reason}"


async def autofix_status(pool: Any, now_days: float) -> dict[str, Any]:
    """What an operator needs to see about the A3 statistical brake, in one read.

    `/v5/status` lists the brakes on the autonomous-write path — kill switch, change freeze,
    spend cap. This one was invisible from the moment it was wired: with
    `KI_V5_STATISTICAL_PROMOTION` on, the flag appears under `active_flags` and nothing on that
    surface says which direction it acts in, whether it has anything to read, or whether it is
    currently holding the gate shut. "Active" next to a flag named *statistical promotion* reads
    as *rungs are being earned here*, which is the one thing this build does not do.

    So the block answers the three questions in order: is it on, can it operate, and what does the
    record currently say. ``samples`` is the count inside the ADR-102 rolling window, not the table
    size — it is the n every threshold in `promotion_stats` is measured against.
    """
    events = await outcomes_from_store(pool, WATCHTOWER_AUTOFIX)
    window = [e for e in events if now_days - e.ts_days <= WINDOW_MAX_DAYS][-WINDOW_MAX_EVENTS:]
    decision = decide(WATCHTOWER_AUTOFIX, AUTOFIX_TRANSITION, AUTOFIX_GRANTED_RUNG, now_days,
                      events=events)
    revoked = decision.action == "demote"
    return {
        "enabled": True,
        "direction": "revoke-only",
        "operating": True,
        "action_class": WATCHTOWER_AUTOFIX,
        "samples": len(window),
        "authority_revoked": revoked,
        "reason": ("; ".join(decision.reasons) if revoked else
                   f"no demotion trigger over {len(window)} sample(s) in the window"),
    }


def autofix_status_unavailable(reason: str) -> dict[str, Any]:
    """The same block when the brake cannot act — flag off, or no outcome store to read.

    ``operating`` is the field to key on, and it is separate from ``enabled`` on purpose: a
    deployment that set the flag without Postgres has it enabled and not operating, and reporting
    only the flag would tell that operator a brake is on their write path when nothing is.
    """
    return {
        "enabled": reason != "flag off",
        "direction": "revoke-only",
        "operating": False,
        "action_class": WATCHTOWER_AUTOFIX,
        "samples": 0,
        "authority_revoked": False,
        "reason": reason,
    }
