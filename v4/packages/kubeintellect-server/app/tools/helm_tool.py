"""
run_helm — read-only Helm inspection tool.

Allows the agent to query Helm release state, values, manifests, and history
without any risk of mutating the cluster.

Safety layers:
  1. Shell injection prevention  — reject dangerous shell metacharacters
  2. Write-verb block            — install/upgrade/rollback/uninstall are refused
  3. subprocess with shell=False — no shell interpolation
  4. Output cap                  — truncate at 6 000 chars
"""
from __future__ import annotations

import re
import shlex
import subprocess

from langchain_core.tools import tool

from app.utils.logger import get_logger

logger = get_logger(__name__)

_OUTPUT_CAP = 6_000

# ── Allowed read-only verbs ───────────────────────────────────────────────────
_READ_ONLY_VERBS = {
    "list", "get", "status", "history", "env", "version", "show", "search",
}

# ── Blocked write verbs ───────────────────────────────────────────────────────
_WRITE_VERBS = {
    "install", "upgrade", "rollback", "uninstall", "delete", "repo",
    "plugin", "dependency", "package", "push", "pull", "create", "lint",
    "template",  # allowed conceptually but excluded to keep scope narrow
}

# Shell metacharacters that are never needed in a Helm inspection command.
_SHELL_METACHAR = re.compile(r"[;&`$\\|<>]")


def _normalise(command: str) -> str:
    """Strip leading 'helm' if the LLM doubled it, and lowercase the verb."""
    cmd = command.strip()
    if not cmd.startswith("helm"):
        cmd = f"helm {cmd}"
    return cmd


def _extract_verb(tokens: list[str]) -> str:
    return tokens[1].lower() if len(tokens) > 1 else ""


@tool
def run_helm(command: str) -> str:
    """Run a read-only Helm command to inspect release state.

    Use this whenever you need to understand the current Helm release
    configuration, history, or values — especially when diagnosing workloads
    that were deployed via Helm.

    Supported subcommands:
      helm list [-n <ns>] [-A]                — list releases
      helm status <release> [-n <ns>]         — show release status and notes
      helm get values <release> [-n <ns>]     — show user-supplied values
      helm get manifest <release> [-n <ns>]   — show rendered manifests
      helm get notes <release> [-n <ns>]      — show chart notes
      helm get all <release> [-n <ns>]        — all of the above
      helm history <release> [-n <ns>]        — show revision history
      helm show chart <chart>                 — inspect chart metadata
      helm show values <chart>                — inspect chart default values
      helm env                                — show Helm environment
      helm version                            — show Helm client version

    NOT supported (write operations):
      install, upgrade, rollback, uninstall, repo add/update, plugin install

    Examples:
      helm list -A
      helm status my-release -n production
      helm get values my-release -n staging
      helm history my-release -n default
    """
    cmd = _normalise(command)
    logger.debug(f"run_helm: {cmd}")

    # ── 1. Shell injection guard ──────────────────────────────────────────────
    if _SHELL_METACHAR.search(cmd):
        return (
            "[Error] Command contains disallowed shell characters. "
            "Use plain helm subcommands without shell operators."
        )

    # ── 2. Parse tokens ───────────────────────────────────────────────────────
    try:
        tokens = shlex.split(cmd)
    except ValueError as exc:
        return f"[Error] Could not parse command: {exc}"

    verb = _extract_verb(tokens)

    # ── 3. Write-verb block ───────────────────────────────────────────────────
    if verb in _WRITE_VERBS:
        return (
            f"[Not Allowed] 'helm {verb}' is a write operation. "
            "run_helm only supports read-only inspection commands "
            "(list, get, status, history, env, version, show, search). "
            "To apply Helm changes, ask the user to run the command manually."
        )

    if verb not in _READ_ONLY_VERBS:
        return (
            f"[Not Allowed] 'helm {verb}' is not a supported subcommand. "
            f"Supported: {', '.join(sorted(_READ_ONLY_VERBS))}."
        )

    # ── 4. Run with shell=False ───────────────────────────────────────────────
    try:
        proc = subprocess.run(
            tokens,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return (
            "[Error] 'helm' binary not found on PATH. "
            "Helm may not be installed in this environment."
        )
    except subprocess.TimeoutExpired:
        return "[Error] helm command timed out after 30 seconds."
    except Exception as exc:
        return f"[Error] Failed to run helm: {exc}"

    output = (proc.stdout or "") + (proc.stderr or "")
    output = output.strip()

    if proc.returncode != 0 and not output:
        output = f"helm exited with code {proc.returncode} (no output)"

    # ── 5. Output cap ─────────────────────────────────────────────────────────
    if len(output) > _OUTPUT_CAP:
        omitted = len(output) - _OUTPUT_CAP
        output = output[:_OUTPUT_CAP] + f"\n[... {omitted} chars truncated]"

    logger.debug(f"run_helm: exit={proc.returncode} output_len={len(output)}")
    return output or "(no output)"
