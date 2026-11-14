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
from app.autonomy.verify import Target
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


# ── Outcomes and the circuit breaker ─────────────────────────────────────────
# Every autonomous investigation ends in one typed outcome, recorded. The bad ones open a breaker for that
# (namespace, playbook): the next finding of the same kind there is proposed to a human instead of retried unattended.
_BREAKER_TRIPPING = frozenset({"not_fixed", "stuck", "budget_exhausted"})
_breaker: dict[tuple[str, str], float] = {}


def reset_breaker() -> None:
    """Close every breaker (tests, or an operator after fixing what tripped it)."""
    _breaker.clear()


def _breaker_open(namespace: str, playbook: str) -> bool:
    until = _breaker.get((namespace, playbook))
    if until is None:
        return False
    if time.monotonic() >= until:
        del _breaker[(namespace, playbook)]
        return False
    return True


def _conclude(finding: Finding, session_id: str, outcome: str, detail: dict) -> None:
    """Record how an autonomous investigation ended, and open the breaker on a bad ending."""
    payload = {
        "type": "autonomy_outcome", "outcome": outcome, "playbook": finding.playbook,
        "namespace": finding.namespace, "object": finding.object_name, "finding_id": finding.id, **detail,
    }
    try:
        from app.db import flight_recorder

        flight_recorder.record(session_id, "autonomy_outcome", payload)
    except Exception as exc:  # recording must never break the investigation
        logger.warning(f"watchtower: could not record the outcome session={session_id}: {exc}")
    level = logger.info if outcome in ("fixed", "unknown") else logger.warning  # "error" is a warning, not a trip
    level(
        f"watchtower_outcome session={session_id} outcome={outcome} playbook={finding.playbook} "
        f"ns={finding.namespace} object={finding.object_name}"
        + (f" command={detail['command']!r}" if detail.get("command") else "")
    )
    if outcome in _BREAKER_TRIPPING:
        _schedule_lesson(finding, outcome)
    if outcome in _BREAKER_TRIPPING and settings.AUTOFIX_BREAKER_SECONDS > 0:
        _breaker[(finding.namespace, finding.playbook)] = time.monotonic() + settings.AUTOFIX_BREAKER_SECONDS
        logger.warning(
            f"watchtower: A3 breaker open for {finding.playbook} in {finding.namespace} for "
            f"{settings.AUTOFIX_BREAKER_SECONDS}s after outcome={outcome}; findings there are proposed, not auto-fixed"
        )


_lesson_tasks: set[asyncio.Task] = set()


def _schedule_lesson(finding: Finding, outcome: str) -> None:
    """Record a verified bad outcome as a cross-session lesson, without waiting for the database (fire-and-forget)."""
    try:
        from app.db import memory_store

        task = asyncio.get_running_loop().create_task(
            memory_store.record_autonomy_lesson(finding.playbook, finding.namespace, outcome)
        )
        _lesson_tasks.add(task)  # keep a reference until it finishes
        task.add_done_callback(_lesson_tasks.discard)
    except RuntimeError:
        pass  # no running loop (a synchronous caller): nothing to schedule on


async def _conclude_after_verify(
    finding: Finding, target: Target | None, session_id: str, ended: str, detail: dict,
) -> str:
    """A session that stopped (stuck, budget, approval prompt) is judged on its workload first: `fixed` if the pinned
    workload is healthy (with how the session ended), otherwise the stop reason — which opens the breaker."""
    from app.autonomy import verify

    if await verify.verify(target, settings.AUTOFIX_VERIFY_TIMEOUT_SECONDS) == "fixed":
        _conclude(finding, session_id, "fixed", {"ended": ended, **detail})
        return "fixed"
    _conclude(finding, session_id, ended, detail)
    return ended


async def _conclude_pending(finding: Finding, target: Target | None, session_id: str, pending: dict) -> str:
    """An unattended turn stopped at an approval prompt: check the session's own workload before calling it stuck.

    Measured 2026-11-14 (Sonnet 4.6): a session repaired its own missing ConfigMap, then tried to write a sibling
    namespace's, which the namespace binding stopped at a prompt. `needs_approval` alone would hide that its own fault
    was fixed. If the pinned workload is healthy the outcome is `fixed`, with the abandoned command recorded.
    """
    from app.autonomy import verify

    if await verify.verify(target, settings.AUTOFIX_VERIFY_TIMEOUT_SECONDS) == "fixed":
        _conclude(finding, session_id, "fixed", {"ended": "needs_approval", "pending_command": pending.get("command"),
                                                 "pending_risk": pending.get("risk_level")})
        return "fixed"
    _conclude(finding, session_id, "needs_approval", pending)
    return "needs_approval"


async def _pending_approval(session_id: str) -> dict | None:
    """The approval request an unattended turn stopped at (command, risk, always_confirm), or None."""
    try:
        from app.agent.workflow import get_graph

        graph = await get_graph()
        snapshot = await graph.aget_state({"configurable": {"thread_id": session_id}})
        for task in getattr(snapshot, "tasks", None) or ():
            for intr in getattr(task, "interrupts", None) or ():
                value = getattr(intr, "value", intr)
                if isinstance(value, dict) and value.get("type") == "hitl":
                    return {k: value.get(k) for k in ("command", "risk_level", "always_confirm")}
        return None
    except Exception as exc:
        logger.warning(f"watchtower: could not read a pending approval session={session_id}: {exc}")
        return None


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
    # A fault the cluster cannot fix (the playbook says so) is investigated and reported, never auto-fixed.
    from app.agent.playbooks import get_playbook
    playbook = get_playbook(finding.playbook)
    if playbook is not None and not playbook.auto_fixable:
        logger.info("watchtower: A3 auto-fix not applied: playbook %s is not auto_fixable", finding.playbook)
        return False
    if _breaker_open(finding.namespace, finding.playbook):
        logger.info("watchtower: A3 auto-fix not applied: breaker open for %s in %s", finding.playbook, finding.namespace)
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
    if auto_fix and not settings.AUTO_APPROVE_ENABLED:
        # Auto-approve is a demonstration/evaluation mode. Without it an unattended fix turn would only stop at an
        # approval prompt nobody can answer, so the A3 finding is investigated like A2: the fix is proposed.
        logger.info("watchtower: A3 auto-fix not applied: AUTO_APPROVE_ENABLED is off; proposing instead")
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
        # The breaker may have opened while this finding waited for a slot: another finding of the same kind in the
        # same namespace just ended badly (independent review F6: it was only checked before waiting).
        if auto_fix and _breaker_open(finding.namespace, finding.playbook):
            logger.info("watchtower: A3 auto-fix withdrawn after the wait: breaker open for %s in %s",
                        finding.playbook, finding.namespace)
            auto_fix, grounding_gated = False, False
            ask = ask.replace(" Then apply the appropriate fix and verify it worked.",
                              " Propose the exact fix commands but do not execute destructive actions.")
        logger.info(
            f"watchtower_investigating playbook={finding.playbook}"
            f" ns={finding.namespace} object={finding.object_name}"
            f" level={level} auto_fix={auto_fix} session={session_id}"
        )
        try:
            # Pin the finding to its workload BEFORE the fix turn, so the outcome is judged on the same object (uid)
            # and a deletion or a substitute under the same name cannot pass as a repair.
            target: Target | None = None
            if auto_fix and settings.AUTOFIX_VERIFY_TIMEOUT_SECONDS > 0:
                from app.autonomy import verify
                target = await asyncio.to_thread(
                    verify.resolve_target, finding.playbook, finding.namespace, finding.object_name,
                )
            if grounding_gated:
                await _run_turn(ask, session_id, auto_approve=False, bound_namespace=finding.namespace)
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
                    session_id, auto_approve=True, bound_namespace=finding.namespace,
                )
            else:
                await _run_turn(ask, session_id, auto_approve=auto_fix, bound_namespace=finding.namespace)
            from app.agent.workflow import pop_turn_error

            from app.tools.kubectl_tool import _executed_writes, session_is_stuck

            turn_error = pop_turn_error(session_id)
            # Executed writes, not reservations: a write the dry run refused never reached the cluster (review N4).
            if turn_error is not None and not (auto_fix and _executed_writes(session_id) > 0):
                # The model (or the graph) failed before anything was written: this says nothing about the fix.
                # Recorded as `error`, which does not open the breaker.
                logger.warning(f"watchtower_escalated session={session_id} outcome=error: {turn_error}")
                if auto_fix:
                    _conclude(finding, session_id, "error", {"error": turn_error})
                return
            # A failure AFTER writes ran (e.g. a context overflow at the end of a long loop) is judged on the workload
            # like any other ending (independent review F5): the writes may have fixed it, or broken it.
            outcome = await _turn_outcome(session_id)
            detail: dict = {"turn_error": turn_error} if turn_error else {}
            if outcome is None and auto_fix and session_is_stuck(session_id):
                outcome = "stuck"
            if outcome is None and auto_fix:
                pending = await _pending_approval(session_id)
                if pending is not None:
                    # An unattended turn stopped at a prompt nobody can answer: say which command, so the operator
                    # gets more than an empty report — after checking the session's own workload.
                    logger.warning(f"watchtower_escalated session={session_id} outcome=needs_approval")
                    await _conclude_pending(finding, target, session_id, pending)
                    return
            if outcome is not None:
                # The session stopped without a conclusion (stuck, budget). It may still have fixed the fault before
                # it stopped, so the workload decides (independent review F5): fixed, or the stop reason.
                logger.warning(f"watchtower_escalated session={session_id} outcome={outcome}")
                if auto_fix:
                    await _conclude_after_verify(finding, target, session_id, outcome, detail)
                return
            logger.info(f"watchtower_done session={session_id}")
            if auto_fix:
                await _record_fix_outcome(finding, target, session_id, detail)
        except Exception as exc:
            logger.warning(f"watchtower: investigation failed session={session_id}: {exc}")


def _grounding_gates_autofix() -> bool:
    """ADR-009 is in force for autonomous fixes. Cortex-only: the grounding verdict is produced
    by the Cortex graph's `ground_check` node, which the V2 graph does not have — gating A3 on a
    verdict that can never exist would silently disable it."""
    return settings.SELF_GOVERN_ENABLED and settings.CORTEX_V4_ENABLED


async def _run_turn(
    ask: str, session_id: str, *, auto_approve: bool, bound_namespace: str | None = None,
) -> None:
    """Run one watchtower turn through the chat session machinery and drain its stream.

    ``bound_namespace`` is the namespace the investigation was opened for. Authority to mutate without a
    human (``auto_approve``) was decided for that namespace by the ladder and the A3 allowlist, so the tool
    layer applies it there and nowhere else.
    """
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
        bound_namespace=bound_namespace,
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


async def _turn_outcome(session_id: str) -> str | None:
    """The `ki_outcome` mark on the session's last AI message (e.g. "budget_exhausted"), or None.

    An escalation must not be recorded as a finished investigation, nor followed by a "the fix held" re-check.
    """
    try:
        from app.agent.workflow import get_graph

        graph = await get_graph()
        snapshot = await graph.aget_state({"configurable": {"thread_id": session_id}})
        messages = (dict(snapshot.values) if snapshot and snapshot.values else {}).get("messages") or []
        for message in reversed(messages):
            if getattr(message, "type", "") == "ai":
                outcome = (getattr(message, "additional_kwargs", None) or {}).get("ki_outcome")
                return outcome if isinstance(outcome, str) else None
        return None
    except Exception as exc:
        logger.warning(f"watchtower: could not read the turn outcome session={session_id}: {exc}")
        return None


async def _record_fix_outcome(
    finding: Finding, target: Target | None, session_id: str, detail: dict | None = None,
) -> str:
    """Judge an autonomous fix on the finding's own workload, not on the model's report.

    Measured 2026-11-14: a session reported "The job has been fixed" after verifying a stub it had created, while the
    Job the detector fired on stayed Failed. `fixed` and `unknown` (verification off or unreadable — never claimed
    as fixed) keep the existing "fix held" re-check; `not_fixed` is logged as a warning and gets none.
    """
    from app.autonomy import verify

    outcome = await verify.verify(target, settings.AUTOFIX_VERIFY_TIMEOUT_SECONDS)
    _conclude(finding, session_id, outcome, dict(detail or {}))
    if outcome == "not_fixed":
        logger.warning(
            f"watchtower_fix_not_verified session={session_id} playbook={finding.playbook} "
            f"ns={finding.namespace} object={finding.object_name} — the workload is still unhealthy, gone or replaced"
        )
        return outcome
    logger.info(f"watchtower_fix_verified session={session_id} outcome={outcome}")
    await _schedule_post_fix_recheck(finding)
    return outcome


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
