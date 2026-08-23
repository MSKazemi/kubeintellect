"""context_fetcher node — pre-fetches pod + event snapshot before the coordinator runs."""
from __future__ import annotations

import asyncio
import os
import subprocess
import time

from app.agent.state import AgentState
from app.core.config import settings
from app.streaming.emitter import StatusEvent, emit
from app.tools.kubectl_tool import (
    _connection_flag_in,
    _extract_namespace,
    _is_all_namespaces,
)
from app.tools.namespace_guard import (
    drop_blocked_table_rows,
    protected_message,
    withheld_sentence,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

_SNAPSHOT_MAX_CHARS = 8_000

# A policy sentence is not cluster data. It shares a line shape with a table row, so every
# scanner in this module has to be able to tell them apart.
_POLICY_PREFIX = "[Protected]"

# Pod phases that count as "healthy" — anything else flips snapshot_has_issues.
# (Note: STATUS column from `kubectl get pods` mixes phases and reasons —
#  e.g. "CrashLoopBackOff", "ImagePullBackOff". We treat any value not in this
#  set as an issue, which is what we want.)
_HEALTHY_POD_STATUSES = frozenset({"Running", "Completed", "Succeeded"})


def _data_row_count(table: str) -> int:
    """Rows of a kubectl table that are actual resources — not the header, not policy text."""
    rows = [
        ln for ln in table.splitlines()
        if ln.strip() and not ln.strip().startswith(_POLICY_PREFIX)
    ]
    return max(len(rows) - 1, 0)  # the first surviving line is the header


def _scan_snapshot(
    pods_out: str,
    events_out: str,
    *,
    pods_ok: bool = True,
    events_ok: bool = True,
) -> tuple[bool, bool, int]:
    """Return (has_issues, has_warnings, pod_count) by scanning kubectl output.

    Cheap line-based parse — no extra subprocess calls. The pod table format is:
        NAMESPACE   NAME   READY   STATUS   RESTARTS   AGE
    We index the STATUS column by header position to be robust to column widths.

    ``pods_ok`` / ``events_ok`` say whether the command that produced each string
    actually succeeded. **They are not optional in spirit.** Until 2026-08-20 this
    function was handed kubectl's *stderr* whenever a read failed — the runner
    returned ``proc.stdout or proc.stderr`` without ever looking at the exit code —
    and parsed it as a pod table. Measured against the real binary, that produced
    two different lies depending on how many lines the error had:

      * ``error: You must be logged in to the server (Unauthorized)`` — one line,
        consumed as the header row ⇒ ``pod_count=0, has_issues=False``: an
        unreachable cluster reported as an empty, healthy one.
      * a real connection failure, which kubectl prints as three lines ⇒ the two
        ``E0820 …`` lines have enough whitespace-separated columns to be counted
        as pods ⇒ ``pod_count=2``, a quantity invented out of an error message.

    A caller that cannot say whether the read worked gets the old behaviour, so the
    defaults are the unsafe ones. Every in-tree caller passes the flags.
    """
    has_issues = False
    pod_count = 0

    if not pods_ok:
        # Nothing about the cluster is known. Callers distinguish this from a real
        # empty cluster via the flag they passed in, not via these values.
        return False, False, 0

    lines = pods_out.splitlines()
    status_idx: int | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(_POLICY_PREFIX):
            # The withheld-rows sentence has enough whitespace-separated columns to be read
            # as a pod. Counting the notice that rows were removed as one of the rows would
            # be its own small lie, and it would flip has_issues on its wording.
            continue
        cols = line.split()
        if status_idx is None:
            # Header row — find STATUS column index. Fallback to 3 (default layout).
            try:
                status_idx = [c.upper() for c in cols].index("STATUS")
            except ValueError:
                status_idx = 3
            continue
        if len(cols) <= status_idx:
            continue
        pod_count += 1
        status = cols[status_idx]
        if status not in _HEALTHY_POD_STATUSES:
            has_issues = True

    # A failed events read is not "no warnings", and its stderr is not a warning list.
    # Neither is a table whose only surviving lines are the header and the policy notice:
    # after the blocked-namespace filter, a cluster whose every warning is in `kube-system`
    # leaves exactly that, and `bool(text.strip())` used to call it a warning.
    has_warnings = (
        events_ok
        and _data_row_count(events_out) > 0
        and "No resources found" not in events_out
    )
    return has_issues, has_warnings, pod_count


def _filter_snapshot_output(args: list[str], output: str) -> str:
    """Remove blocked-namespace rows from a cluster-wide snapshot, and say how many.

    `-n <blocked>` is refused before it runs; `--all-namespaces` names no namespace in
    particular, so it has to be filtered on the way back — exactly as `run_kubectl` filters the
    output of the identical command. The withheld sentence goes in because the alternative is a
    short listing that reads as a complete one: the model is being handed this table as *the*
    state of the cluster, with no request of its own to compare it against.
    """
    if not _is_all_namespaces(args):
        return output
    kept, dropped = drop_blocked_table_rows(output)
    if not dropped:
        return output
    return kept.rstrip("\n") + "\n" + withheld_sentence(dropped, "row") + "\n"


def _snapshot_refusal(args: list[str]) -> str | None:
    """Why this read must not run, or None — the same policy `run_kubectl` applies.

    This module is a **second kubectl executor**. It exists because the snapshot is a fixed,
    internal read that should not pay for tool dispatch — but until 2026-08-20 "internal" was
    taken to mean "trusted", and it ran whatever it was handed. Two of its three callers pass
    an argument list that is not fixed at all:

      * `targeted_investigator` builds `-n <namespace>` out of a namespace the **model wrote**
        in a `TARGETED:` line. Measured: `run_kubectl` refuses
        `kubectl describe pod etcd-control-plane -n kube-system` with `[Protected]`, while the
        same read through this function returned the full description — environment variable
        names, mounted certificate paths — straight into the prompt.
      * `_verify_resolution` passes a namespace derived from the applied manifest.

    So the gate belongs here, at the one place the subprocess is actually launched, and it is
    the blocklist itself rather than a second reading of it. The connection/identity family is
    refused for the same reason it is refused in `run_kubectl`: which cluster this talks to is
    the deployment's decision, and a value the model wrote must not be able to change it.
    """
    conn_flag = _connection_flag_in(args)
    if conn_flag:
        return (
            f"{_POLICY_PREFIX} '{conn_flag}' is not permitted. Which cluster this connects to, "
            "and the identity it uses, are fixed by the deployment."
        )
    namespace = _extract_namespace(args)
    if namespace and namespace.strip().lower() in settings.kubectl_blocked_namespaces:
        return protected_message(namespace)
    return None


def _kubectl_snapshot(args: list[str]) -> tuple[bool, str]:
    """Run a read-only kubectl command. Returns ``(ok, text)``.

    ``ok`` is False when kubectl exited non-zero, could not be started, or timed
    out. The text is still returned — it is the operator-facing explanation — but
    a caller must not treat it as cluster data. See ``_scan_snapshot`` for what
    happened while nothing checked the exit code.
    """
    refusal = _snapshot_refusal(args)
    if refusal:
        logger.warning("context_fetcher: refused snapshot read %r: %s", args, refusal)
        return False, refusal

    kubeconfig = os.path.expanduser(settings.KUBECONFIG_PATH)
    env = {**os.environ, "KUBECONFIG": kubeconfig}
    try:
        proc = subprocess.run(
            ["kubectl"] + args,
            capture_output=True,
            text=True,
            timeout=settings.KUBECTL_TIMEOUT_SECONDS,
            env=env,
            shell=False,
        )
        out = proc.stdout or proc.stderr or ""
        if proc.returncode != 0:
            logger.warning(
                "context_fetcher: kubectl %s exited %d: %s",
                " ".join(args[:2]), proc.returncode,
                (proc.stderr or "").strip().splitlines()[-1:] or ["(no stderr)"],
            )
            return False, out[:_SNAPSHOT_MAX_CHARS]
        return True, _filter_snapshot_output(args, out)[:_SNAPSHOT_MAX_CHARS]
    except Exception as exc:
        logger.warning(f"context_fetcher: kubectl {' '.join(args[:2])} failed: {exc}")
        return False, f"(unavailable: {exc})"


def _run_kubectl_snapshot(args: list[str]) -> str:
    """Text-only wrapper, for callers that render the output for a human."""
    return _kubectl_snapshot(args)[1]


def _unavailable_reason(text: str) -> str:
    """The one line worth showing a reader from a failed kubectl read."""
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1].strip()[:300] if lines else "kubectl reported no reason"


async def context_fetcher(state: AgentState) -> dict:
    """Pre-fetch pod list and warning events in parallel before coordinator runs."""
    session_id = state.get("session_id", "-")
    await emit(session_id, StatusEvent(
        phase="snapshot",
        message="Fetching cluster snapshot…",
        session_id=session_id,
    ))

    (pods_ok, pods_out), (events_ok, events_out) = await asyncio.gather(
        asyncio.to_thread(_kubectl_snapshot, ["get", "pods", "--all-namespaces"]),
        asyncio.to_thread(_kubectl_snapshot, [
            "get", "events", "--all-namespaces",
            "--sort-by=.lastTimestamp",
            "--field-selector=type=Warning",
        ]),
    )
    read_failed = not (pods_ok and events_ok)

    parts = ["## Cluster Snapshot"]
    if pods_ok:
        parts.append(f"### Live Pod State\n```\n{pods_out.strip()}\n```")
    else:
        # Never print kubectl's stderr under a heading that claims it is pod state.
        parts.append(
            "### Live Pod State\n**UNAVAILABLE — the cluster read failed.** "
            f"kubectl said: `{_unavailable_reason(pods_out)}`\n\n"
            "This is not zero pods; it is unknown. Do not answer questions about "
            "what is running from this snapshot."
        )

    if not events_ok:
        parts.append(
            "### Warning Events\n**UNAVAILABLE — the cluster read failed.** "
            f"kubectl said: `{_unavailable_reason(events_out)}`\n\n"
            "This is not an absence of warnings; it is unknown."
        )
    elif not events_out.strip() or "No resources found" in events_out:
        parts.append("### Warning Events\n(none — cluster appears healthy)")
    else:
        parts.append(f"### Warning Events (most recent)\n```\n{events_out.strip()}\n```")

    cluster_snapshot = "\n\n".join(parts)

    # ── Snapshot health scan ──────────────────────────────────────────────────
    has_issues, has_warnings, pod_count = _scan_snapshot(
        pods_out, events_out, pods_ok=pods_ok, events_ok=events_ok)

    # ── Playbook trigger matching ─────────────────────────────────────────────
    matched_playbooks: list[str] = []
    if settings.PLAYBOOKS_ENABLED:
        try:
            from app.agent.playbooks import match_playbooks
            # Matching against stderr fires playbooks on the text of an error.
            matched_playbooks = match_playbooks(
                pods_out if pods_ok else "", events_out if events_ok else "")
        except Exception as exc:
            logger.warning(f"context_fetcher: playbook matching failed: {exc}")

    # Resolve cluster identity (cached for the process lifetime).
    try:
        from app.cluster_id import get_cluster_id
        cluster_id = get_cluster_id()
    except Exception as exc:
        logger.warning(f"context_fetcher: cluster_id resolution failed: {exc}")
        cluster_id = "unknown"

    logger.info(
        "context_fetcher: snapshot_complete",
        extra={
            "session_id": session_id,
            "snapshot_chars": len(cluster_snapshot),
            "snapshot_pod_count": pod_count,
            "snapshot_has_issues": has_issues,
            "snapshot_has_warnings": has_warnings,
            "snapshot_read_failed": read_failed,
            "matched_playbooks": matched_playbooks,
            "cluster_id": cluster_id,
        },
    )

    return {
        "cluster_snapshot": cluster_snapshot,
        "snapshot_has_issues": has_issues,
        "snapshot_has_warnings": has_warnings,
        "snapshot_pod_count": pod_count,
        "snapshot_read_failed": read_failed,
        "snapshot_built_at": time.time(),
        "matched_playbooks": matched_playbooks,
        "cluster_id": cluster_id,
    }
