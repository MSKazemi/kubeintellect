"""K8s-ACI v0 read-only verbs: inspect / search / logs / diff_change (v5 specs/01).

Each verb builds a kubectl command, hard-asserts it is read-only, executes it through
the single ``run_kubectl`` seam (inheriting V4's injection guard, protected-namespace
block, and secret redaction), then normalizes + bounds the output into an ``AciResult``
whose ``render()`` is the never-silent string the model sees.
"""

from __future__ import annotations

from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from app.core.config import settings
from app.tools.aci.bounds import empty_message, is_read_only, normalize_krm, window
from app.tools.aci.models import (
    AciContractError,
    AciResult,
    DiffChangeInput,
    Health,
    InspectInput,
    LogsInput,
    SearchInput,
)
from app.tools.kubectl_tool import run_kubectl

# Guard strings run_kubectl returns instead of raising (HITL / protected access).
_GUARD_MARKERS = ("blocked protected", "requires confirmation", "HITL", "not permitted")


def _exec(command: str) -> str:
    """Single execution seam — the only place a verb touches the cluster.

    Isolated so unit tests can patch it without a live cluster.
    """
    return run_kubectl.invoke({"command": command})


def _ns_flag(namespace: Optional[str], all_namespaces: bool = False) -> str:
    if all_namespaces:
        return " --all-namespaces"
    return f" -n {namespace}" if namespace else ""


def _looks_empty(out: str) -> bool:
    s = out.strip()
    return s == "" or s.lower().startswith(("no resources found", "no resources", "(no output)"))


def _run(verb: str, command: str, target: str, view: str = "summary") -> AciResult:
    """Assert read-only, execute, normalize, bound — the shared verb tail."""
    if not is_read_only(command):
        # A programming error: a read verb built a mutating command. Never reaches
        # the cluster (R-aci-wrap-02 / "subagents isolate reads" is structural).
        raise AciContractError(f"{verb} produced a non-read-only command: {command!r}")

    raw = _exec(command)

    # run_kubectl surfaces errors as text (may include a guard string).
    lowered = raw.lower()
    if any(m.lower() in lowered for m in _GUARD_MARKERS):
        return AciResult(verb=verb, ok=False, target=target, kubectl_command=command, error=raw.strip())
    if lowered.startswith("error") or "exit=1" in lowered and _looks_empty(raw) is False and "\n" not in raw:
        return AciResult(verb=verb, ok=False, target=target, kubectl_command=command, error=raw.strip())

    if _looks_empty(raw):
        return AciResult(
            verb=verb, ok=True, target=target, kubectl_command=command, empty=True,
            body=empty_message(verb, target),
        )

    normalized = normalize_krm(raw, view)
    body, total, shown, cursor = window(
        normalized, settings.KI_V5_ACI_MAX_LINES, settings.KI_V5_ACI_MAX_CHARS,
    )
    return AciResult(
        verb=verb, ok=True, target=target, kubectl_command=command,
        body=body, total_lines=total, shown_lines=shown, cursor=cursor,
    )


def _health_from(body: str) -> Health:
    b = body.lower()
    if "terminating" in b:
        return Health.TERMINATING
    if any(k in b for k in ("crashloopbackoff", "error", "failed", "imagepullbackoff")):
        return Health.FAILED
    if any(k in b for k in ("pending", "containercreating", "progressing")):
        return Health.IN_PROGRESS
    if any(k in b for k in ("running", "ready", "active", "bound", "available")):
        return Health.CURRENT
    return Health.UNKNOWN


# ── The four verbs ─────────────────────────────────────────────────────────────
@tool(args_schema=InspectInput)
async def inspect(
    kind: str,
    name: str,
    namespace: Optional[str] = None,
    view: str = "summary",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Inspect one object (normalized, bounded). Read-only."""
    ns = _ns_flag(namespace)
    if view == "full":
        cmd = f"kubectl get {kind} {name}{ns} -o yaml"
    else:
        cmd = f"kubectl describe {kind} {name}{ns}"
    target = f"{kind}/{name}" + (f" in {namespace}" if namespace else "")
    result = _run("inspect", cmd, target, view=view)
    if result.ok and not result.empty:
        result.health = _health_from(result.body)
    return result.render()


@tool(args_schema=SearchInput)
async def search(
    kinds: list[str],
    namespace: Optional[str] = None,
    all_namespaces: bool = False,
    selector: Optional[str] = None,
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List objects by kind/namespace/selector, names-first. Read-only."""
    kinds_arg = ",".join(kinds)
    ns = _ns_flag(namespace, all_namespaces)
    sel = f" -l {selector}" if selector else ""
    cmd = f"kubectl get {kinds_arg}{ns}{sel} -o wide"
    target = f"{kinds_arg}" + (" (all namespaces)" if all_namespaces else (f" in {namespace}" if namespace else ""))
    return _run("search", cmd, target).render()


@tool(args_schema=LogsInput)
async def logs(
    namespace: str,
    pod: Optional[str] = None,
    selector: Optional[str] = None,
    container: Optional[str] = None,
    lines: int = 100,
    since: Optional[str] = None,
    previous: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Read pod logs (bounded, dead-pod evidence via previous=True). Read-only."""
    if pod is None and selector is None:
        return AciResult(
            verb="logs", ok=False, target=f"logs in {namespace}", kubectl_command="",
            error="logs requires either 'pod' or 'selector'.",
        ).render()
    tail = min(lines, settings.KI_V5_ACI_MAX_LINES)
    target_ref = pod if pod else f"-l {selector}"
    parts = [f"kubectl logs {target_ref} -n {namespace} --tail={tail}"]
    if container:
        parts.append(f"-c {container}")
    if since:
        parts.append(f"--since={since}")
    if previous:
        parts.append("--previous")
    cmd = " ".join(parts)
    target = f"logs {target_ref} in {namespace}" + (" (previous)" if previous else "")
    return _run("logs", cmd, target).render()


@tool(args_schema=DiffChangeInput)
async def diff_change(
    against: str,
    kind: str,
    name: str,
    namespace: Optional[str] = None,
    manifest: Optional[str] = None,
    revision: Optional[int] = None,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Diff an object against live state or a prior revision. Read-only.

    ``against=git`` is an explicit v0 non-goal — declined, never a silent no-op.
    """
    ns = _ns_flag(namespace)
    target = f"{kind}/{name}" + (f" in {namespace}" if namespace else "")
    if against == "git":
        return AciResult(
            verb="diff_change", ok=False, target=target, kubectl_command="",
            error="against=git is unsupported in K8s-ACI v0 (GitOps diff deferred).",
        ).render()
    if against == "previous":
        rev = f" --revision={revision}" if revision else ""
        cmd = f"kubectl rollout history {kind}/{name}{ns}{rev}"
        return _run("diff_change", cmd, f"{target} (rollout history)").render()
    # against == "live": needs a proposed manifest to diff.
    if not manifest:
        return AciResult(
            verb="diff_change", ok=False, target=target, kubectl_command="",
            error="against=live requires a 'manifest' to diff against the cluster.",
        ).render()
    cmd = "kubectl diff -f -"
    if not is_read_only(cmd):  # defensive; 'diff' is in the read-only set
        raise AciContractError("diff_change built a non-read-only command")
    raw = run_kubectl.invoke({"command": cmd, "stdin": manifest})
    if _looks_empty(raw):
        return AciResult(
            verb="diff_change", ok=True, target=f"{target} (live diff)", kubectl_command=cmd,
            empty=True, body=empty_message("diff_change", f"{target} (live diff)"),
        ).render()
    body, total, shown, cursor = window(raw, settings.KI_V5_ACI_MAX_LINES, settings.KI_V5_ACI_MAX_CHARS)
    return AciResult(
        verb="diff_change", ok=True, target=f"{target} (live diff)", kubectl_command=cmd,
        body=body, total_lines=total, shown_lines=shown, cursor=cursor,
    ).render()
