"""Watchtower — findings open their own investigations (ADR-003, A1+).

The inversion at the heart of V4: the agent does not wait to be asked. When a
compiled detector fires (zero-token), the watchtower resolves the namespace's
autonomy level and, at A1+, schedules an autonomous investigation through the
SAME session machinery chat uses (run_session → cortex graph → flight
recorder → episode). At A3 — and only for explicitly allowlisted
(playbook, namespace) pairs — the investigation runs with the HITL gate
bypassed so the proposed fix executes, followed by the standard R4
post-verification.

Guard rails:
- per-(playbook, ns, object) cooldown so a flapping condition doesn't spawn
  investigation storms;
- a global concurrency cap;
- protected namespaces are pinned to A0 by the ladder;
- the autonomous identity runs with a configured role (default operator) —
  the role ceiling still applies inside the tools;
- with SELF_GOVERN_ENABLED (Cortex graph), an A3 fix runs only after the
  diagnosis passed the ADR-009 grounding check: turn 1 diagnoses and proposes
  (HITL-gated), turn 2 applies — and turn 2 is skipped when any claim was
  unsupported, the check errored, or its verdict cannot be read.
"""
from __future__ import annotations

import asyncio
import time

from app.autonomy.ladder import a3_allowed, at_least, level_for_namespace
from app.core.config import settings
from app.detectors.models import Finding
from app.utils.logger import get_logger

logger = get_logger(__name__)

_COOLDOWN_SECONDS = 1800.0
_MAX_CONCURRENT = 2
# Memory V5 P6 (ADR-017): after an autonomous fix, re-verify it held this many seconds
# later via prospective memory ("did the fix hold?"). Fired by the consolidation scheduler.
_RECHECK_DELAY_SECONDS = 900.0

_recent: dict[tuple[str, str, str], float] = {}
_semaphore: asyncio.Semaphore | None = None


def on_finding(finding: Finding) -> None:
    """Detector-engine callback. Sync, non-blocking, never raises."""
    try:
        if not settings.WATCHTOWER_ENABLED:
            return
        level = level_for_namespace(finding.namespace)
        if not at_least(level, "A1"):
            return
        key = (finding.playbook, finding.namespace, finding.object_name)
        now = time.time()
        if now - _recent.get(key, 0.0) < _COOLDOWN_SECONDS:
            return
        _recent[key] = now
        # P4 predictive pre-capture: for an imminent PREDICTED failure, arm recorders before death
        # so the post-mortem has evidence. Additive; default-off; never blocks the callback.
        if settings.CORTEX_V5_ENABLED and settings.KI_V5_PREDICTIVE_PRECAPTURE:
            from app.sensorium.pre_capture import plan_pre_capture
            plan = plan_pre_capture(finding, eta_threshold_min=settings.KI_V5_PRECAPTURE_ETA_MIN)
            if plan is not None:
                logger.info("watchtower: pre-capture armed for %s/%s → %s",
                            plan.namespace, plan.target, plan.actions)
        asyncio.get_running_loop().create_task(_investigate(finding, level))
    except Exception as exc:
        logger.warning(f"watchtower: on_finding error: {exc}")


def _should_auto_fix(finding: Finding, level: str) -> bool:
    """A3 auto-fix is allowed only for realized failures on allowlisted pairs.

    Safety contract (ADR-010): a *predicted* finding is lower-confidence than a
    realized one — it may pre-empt (investigate/advise) but must NEVER drive a
    destructive autonomous action, regardless of level or allowlist.
    """
    if getattr(finding, "severity", "warning") == "predicted":
        return False
    if not (level == "A3" and a3_allowed(finding.playbook, finding.namespace)):
        return False
    # P3 blast-radius gate: an additional fail-closed brake (kill switch / change freeze) on top of
    # the allowlist. Off ⇒ no change to A3 behavior.
    from app.autonomy.budget import auto_write_permitted
    decision = auto_write_permitted()
    if not decision.allow:
        logger.info("watchtower: A3 auto-fix denied by blast-radius gate: %s", decision.reason)
        return False
    return True


async def _autofix_revoked() -> str | None:
    """ADR-102 brake: has `watchtower-autofix`'s recorded record taken its write authority away?

    Behind `KI_V5_STATISTICAL_PROMOTION` (default off), and **revocation only** — the recorded
    outcomes can close the A3 gate, never open it. See `promotion_source.autofix_revocation` for
    why that asymmetry is the only honest reading of these samples. Off ⇒ A3 is unchanged.

    Three outcomes, and the difference between the last two matters:

    * flag off ⇒ ``None``. Nothing changes.
    * no store configured (`MEMORY_HIERARCHY_ENABLED` off, or the pool not up yet) ⇒ ``None``,
      with a warning. Failing closed here would silently disable A3 on any deployment that turned
      on a *promotion* flag without Postgres — an outcome nobody would attribute to this flag.
      The warning is the point: the operator learns the brake is not operating.
    * the store exists and could not be read ⇒ **revoke**. A brake whose evidence cannot be read
      is not a brake, and `promotion_engine.read_outcomes` already records what happens when an
      unreadable source is allowed to read as a clean one: fast-down-slow-up means a class whose
      agreement has collapsed is held at its rung, silently, by the read failure itself.
    """
    if not settings.KI_V5_STATISTICAL_PROMOTION:
        return None
    from app.memory import service as _memory_service

    pool = _memory_service._pool
    if pool is None:
        logger.warning(
            "watchtower: KI_V5_STATISTICAL_PROMOTION is on but there is no outcome store — "
            "the A3 statistical brake is NOT operating; A3 is governed by the allowlist alone")
        return None
    try:
        from app.autonomy.promotion_source import autofix_revocation

        return await autofix_revocation(pool, time.time() / 86400.0)
    except Exception as exc:
        logger.warning("watchtower: could not read the promotion outcome store (%s) — "
                       "revoking A3 auto-fix for this finding rather than assuming a clean record",
                       exc)
        return f"outcome store unreadable: {exc}"


async def _investigate(finding: Finding, level: str) -> None:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

    auto_fix = _should_auto_fix(finding, level)
    if auto_fix:
        revocation = await _autofix_revoked()
        if revocation is not None:
            logger.warning("watchtower: A3 auto-fix revoked by the recorded record: %s", revocation)
            auto_fix = False
    predicted = getattr(finding, "severity", "warning") == "predicted"
    # ADR-009 (SELF_GOVERN_ENABLED): an auto-fix is split into a diagnose-and-propose turn and,
    # only if that diagnosis is grounded, an apply turn. Off ⇒ one turn, exactly as before.
    grounding_gated = auto_fix and _grounding_gates_autofix()
    session_id = f"auto-{finding.id}"
    if predicted:
        ask = (
            f"[autonomous investigation — PREDICTED failure {finding.playbook}] "
            f"The trend detector projects a failure for object '{finding.object_name}' "
            f"in namespace '{finding.namespace}' in ~{finding.eta_minutes}m "
            f"(evidence: {finding.evidence}). Diagnose what is trending and "
            "recommend a pre-emptive action. Do NOT execute destructive fixes."
        )
    else:
        ask = (
            f"[autonomous investigation — triggered by detector {finding.playbook}] "
            f"The detector fired for object '{finding.object_name}' in namespace "
            f"'{finding.namespace}' (evidence: {finding.evidence}). "
            "Diagnose the root cause and report it concisely."
        )
        if grounding_gated:
            # ADR-009: the diagnosis must be grounded BEFORE it may drive a fix, so the fix is
            # not requested in this turn. This turn proposes (human-gated, like A2); a second,
            # auto-approved turn applies it only if the diagnosis passed `ground_check`.
            ask += " Propose the exact fix commands but do not execute destructive actions."
        elif auto_fix:
            ask += " Then apply the appropriate fix and verify it worked."
        elif at_least(level, "A2"):
            ask += " Propose the exact fix commands but do not execute destructive actions."

    async with _semaphore:
        logger.info(
            f"watchtower_investigating playbook={finding.playbook}"
            f" ns={finding.namespace} object={finding.object_name}"
            f" level={level} auto_fix={auto_fix} session={session_id}"
        )
        try:
            if grounding_gated:
                await _run_turn(ask, session_id, auto_approve=False)
                grounding = await _turn_grounding(session_id)
                from app.cortex.verify import grounding_permits_autofix
                if not grounding_permits_autofix(grounding):
                    # Demoted to advisory (or the check errored / did not run): the proposal
                    # stands in the report, nothing executes. Never raised on an unchecked
                    # diagnosis.
                    logger.warning(
                        f"watchtower: A3 auto-fix withheld — diagnosis not grounded "
                        f"(grounding={grounding}) session={session_id}"
                    )
                    return
                await _run_turn(
                    "Apply the fix you proposed above and verify it worked.",
                    session_id, auto_approve=True,
                )
            else:
                await _run_turn(ask, session_id, auto_approve=auto_fix)
            logger.info(f"watchtower_done session={session_id}")
            if auto_fix:
                await _schedule_post_fix_recheck(finding)
        except Exception as exc:
            logger.warning(f"watchtower: investigation failed session={session_id}: {exc}")


def _grounding_gates_autofix() -> bool:
    """ADR-009 is in force for autonomous fixes. Cortex-only: the grounding verdict is produced
    by the Cortex graph's `ground_check` node, which the V2 graph does not have — gating A3 on a
    verdict that can never exist would silently disable it."""
    return settings.SELF_GOVERN_ENABLED and settings.CORTEX_V4_ENABLED


async def _run_turn(ask: str, session_id: str, *, auto_approve: bool) -> None:
    """Run one watchtower turn through the chat session machinery and drain its stream."""
    from app.agent.workflow import run_session
    from app.streaming.emitter import prepare_session, stream

    prepare_session(session_id)
    runner = asyncio.create_task(run_session(
        ask,
        session_id,
        user_id="watchtower",
        user_role=settings.WATCHTOWER_ROLE,
        auto_approve=auto_approve,
        # In-process, detector-triggered: the one caller entitled to sensor trust.
        trigger_source="detector",
    ))
    # Drain the event stream (no human is attached); the flight
    # recorder + episode write are the durable outputs.
    async for _event in stream(session_id, heartbeat_interval=5.0):
        pass
    await runner


async def _turn_grounding(session_id: str) -> dict | None:
    """The `ground_check` outcome of the session's last turn, or None when it cannot be read.

    None is never permission: `grounding_permits_autofix(None)` is False, so a checkpoint that
    cannot be read withholds the fix rather than assuming a grounded diagnosis.
    """
    try:
        from app.agent.workflow import get_graph

        graph = await get_graph()
        snapshot = await graph.aget_state({"configurable": {"thread_id": session_id}})
        values = dict(snapshot.values) if snapshot and snapshot.values else {}
        grounding = values.get("grounding")
        return grounding if isinstance(grounding, dict) else None
    except Exception as exc:
        logger.warning(f"watchtower: could not read the grounding verdict session={session_id}: {exc}")
        return None


async def _schedule_post_fix_recheck(finding: Finding) -> None:
    """After an autonomous fix, record a prospective re-check that the fix held (ADR-017,
    R6.4). Gated by MEMORY_PROSPECTIVE; fire-and-forget — never breaks the investigation."""
    if not settings.MEMORY_PROSPECTIVE:
        return
    try:
        import time as _time

        from app.cluster_id import get_cluster_id
        from app.memory import prospective

        await prospective.schedule_recheck(
            cluster_id=get_cluster_id(),
            namespace=finding.namespace,
            condition=(
                f"Re-verify the autonomous fix for '{finding.playbook}' on object "
                f"'{finding.object_name}' in namespace '{finding.namespace}' still holds."
            ),
            check_query=finding.playbook,
            dedup_key=f"recheck:{finding.playbook}:{finding.namespace}:{finding.object_name}",
            due_at=_time.time() + _RECHECK_DELAY_SECONDS,
            created_by="watchtower",
        )
    except Exception as exc:
        logger.warning(f"watchtower: post-fix re-check scheduling failed: {exc}")


def reset_cooldowns() -> None:
    """Test helper."""
    _recent.clear()
