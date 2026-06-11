"""refresh_snapshot — write a fresh /snapshot.md to the agent's virtual filesystem.

Two entry points share `_collect_snapshot()`:

- `refresh_snapshot` (LangGraph tool): the agent calls this when /snapshot.md
  is missing or its `built_at` is more than 30 s old. Returns a Command that
  merges the new file into agent state.
- `seed_snapshot` (helper): runner.run_session calls this once at session
  start to pre-seed /snapshot.md via `graph.aupdate_state`, so the agent
  starts informed and avoids a round-trip on simple questions.

Both routes use the same markdown shape (front-matter + pod table + warning
events block) and the same FileData wrapper as the framework's write_file.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time

from deepagents.backends.utils import create_file_data
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command
from typing import Annotated

from app.core.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

SNAPSHOT_PATH = "/snapshot.md"
_SNAPSHOT_MAX_CHARS = 8_000

# Pod statuses we treat as healthy. Anything else (Pending, CrashLoopBackOff,
# ImagePullBackOff, OOMKilled, ...) flips has_issues.
_HEALTHY_POD_STATUSES = frozenset({"Running", "Completed", "Succeeded"})


def _run_kubectl_snapshot(args: list[str]) -> str:
    """Direct subprocess kubectl call — bypasses run_kubectl's HITL/RBAC for
    the trusted snapshot path. Read-only `get` only; never user input."""
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
        return (proc.stdout or proc.stderr or "")[:_SNAPSHOT_MAX_CHARS]
    except Exception as exc:
        logger.warning(f"snapshot: kubectl {' '.join(args[:2])} failed: {exc}")
        return f"(unavailable: {exc})"


def _scan_snapshot(pods_out: str, events_out: str) -> tuple[bool, bool, int]:
    """Cheap line-based parse of `kubectl get pods` to compute health flags.

    Indexes the STATUS column from the header so it's robust to width changes.
    """
    has_issues = False
    pod_count = 0
    status_idx: int | None = None
    for line in pods_out.splitlines():
        cols = line.split()
        if not cols:
            continue
        if status_idx is None:
            try:
                status_idx = [c.upper() for c in cols].index("STATUS")
            except ValueError:
                status_idx = 3
            continue
        if len(cols) <= status_idx:
            continue
        pod_count += 1
        if cols[status_idx] not in _HEALTHY_POD_STATUSES:
            has_issues = True
    has_warnings = bool(events_out.strip()) and "No resources found" not in events_out
    return has_issues, has_warnings, pod_count


def _build_snapshot_markdown(pods_out: str, events_out: str) -> tuple[str, dict]:
    """Compose /snapshot.md content + the front-matter dict for logging."""
    has_issues, has_warnings, pod_count = _scan_snapshot(pods_out, events_out)
    built_at = time.time()

    front_matter = {
        "built_at": built_at,
        "pod_count": pod_count,
        "has_issues": has_issues,
        "has_warnings": has_warnings,
    }

    lines = [
        "---",
        f"built_at: {built_at:.0f}",
        f"pod_count: {pod_count}",
        f"has_issues: {str(has_issues).lower()}",
        f"has_warnings: {str(has_warnings).lower()}",
        "---",
        "",
        "# Cluster Snapshot",
        "",
        "## Live Pod State",
        "```",
        pods_out.strip() or "(no pods)",
        "```",
        "",
        "## Warning Events (most recent)",
    ]
    no_events = not events_out.strip() or "No resources found" in events_out
    if no_events:
        lines.append("(none — cluster appears healthy)")
    else:
        lines += ["```", events_out.strip(), "```"]

    return "\n".join(lines), front_matter


async def _collect_snapshot() -> tuple[str, dict]:
    """Run both kubectl probes in parallel and return (markdown, front_matter)."""
    pods_out, events_out = await asyncio.gather(
        asyncio.to_thread(_run_kubectl_snapshot, ["get", "pods", "--all-namespaces"]),
        asyncio.to_thread(
            _run_kubectl_snapshot,
            [
                "get",
                "events",
                "--all-namespaces",
                "--sort-by=.lastTimestamp",
                "--field-selector=type=Warning",
            ],
        ),
    )
    return _build_snapshot_markdown(pods_out, events_out)


@tool
async def refresh_snapshot(tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Refresh the cluster snapshot at /snapshot.md.

    Runs `kubectl get pods --all-namespaces` and `kubectl get events
    --field-selector=type=Warning` in parallel, writes the result to
    /snapshot.md (front-matter: built_at, pod_count, has_issues,
    has_warnings) in the virtual filesystem.

    Call this when /snapshot.md is missing, when its `built_at` is more
    than 30 seconds old, or after any action that materially changes
    cluster state (delete/scale/apply/restart).
    """
    content, fm = await _collect_snapshot()
    summary = (
        f"snapshot refreshed: {fm['pod_count']} pods, "
        f"has_issues={fm['has_issues']}, has_warnings={fm['has_warnings']}"
    )
    logger.debug(f"refresh_snapshot: {summary}")
    return Command(
        update={
            "files": {SNAPSHOT_PATH: create_file_data(content)},
            "messages": [
                {"role": "tool", "content": summary, "tool_call_id": tool_call_id}
            ],
        }
    )


async def seed_snapshot_state(graph, config: dict) -> dict:
    """Pre-seed /snapshot.md via graph.aupdate_state — runner-side only.

    Returns the front_matter dict for telemetry. Best-effort: any failure
    is logged and swallowed so a flaky kubectl never blocks a session.
    """
    try:
        content, fm = await _collect_snapshot()
        await graph.aupdate_state(
            config,
            {"files": {SNAPSHOT_PATH: create_file_data(content)}},
        )
        return fm
    except Exception as exc:
        logger.warning(f"seed_snapshot_state: failed — {exc}")
        return {}
