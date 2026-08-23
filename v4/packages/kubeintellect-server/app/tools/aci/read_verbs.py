"""K8s-ACI v0 read-only verbs: inspect / search / logs / diff_change (v5 specs/01).

Each verb builds a kubectl command, hard-asserts it is read-only, executes it through
the single ``run_kubectl`` seam (inheriting V4's injection guard, protected-namespace
block, and secret redaction), then normalizes + bounds the output into an ``AciResult``
whose ``render()`` is the never-silent string the model sees.
"""

from __future__ import annotations

from typing import Annotated

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
from app.tools.aci import kubectl_output as _out
from app.tools.kubectl_tool import run_kubectl


def _exec(command: str) -> str:
    """Single execution seam — the only place a verb touches the cluster.

    Isolated so unit tests can patch it without a live cluster.
    """
    return run_kubectl.invoke({"command": command})


def _ns_flag(namespace: str | None, all_namespaces: bool = False) -> str:
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

    # run_kubectl surfaces refusals and errors as TEXT, so the only thing separating "here is the
    # cluster" from "here is why I could not look" is how this string is read. It used to be read
    # with a hand-kept marker list plus `lowered.startswith("error")` — and measured 2026-08-20,
    # against the real run_kubectl, two of its own refusals sailed through as **content**:
    #   `[Error] kubectl is not installed or not found in PATH…`  → ok=True, and _health_from read
    #       "error" out of it and reported the target as FAILED — a cluster verdict from a tooling
    #       problem. (`startswith("error")` is False: the string starts with `[`.)
    #   `[Unsupported] 'kubectl edit' requires an interactive terminal which is not available…`
    #       → ok=True, and "available" made it read as CURRENT — a refusal reported as healthy.
    # The three `[Protected]` refusals were caught only because their wording happens to contain
    # "not permitted". Classifying the string once, in the shared reader, is what keeps this from
    # depending on the wording of a message someone may reasonably reword.
    if _out.classify_output(raw) != _out.OK:
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


# Health words are matched as whole whitespace-separated fields, not as substrings anywhere in the
# body. A Deployment called `error-budget-exporter` is not an error, a `crashloop-detector` is not
# crashlooping, and `availability-probe` is not a readiness verdict — a status word only means
# something when it stands alone in a STATUS/phase position.
_FAILED_WORDS = frozenset({"crashloopbackoff", "error", "failed", "imagepullbackoff"})
_IN_PROGRESS_WORDS = frozenset({"pending", "containercreating", "progressing"})
_CURRENT_WORDS = frozenset({"running", "ready", "active", "bound", "available"})
_TERMINATING = "terminating"


def _health_fields(body: str) -> set[str]:
    """Whole fields of ``body``, lowercased, with surrounding punctuation stripped.

    Split on whitespace only — a hyphen is part of a Kubernetes name, so splitting on it is what
    turned `error-budget-exporter` into the word "error".
    """
    return {f.strip(":,.'\"()[]{}").lower() for f in body.split()}


def _health_from(body: str) -> Health:
    fields = _health_fields(body)
    if _TERMINATING in fields:
        return Health.TERMINATING
    if fields & _FAILED_WORDS:
        return Health.FAILED
    if fields & _IN_PROGRESS_WORDS:
        return Health.IN_PROGRESS
    if fields & _CURRENT_WORDS:
        return Health.CURRENT
    return Health.UNKNOWN


# ── The four verbs ─────────────────────────────────────────────────────────────
@tool(args_schema=InspectInput)
async def inspect(
    kind: str,
    name: str,
    namespace: str | None = None,
    view: str = "summary",
    # INVARIANT (AGENTS.md #6): this annotation must stay bare `RunnableConfig`.
    # langchain_core matches the injected run config by identity (`type_ is
    # RunnableConfig`), so `Optional[RunnableConfig]` is NOT matched and the tool
    # silently receives config=None — losing `user_role` (RBAC) and `hitl_bypass`.
    # Guarded by tests/test_injected_config_invariant.py.
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # type: ignore[assignment]
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
    namespace: str | None = None,
    all_namespaces: bool = False,
    selector: str | None = None,
    limit: int = 100,
    # INVARIANT (AGENTS.md #6): this annotation must stay bare `RunnableConfig`.
    # langchain_core matches the injected run config by identity (`type_ is
    # RunnableConfig`), so `Optional[RunnableConfig]` is NOT matched and the tool
    # silently receives config=None — losing `user_role` (RBAC) and `hitl_bypass`.
    # Guarded by tests/test_injected_config_invariant.py.
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # type: ignore[assignment]
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
    pod: str | None = None,
    selector: str | None = None,
    container: str | None = None,
    lines: int = 100,
    since: str | None = None,
    previous: bool = False,
    # INVARIANT (AGENTS.md #6): this annotation must stay bare `RunnableConfig`.
    # langchain_core matches the injected run config by identity (`type_ is
    # RunnableConfig`), so `Optional[RunnableConfig]` is NOT matched and the tool
    # silently receives config=None — losing `user_role` (RBAC) and `hitl_bypass`.
    # Guarded by tests/test_injected_config_invariant.py.
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # type: ignore[assignment]
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
    namespace: str | None = None,
    manifest: str | None = None,
    revision: int | None = None,
    # INVARIANT (AGENTS.md #6): this annotation must stay bare `RunnableConfig`.
    # langchain_core matches the injected run config by identity (`type_ is
    # RunnableConfig`), so `Optional[RunnableConfig]` is NOT matched and the tool
    # silently receives config=None — losing `user_role` (RBAC) and `hitl_bypass`.
    # Guarded by tests/test_injected_config_invariant.py.
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # type: ignore[assignment]
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
