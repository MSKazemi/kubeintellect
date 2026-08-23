"""Transactional, oracle-verified mitigation (v5 P3, A-CH-02-08 / STRATUS-style TNR).

Apply → verify a machine-checkable postcondition → **roll back automatically if the oracle fails**.
A mitigation either commits (postcondition holds) or leaves the cluster as it was (rolled back) —
never a half-applied change nobody verified. This is the executor the trust plane invokes AFTER a
mutation is authorized (chokepoint decision == "auto") and server-side dry-run validated.

The apply seam and the postcondition oracle are injectable, so the transaction logic is unit-
testable with no cluster; the live apply goes through run_kubectl with hitl_bypass (the autonomous
path — reachable only for chokepoint-authorized, reversible classes).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.tools.aci import kubectl_output as _out
from app.tools.aci.postcondition import PostconditionResult

# apply(command) -> combined output. Injectable; default runs the real (auto-approved) mutation.
ApplyFn = Callable[[str], str]
# postcondition() -> the health-oracle verdict after the change.
PostconditionFn = Callable[[], PostconditionResult]

COMMITTED = "committed"
ROLLED_BACK = "rolled_back"
APPLY_FAILED = "apply_failed"
APPLY_REFUSED = "apply_refused"
VERIFY_FAILED_NO_ROLLBACK = "verify_failed_no_rollback"
VERIFY_INCONCLUSIVE = "verify_inconclusive"

# Outcome of reading the apply seam's output. The classifier lives in `kubectl_output` because
# the verification side (`postcondition.py`) has to read the same strings, and the two must never
# disagree about what a given `run_kubectl` result meant.
REFUSED = _out.REFUSED  # KubeIntellect blocked it — nothing was sent to the cluster
FAILED = _out.FAILED  # kubectl itself reported an error
APPLIED = "applied"  # nothing says it failed; the oracle is the authority from here


def classify_apply(output: str) -> str:
    """REFUSED / FAILED / APPLIED for one apply-seam result.

    Anything unrecognised is APPLIED — not because it certainly landed, but because the
    postcondition oracle is a better authority on that than a keyword, and this classifier
    exists only to catch the two cases where running the oracle would be wrong: a command
    KubeIntellect refused (nothing to verify, nothing to roll back) and one kubectl rejected.
    """
    verdict = _out.classify_output(output)
    return APPLIED if verdict == _out.OK else verdict


def _default_apply(command: str) -> str:
    from app.tools.kubectl_tool import run_kubectl
    # hitl_bypass: the autonomous execution path (only chokepoint-authorized commands reach here).
    return run_kubectl.invoke({"command": command}, config={"configurable": {"hitl_bypass": True}})


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    postcondition: PostconditionResult | None = None
    apply_output: str = ""
    rollback_output: str = ""


def execute_transactional(
    command: str,
    postcondition: PostconditionFn,
    *,
    rollback_command: str | None = None,
    apply_fn: ApplyFn | None = None,
) -> ExecutionResult:
    """Apply ``command``, verify the ``postcondition`` oracle, roll back on failure.

    - KubeIntellect refused the command ⇒ APPLY_REFUSED (nothing ran, nothing to roll back).
    - kubectl reported an error ⇒ APPLY_FAILED (nothing to roll back).
    - the oracle could not evaluate ⇒ VERIFY_INCONCLUSIVE (escalate; **no rollback**).
    - postcondition holds ⇒ COMMITTED.
    - postcondition fails + rollback_command ⇒ run it ⇒ ROLLED_BACK.
    - postcondition fails + no rollback_command ⇒ VERIFY_FAILED_NO_ROLLBACK (escalate).

    The refused branch is not a nicety. Every safety gate in the project — role denial, protected
    namespace, cluster-wide block, unsupported verb — answers with a string, and the substring
    test this replaced read all five as success: the executor then failed the oracle (of course:
    nothing had changed) and issued the **rollback command against the live cluster**, undoing
    something that was never done. A multi-document apply that fails halfway is the one case
    APPLY_FAILED can under-report; kubectl's own error line is all this seam has to go on.
    """
    apply_fn = apply_fn or _default_apply
    out = apply_fn(command)
    outcome = classify_apply(out)
    if outcome == REFUSED:
        return ExecutionResult(APPLY_REFUSED, apply_output=out.strip()[:2000])
    if outcome == FAILED:
        return ExecutionResult(APPLY_FAILED, apply_output=out.strip()[:2000])

    verdict = postcondition()
    if not verdict.evaluated:
        # The oracle could not look. That is not a failed mitigation, and rolling back on it
        # would mutate a cluster we have just been told we cannot read. Escalate instead.
        return ExecutionResult(VERIFY_INCONCLUSIVE, verdict, out.strip()[:2000])
    if verdict.met:
        return ExecutionResult(COMMITTED, verdict, out.strip()[:2000])

    if rollback_command is None:
        return ExecutionResult(VERIFY_FAILED_NO_ROLLBACK, verdict, out.strip()[:2000])
    rb = apply_fn(rollback_command)
    return ExecutionResult(ROLLED_BACK, verdict, out.strip()[:2000], rb.strip()[:2000])
