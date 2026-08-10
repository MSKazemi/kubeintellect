"""
run_kubectl — the single execution surface for all Kubernetes operations.

Safety layers (in order):
  1. Shell injection prevention  — reject dangerous shell metacharacters
  2. YAML pre-validation         — validate stdin YAML before touching cluster
  3. Risk classification         — destructive verbs trigger LangGraph interrupt
  4. subprocess with shell=False — no shell interpolation
  5. Pipe emulation              — | grep handled in Python (no shell needed)
  6. Namespace output filter     — strip blocked namespaces from get-namespaces output
  7. Output cap                  — truncate at 8 000 chars
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from typing import Annotated

import yaml
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool
from langgraph.types import interrupt

from app.core.config import settings
from app.tools import kubectl_errors
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── Risk tables ───────────────────────────────────────────────────────────────

_HIGH_RISK = {"delete", "drain", "replace", "taint"}
_MEDIUM_RISK = {"patch", "apply", "scale", "exec", "cordon", "uncordon", "create", "run", "set"}
DESTRUCTIVE_VERBS = _HIGH_RISK | _MEDIUM_RISK

# (verb, target) pairs whose blast radius is too large to auto-approve.
# These trigger a HITL prompt even when hitl_bypass=True is set on the session.
# Reasoning: cascading deletes (namespace/pv/crd) and live workload mutations
# (set image/resources, drain) can destroy data or take traffic down without
# any rollback path — the user must confirm even on auto-approve sessions.
_ALWAYS_CONFIRM_DELETE_TARGETS = {
    "namespace", "namespaces", "ns",
    "pv", "persistentvolume", "persistentvolumes",
    "crd", "customresourcedefinition", "customresourcedefinitions",
}
_ALWAYS_CONFIRM_SET_SUBCOMMANDS = {"image", "resources"}

# Verbs that have no side effects — allowed on all namespaces including protected ones.
_READ_ONLY_VERBS = {
    "get", "describe", "logs", "top", "diff", "explain",
    "auth", "version", "cluster-info", "api-resources", "api-versions", "rollout",
}

# kubectl edit requires an interactive terminal that is never available in the container.
_REJECTED_VERBS = {"edit"}

# ── Shell injection guard ─────────────────────────────────────────────────────
# Pipe (|) is intentionally excluded — it is handled in Python via _apply_pipes.
# Backslash (\) is intentionally excluded: it appears in valid jsonpath
# separators like {"\n"} and {"\t"}, and is harmless because we run with
# shell=False (backslashes are passed literally to kubectl, not interpreted
# by a shell).
_SHELL_METACHAR = re.compile(r"[;&`$<>]")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _classify_risk(verb: str) -> str:
    if verb in _HIGH_RISK:
        return "high"
    if verb in _MEDIUM_RISK:
        return "medium"
    return "low"


def _requires_always_confirm(verb: str, args: list[str]) -> bool:
    """True for actions whose blast radius is too large to auto-approve.

    Always-confirm actions trigger an HITL interrupt even when
    `hitl_bypass=True` (auto-approve session). The user must explicitly
    confirm; there is no way to silently auto-approve them.
    """
    if verb == "drain":
        return True
    if verb == "set" and len(args) >= 3 and args[2].lower() in _ALWAYS_CONFIRM_SET_SUBCOMMANDS:
        return True
    if verb == "delete" and len(args) >= 3:
        target = args[2].split("/")[0].lower()
        if target in _ALWAYS_CONFIRM_DELETE_TARGETS:
            return True
    return False


def _normalise(command: str) -> str:
    """Strip leading 'kubectl' duplication if the LLM included it twice."""
    cmd = command.strip()
    if not cmd.startswith("kubectl"):
        cmd = f"kubectl {cmd}"
    return cmd


def _extract_verb(tokens: list[str]) -> str:
    """Return the kubectl subcommand verb (second token after 'kubectl')."""
    return tokens[1] if len(tokens) > 1 else ""


def _split_on_pipes(command: str) -> list[str]:
    """Split a command on unquoted '|' characters, respecting single/double quotes."""
    parts: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    for ch in command:
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
        elif ch == "|" and not in_single and not in_double:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _apply_pipes(output: str, pipe_segments: list[str]) -> str:
    """Apply a list of pipe segments (e.g. ['grep foo', 'grep -v bar']) to output.

    Only grep is supported. Any other command raises ValueError so the LLM
    knows to ask differently rather than silently getting wrong results.
    """
    for segment in pipe_segments:
        tokens = shlex.split(segment.strip())
        if not tokens or tokens[0] != "grep":
            raise ValueError(
                f"Pipe segment {segment!r} contains disallowed shell characters or unsupported command. "
                "Only 'grep' is allowed after '|'."
            )
        # Parse simple grep flags: -v (invert), -i (case-insensitive), -E (extended regex)
        invert = False
        ignore_case = False
        pattern_tokens = []
        i = 1
        while i < len(tokens):
            t = tokens[i]
            if t in ("-v", "--invert-match"):
                invert = True
            elif t in ("-i", "--ignore-case"):
                ignore_case = True
            elif t in ("-E", "--extended-regexp"):
                pass  # Python re already uses extended syntax
            elif not t.startswith("-"):
                pattern_tokens.append(t)
            i += 1
        if not pattern_tokens:
            raise ValueError(f"grep in pipe segment {segment!r} has no pattern.")
        pattern = " ".join(pattern_tokens)
        flags = re.IGNORECASE if ignore_case else 0
        compiled = re.compile(pattern, flags)
        lines = output.splitlines(keepends=True)
        matched = [ln for ln in lines if bool(compiled.search(ln)) != invert]
        output = "".join(matched) or "(no matching lines)"
    return output


def _extract_namespace(args: list[str]) -> str | None:
    """Extract the -n / --namespace value from a parsed arg list."""
    for i, arg in enumerate(args):
        if arg in ("-n", "--namespace") and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--namespace="):
            return arg.split("=", 1)[1]
    return None


def _extract_resource_type(verb: str, args: list[str]) -> str | None:
    """
    Extract the Kubernetes resource type from the command args.

    Returns None for verbs where args[2] is not a resource type
    (e.g. 'logs', 'exec', 'rollout') — those are safe to skip.
    """
    _resource_verbs = {
        "get", "describe", "delete", "edit", "patch",
        "apply", "create", "replace", "label", "annotate",
    }
    if verb not in _resource_verbs or len(args) < 3:
        return None
    # Handle "resource/name" shorthand (e.g. "deployment/myapp")
    return args[2].split("/")[0].lower()


def _filter_namespace_output(args: list[str], output: str) -> str:
    """Strip blocked namespaces from kubectl get namespaces output."""
    resource = _extract_resource_type("get", args)
    if resource not in ("namespaces", "namespace", "ns"):
        return output

    blocked = settings.kubectl_blocked_namespaces
    out_format: str | None = None
    for i, a in enumerate(args):
        if a in ("-o", "--output") and i + 1 < len(args):
            out_format = args[i + 1]
            break
        if a.startswith(("-o=", "--output=")):
            out_format = a.split("=", 1)[1]
            break

    lines = output.splitlines(keepends=True)

    if out_format == "name":
        return "".join(l for l in lines if l.strip().split("/")[-1] not in blocked)
    if out_format in ("json", "yaml"):
        return output  # too complex to strip reliably; blocked at execution anyway
    if out_format and "jsonpath" in out_format:
        tokens = output.split()
        return " ".join(t for t in tokens if t not in blocked)
    # default table: keep header + rows whose first column isn't blocked
    result = []
    for line in lines:
        parts = line.split()
        if not parts or parts[0] == "NAME" or parts[0] not in blocked:
            result.append(line)
    return "".join(result)


def _check_protected_access(verb: str, args: list[str]) -> str | None:
    """
    Return an error string if the command targets a protected namespace or
    resource type. Returns None if the command is allowed to proceed.

    All operations on infrastructure namespaces are blocked, including reads.
    Secrets and serviceaccounts are blocked regardless of namespace.
    """
    resource = _extract_resource_type(verb, args)
    ns = _extract_namespace(args)

    # Secrets and serviceaccounts are fully blocked regardless of verb — they
    # would expose credentials and tokens even to read-only viewers.
    if resource and resource in settings.kubectl_blocked_resources:
        return (
            f"[Protected] Access to '{resource}' is not permitted through KubeIntellect. "
            "Kubernetes Secrets and ServiceAccount tokens are shielded from inspection "
            "to protect cluster credentials."
        )

    # All operations on infrastructure namespaces are blocked.
    if ns and ns in settings.kubectl_blocked_namespaces:
        return (
            f"[Protected] Access to namespace '{ns}' is not permitted. "
            "This is an infrastructure namespace."
        )

    return None


def _validate_stdin_yaml(stdin: str) -> None:
    """Raise ValueError if stdin is not valid YAML."""
    try:
        docs = list(yaml.safe_load_all(stdin))
        if not docs or docs == [None]:
            raise ValueError("stdin YAML is empty or null")
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in stdin: {exc}") from exc


# ── Main tool ─────────────────────────────────────────────────────────────────


def _capture_rollback_point(verb: str, args: list, stdin: str | None, config, env: dict) -> None:
    """Best-effort pre-state capture for the Safety Sandwich (never raises).

    Records a `rollback_point` into the flight recorder: the rollback_id,
    the mutating command, and the current YAML of the targeted objects
    (redacted + capped). Recovery = `kubectl apply -f` the captured state
    (or recreate, for deletes).
    """
    import uuid as _uuid

    try:
        from app.db import flight_recorder
        from app.utils.redact import redact_secrets

        targets: list[list[str]] = []
        if stdin:
            try:
                for doc in yaml.safe_load_all(stdin):
                    if not isinstance(doc, dict):
                        continue
                    kind = str(doc.get("kind", "")).lower()
                    name = str(doc.get("metadata", {}).get("name", ""))
                    ns = str(doc.get("metadata", {}).get("namespace", ""))
                    if kind and name:
                        target = ["kubectl", "get", kind, name, "-o", "yaml"]
                        if ns:
                            target += ["-n", ns]
                        targets.append(target)
            except Exception:
                pass
        else:
            # delete/patch/scale/label with explicit resource args:
            # swap the verb for `get ... -o yaml`, drop value-bearing flags.
            tail = [a for a in args[1:] if a != verb]
            keep: list[str] = []
            skip_next = False
            for token in tail:
                if skip_next:
                    skip_next = False
                    continue
                if token in ("-n", "--namespace"):
                    keep.append(token)
                    continue
                if token.startswith("-") and token not in ("-n", "--namespace"):
                    if "=" not in token and token not in ("--all",):
                        skip_next = True
                    continue
                keep.append(token)
            if keep:
                targets.append(["kubectl", "get", *keep, "-o", "yaml"])

        if not targets:
            return
        states = []
        for target in targets[:5]:
            try:
                pre = subprocess.run(
                    target, capture_output=True, text=True, timeout=5, env=env, shell=False
                )
                if pre.returncode == 0 and pre.stdout:
                    states.append(redact_secrets(pre.stdout, max_chars=4000))
            except Exception:
                continue
        if not states:
            return
        session_id = (config.get("configurable") or {}).get("thread_id", "-") if config else "-"
        rollback_id = f"rb-{_uuid.uuid4().hex[:12]}"
        flight_recorder.record(session_id, "rollback_point", {
            "type": "rollback_point",
            "rollback_id": rollback_id,
            "command": " ".join(str(a) for a in args)[:300],
            "pre_state": states,
            "session_id": session_id,
        })
        logger.info(f"rollback_point_armed id={rollback_id} targets={len(states)}")
    except Exception as exc:
        logger.warning(f"rollback capture failed (non-fatal): {exc}")



@tool
def run_kubectl(
    command: str,
    stdin: str | None = None,
    # The annotation MUST stay exactly `RunnableConfig` — langchain_core's
    # _get_runnable_config_param matches with `type_ is RunnableConfig`, so widening it
    # to `RunnableConfig | None` stops the run config being injected at all. The tool
    # then sees config=None and silently loses user_role (RBAC) and hitl_bypass, which
    # turns the HITL gate into a no-op. Covered by tests/test_kubectl_tool.py
    # ::TestAlwaysConfirm. The body already treats config as optional.
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # type: ignore[assignment]
) -> str:
    """Run any kubectl command against the configured cluster.

    Args:
        command: A kubectl command string. Examples:
            kubectl get pods -n production
            kubectl describe deployment my-app -n staging
            kubectl logs my-pod --tail=100 --since=5m
            kubectl apply -f -          (pass YAML via stdin)
            kubectl rollout status deployment/my-app -n production
        stdin: Optional YAML content piped to stdin (for `kubectl apply -f -`).

    Returns:
        The combined stdout + stderr output, capped at 8 000 characters.

    Raises:
        ValueError: If shell injection is detected or stdin YAML is invalid.
    """
    # ── 0. Split on pipes before any further processing ──────────────────────
    raw_parts = _split_on_pipes(command)
    cmd = _normalise(raw_parts[0].strip())
    pipe_segments = [p.strip() for p in raw_parts[1:]]

    # Validate pipe segments early so non-grep pipes fail before subprocess runs.
    for seg in pipe_segments:
        seg_tokens = shlex.split(seg) if seg else []
        if seg_tokens and seg_tokens[0] != "grep":
            raise ValueError(
                f"Pipe segment {seg!r} contains disallowed shell characters or unsupported command. "
                "Only 'grep' is allowed after '|'."
            )

    # ── 1. Shell injection prevention ────────────────────────────────────────
    if _SHELL_METACHAR.search(cmd):
        raise ValueError(
            f"Command contains disallowed shell characters: {cmd!r}. "
            "Use plain kubectl syntax only. "
            "Tip: -o jsonpath='...' with {\"\\n\"} separators is supported; "
            "use -o json for complex extraction."
        )
    for seg in pipe_segments:
        if _SHELL_METACHAR.search(seg):
            raise ValueError(
                f"Pipe segment contains disallowed shell characters: {seg!r}."
            )
    # stdin is passed directly to the subprocess (shell=False), so shell
    # metacharacters in YAML/HTML content are harmless — no injection risk.

    # ── 2. YAML pre-validation ───────────────────────────────────────────────
    if stdin:
        _validate_stdin_yaml(stdin)

    # ── 3. Parse into arg list (shell=False) ─────────────────────────────────
    try:
        args = shlex.split(cmd)
    except ValueError as exc:
        # "No closing quotation" usually means a jsonpath expression was
        # truncated mid-generation. Provide an actionable error message
        # rather than a raw shlex exception.
        if "closing quotation" in str(exc).lower():
            raise ValueError(
                f"Could not parse command (unclosed quote): {cmd!r}. "
                "The jsonpath expression appears to be truncated — use "
                "-o custom-columns=COL:.path,COL2:.path2 or -o json instead."
            ) from exc
        raise ValueError(f"Could not parse command: {exc}") from exc

    verb = _extract_verb(args)

    # ── 4. Rejected verbs (non-interactive, always fail in container) ─────────
    if verb in _REJECTED_VERBS:
        return (
            f"[Unsupported] 'kubectl {verb}' requires an interactive terminal which is "
            "not available. Use 'kubectl patch' or 'kubectl apply -f -' with stdin instead."
        )

    # ── 4a. Role check ────────────────────────────────────────────────────────
    # readonly   : all writes blocked
    # operator   : medium-risk allowed (HITL-gated); high-risk blocked
    # admin      : everything allowed (HITL-gated); infra namespace writes blocked
    # superadmin : everything allowed (HITL-gated); no namespace restrictions at all
    #              (reads of infra namespaces included) - but never the resource block
    user_role = "admin"
    if config:
        user_role = (config.get("configurable") or {}).get("user_role", "admin")

    if user_role == "readonly" and verb in DESTRUCTIVE_VERBS:
        return (
            f"[Permission Denied] Your API key has read-only access. "
            f"The '{verb}' operation requires an operator or admin API key."
        )
    if user_role == "operator" and verb in _HIGH_RISK:
        return (
            f"[Permission Denied] Your API key has operator access. "
            f"The '{verb}' operation requires an admin API key."
        )

    # ── 4b. Protected namespace / resource check ──────────────────────────────
    # Runs before HITL so users never even get an approval prompt for
    # commands that would expose internal credentials.
    # superadmin bypasses the namespace block ENTIRELY - for any verb, so reads of
    # infrastructure namespaces too, not only writes - but never the resource block
    # (secrets/serviceaccounts remain shielded for all roles).
    protected_err = _check_protected_access(verb, args)
    if protected_err and user_role == "superadmin":
        # Re-run the check considering only the resource block (not ns block)
        resource = _extract_resource_type(verb, args)
        if not (resource and resource in settings.kubectl_blocked_resources):
            protected_err = None
    if protected_err:
        logger.warning(f"run_kubectl: blocked protected access: {cmd!r}")
        return protected_err

    # ── 4c. Risk classification → HITL interrupt ─────────────────────────────
    if verb in DESTRUCTIVE_VERBS:
        has_dry_run = any(
            flag in args for flag in ("--dry-run=client", "--dry-run=server", "--dry-run")
        )
        hitl_bypass = bool((config.get("configurable") or {}).get("hitl_bypass", False)) if config else False
        always_confirm = _requires_always_confirm(verb, args)
        if not has_dry_run and (not hitl_bypass or always_confirm):
            risk = "high" if always_confirm else _classify_risk(verb)
            if always_confirm and hitl_bypass:
                logger.warning(
                    f"run_kubectl: always-confirm override of auto-approve for {cmd!r}"
                )
            session_id = (config.get("configurable") or {}).get("thread_id", "-") if config else "-"
            logger.info(
                f"run_kubectl: hitl_classification verb={verb} risk={risk}",
                extra={"session_id": session_id, "hitl_verb": verb, "hitl_risk_level": risk, "hitl_cmd": cmd[:200]},
            )
            approved = interrupt({
                "type": "hitl",
                "command": cmd,
                "stdin": stdin,          # include YAML so the user sees what will be applied
                "risk_level": risk,
                "always_confirm": always_confirm,
                "human_summary": f"About to run: `{cmd}`",
            })
            if not approved:
                return "Action cancelled by user."
        elif not has_dry_run and hitl_bypass:
            logger.info(f"run_kubectl: HITL bypassed (auto-approve) for: {cmd!r}")

    # ── 5. Execute ───────────────────────────────────────────────────────────
    kubeconfig = os.path.expanduser(settings.KUBECONFIG_PATH)
    env = {**os.environ, "KUBECONFIG": kubeconfig}

    # Safety Sandwich (V4 ADR-003): arm a rollback point before any mutation —
    # the pre-state YAML of the targets lands in the flight recorder, so every
    # destructive action is undoable from the audit trail.
    if verb in _HIGH_RISK or verb in _MEDIUM_RISK:
        if not has_dry_run:
            _capture_rollback_point(verb, args, stdin, config, env)

    logger.debug(f"run_kubectl: {cmd}")

    timeout = (
        settings.KUBECTL_DESTRUCTIVE_TIMEOUT_SECONDS
        if verb in _HIGH_RISK
        else settings.KUBECTL_TIMEOUT_SECONDS
    )
    try:
        proc = subprocess.run(
            args,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            shell=False,
        )
    except FileNotFoundError:
        return (
            "[Error] kubectl is not installed or not found in PATH. "
            "Install it from https://kubernetes.io/docs/tasks/tools/ "
            "or run 'kubeintellect kind-setup' to provision a local cluster."
        )

    output = proc.stdout or proc.stderr or "(no output)"
    logger.debug(f"run_kubectl: exit={proc.returncode} output_len={len(output)} cmd={cmd}")

    # ── 5b. Error interpretation ──────────────────────────────────────────────
    # Append a single-line hint when kubectl exits non-zero and the stderr
    # matches a known pattern. Original error is preserved verbatim.
    if proc.returncode != 0 and settings.KUBECTL_ERROR_HINTS_ENABLED:
        output, pattern_name = kubectl_errors.annotate(output)
        if pattern_name:
            logger.info(
                "kubectl_error_interpreted "
                f"pattern={pattern_name} exit_code={proc.returncode} cmd={cmd!r}",
                extra={"pattern": pattern_name, "exit_code": proc.returncode},
            )

    # ── 6. Pipe emulation (grep) ─────────────────────────────────────────────
    if pipe_segments:
        output = _apply_pipes(output, pipe_segments)

    # ── 6b. Strip blocked namespaces from namespace listings ─────────────────
    output = _filter_namespace_output(args, output)

    # ── 7. Output cap ────────────────────────────────────────────────────────
    limit = 8_000
    if len(output) > limit:
        omitted = len(output) - limit
        output = (
            output[:limit]
            + f"\n\n[truncated: {omitted} chars omitted — output was cut short. "
            "Inform the user that the list is incomplete and suggest narrowing with "
            "--tail, -n <namespace>, or -l <label> flags.]"
        )
        logger.debug(f"run_kubectl: output truncated ({omitted} chars omitted)")

    return output
