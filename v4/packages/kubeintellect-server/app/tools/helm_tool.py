"""
run_helm — read-only Helm inspection tool.

Allows the agent to query Helm release state, values, manifests, and history
without any risk of mutating the cluster.

Safety layers:
  1. Shell injection prevention  — reject dangerous shell metacharacters
  2. Write-verb block            — install/upgrade/rollback/uninstall are refused
  3. Protected-namespace block   — the same blocklist `run_kubectl` enforces
  4. Secret stripping            — `helm get manifest|all` renders the chart's own Secret
                                   objects; those documents are removed from the output
  5. subprocess with shell=False — no shell interpolation
  6. Output cap                  — truncate at 6 000 chars

Layers 3 and 4 were absent until 2026-08-20. "Read-only" was enforced against the *cluster*
and not against *what may be read*, so this tool answered questions `run_kubectl` refuses:
`helm get values kubeintellect -n kubeintellect`, `helm get manifest … -n kube-system`, and
`helm list -n monitoring` all ran. `docs/security.md` states that infrastructure namespaces are
blocked including reads and that Secrets are shielded for every role — guarantees about the
product, not about one of its two tools.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess

import yaml

from langchain_core.tools import tool

from app.core.config import settings
from app.tools import kubectl_errors
from app.tools.output_policy import mark_unavailable, truncation_marker, unavailable_notice
from app.tools.kubectl_tool import _blocked_resources, _flag_value, _resource_spellings
from app.tools.namespace_guard import withheld_note
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


# Helm's own connection/identity family — the same guarantee as `run_kubectl`'s
# `_CONNECTION_FLAGS`, spelled the way Helm spells it. Both tools talk to the same cluster with
# the same pinned kubeconfig, so a hole in either is a hole in the product: `helm list -A
# --kube-as-user system:masters` and `--kube-apiserver https://attacker.example.com` both reached
# `subprocess.run` byte-for-byte until 2026-08-20. `--kube-*` is matched by prefix because every
# Helm connection override is spelled that way.
_HELM_CONNECTION_FLAGS = frozenset({"--kubeconfig", "--registry-config", "--repository-config"})


def _connection_flag_in(tokens: list[str]) -> str | None:
    """The first connection/identity override in the command, or None."""
    for tok in tokens:
        if not tok.startswith("-"):
            continue
        name = tok.split("=", 1)[0]
        if name in _HELM_CONNECTION_FLAGS or name.startswith("--kube-"):
            return name
    return None


def _normalise(command: str) -> str:
    """Strip leading 'helm' if the LLM doubled it, and lowercase the verb."""
    cmd = command.strip()
    if not cmd.startswith("helm"):
        cmd = f"helm {cmd}"
    return cmd


def _extract_verb(tokens: list[str]) -> str:
    """The helm subcommand, ignoring any global flags that precede it.

    This was `tokens[1]`, the same parse `run_kubectl` had until pass 51 — but here it failed
    *closed*, because the check below is an allowlist: `helm -n prod list` was answered
    "'helm -n' is not a supported subcommand" rather than executed. A usability bug, not a
    bypass, and a direct demonstration of why the kubectl verb sets were inverted to an
    allowlist: the identical parser defect is catastrophic behind a deny-list and merely
    annoying behind an allowlist.
    """
    i = _skip_flags(tokens, 1)
    return tokens[i].lower() if i < len(tokens) else ""


def _skip_flags(tokens: list[str], start: int) -> int:
    """Index of the first token from `start` that is neither a flag nor a flag's value."""
    i = start
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-"):
            return i
        # `-n prod` consumes its value; `-n=prod`, `-nprod`, `-A` do not.
        if tok in ("-n", "--namespace", "-o", "--output", "--kube-context", "--kubeconfig"):
            i += 2
        else:
            i += 1
    return len(tokens)


def _blocked_namespace(tokens: list[str]) -> str | None:
    """The protected namespace this helm command targets, if any."""
    ns = _flag_value(tokens, "-n", "--namespace")
    if ns and ns.lower() in settings.kubectl_blocked_namespaces:
        return ns.lower()
    return None


# `kind: Secret`, `kind: "Secret"`, `kind: \'Secret\'` and `kind: Secret  # managed by …` are one
# line. The value used to be matched as a bare token to end-of-line, so measured 2026-08-20 both
# the quoted form and a trailing comment kept the document — and the base64 `data:` with it.
# A quote is not a kind, and a comment is not part of the value.
_KIND_RE = re.compile(r"""(?mi)^kind:\s*(?:"|')?([A-Za-z0-9.-]+)(?:"|')?\s*(?:\#.*)?$""")


def _strip_blocked_kinds(output: str) -> str:
    """Remove YAML documents whose `kind` is a protected resource.

    `helm get manifest` and `helm get all` render every object in the release, Secrets included
    and with their base64 `data:` intact. `run_kubectl` blocks Secrets and ServiceAccounts for
    **every** role and regardless of namespace, so leaving them readable here would make that
    guarantee a statement about one tool rather than about the product. Document-level removal
    keeps the rest of the manifest intact and readable, which is the point of the tool.
    """
    # Expanded on both sides — the rendered manifest's `kind:` is folded by
    # `_resource_spellings`, and so is what the operator configured.
    blocked = _blocked_resources()
    docs = re.split(r"(?m)^---\s*$", output)
    kept, dropped = [], 0
    for doc in docs:
        match = _KIND_RE.search(doc)
        if match and (_resource_spellings(match.group(1)) & blocked):
            dropped += 1
            continue
        kept.append(doc)
    result = "---".join(kept)
    if dropped:
        result += (
            f"\n[{dropped} object(s) of a protected kind were removed from this manifest. "
            "Kubernetes Secrets and ServiceAccount tokens are shielded from inspection "
            "to protect cluster credentials.]"
        )
    return result


def _log_silent_filter(dropped: int, out_format: str) -> None:
    """The one path where withholding cannot be announced in-band. Say so in the log.

    A bare JSON/YAML sequence has no field to carry the notice and no room after it, so
    `helm list -A -o json` returns a short array with nothing marking it short. Asserted as a
    limit in `tests/test_a_filtered_listing_says_so.py` rather than left to be discovered.
    """
    if dropped:
        logger.warning(
            f"run_helm: {dropped} release(s) withheld from a '-o {out_format}' listing; "
            "that format cannot carry the notice — the table format can."
        )


def _noting_releases(text: str, dropped: int) -> str:
    """Say when `helm list` came back short — same rule as the kubectl filters.

    A release listing is how the agent learns what is deployed. Removing rows without saying so
    turns "prometheus is not installed" into a conclusion the tool invited.
    """
    return text + withheld_note(dropped, "release") if dropped else text


def _filter_release_namespaces(output: str) -> str:
    """Drop rows/entries for protected namespaces from a `helm list -A` result.

    Handles the default table, `-o json` and `-o yaml`, because a filter that only covers the
    format nobody asked for is not a filter — see `_filter_namespace_output` in `kubectl_tool`.
    """
    blocked = settings.kubectl_blocked_namespaces
    stripped = output.lstrip()
    if stripped.startswith(("[", "{")):
        try:
            doc = json.loads(output)
        except ValueError:
            return (
                "[Protected] This release listing could not be parsed, so releases in "
                "protected namespaces could not be removed from it."
            )
        if isinstance(doc, list):
            # `helm list -o json` is a bare array, so there is nowhere inside the document to
            # put the notice and nothing may be appended after it without making the payload
            # unparseable. Documented limit — the table format says what was withheld.
            kept = [r for r in doc
                    if not (isinstance(r, dict)
                            and str(r.get("namespace", "")).lower() in blocked)]
            _log_silent_filter(len(doc) - len(kept), "json")
            return json.dumps(kept, indent=2)
        return output
    if stripped.startswith("- "):
        try:
            doc = yaml.safe_load(output)
        except yaml.YAMLError:
            return (
                "[Protected] This release listing could not be parsed, so releases in "
                "protected namespaces could not be removed from it."
            )
        if isinstance(doc, list):
            kept = [r for r in doc
                    if not (isinstance(r, dict)
                            and str(r.get("namespace", "")).lower() in blocked)]
            _log_silent_filter(len(doc) - len(kept), "yaml")
            return yaml.safe_dump(kept, default_flow_style=False, sort_keys=False)
        return output
    # Default table: NAME<tab>NAMESPACE<tab>...
    lines = output.splitlines(keepends=True)
    kept = []
    for idx, line in enumerate(lines):
        parts = line.split()
        if idx == 0 or len(parts) < 2 or parts[1].lower() not in blocked:
            kept.append(line)
    return _noting_releases("".join(kept), len(lines) - len(kept))


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

    # ── 2b. Connection and identity are the deployment's, not the caller's ────
    conn_flag = _connection_flag_in(tokens)
    if conn_flag:
        logger.warning(f"run_helm: refused connection/identity override {conn_flag!r}: {cmd!r}")
        return (
            f"[Protected] '{conn_flag}' is not permitted. Which cluster this connects to, and "
            "the identity it uses, are fixed by the deployment — they are not part of a query. "
            "Ask the question without it."
        )

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

    # ── 3b. Protected namespace ───────────────────────────────────────────────
    # The same blocklist run_kubectl enforces, read with the same parser, so the two tools
    # cannot disagree about which namespaces are off limits.
    blocked_ns = _blocked_namespace(tokens)
    if blocked_ns:
        logger.warning(f"run_helm: blocked protected access: {cmd!r}")
        return (
            f"[Protected] Access to namespace '{blocked_ns}' is not permitted. "
            "This is an infrastructure namespace."
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
        return mark_unavailable(
            "[Error] 'helm' binary not found on PATH. "
            "Helm may not be installed in this environment.",
            "helm is not on PATH.",
        )
    except subprocess.TimeoutExpired:
        return "[Error] helm command timed out after 30 seconds."
    except Exception as exc:
        return f"[Error] Failed to run helm: {exc}"

    stdout, stderr = proc.stdout or "", proc.stderr or ""

    # ── 4b. Strip what run_kubectl would never have returned ──────────────────
    # Every `helm get`, not an enumerated pair of subcommands. This read the subcommand as the
    # first non-flag token in `tokens[2:]`, so measured 2026-08-20 `helm -n prod get manifest shop`
    # — the flag written first, which is how anyone writes it — took `prod` as the subcommand and
    # returned the release's Secrets with their base64 `data:` intact. `helm get hooks` renders
    # manifests too and was never on the list at all. The stripper is a no-op on output that has
    # no protected `kind:` line, so there is nothing to gain from guessing which subcommand
    # produces one.
    # STDOUT ONLY. Merging stderr in first made a routine `WARNING: Kubernetes configuration
    # file is group-readable` part of the document handed to `json.loads`, so measured 2026-08-24
    # a **successful** `helm list -A -o json` and an unreachable cluster returned the same string
    # — "[Protected] This release listing could not be parsed" — with the release and the error
    # both deleted. A filter parses a listing; it must never be shown an error message.
    if verb == "get":
        stdout = _strip_blocked_kinds(stdout)
    if verb == "list":
        stdout = _filter_release_namespaces(stdout)
    stdout, detail = stdout.strip(), stderr.strip()

    # ── 4c. Compose the answer ────────────────────────────────────────────────
    # `run_kubectl` says `[kubectl exited N]`; the exit code reached this answer only when helm
    # printed nothing at all, so every other failure was returned as if it were the result.
    if proc.returncode != 0:
        output = f"[helm exited {proc.returncode}] " + (
            detail or "(helm wrote nothing to stderr)"
        )
        # helm talks to the same apiserver kubectl does, so it fails the same way and is
        # classified by the same patterns rather than a second copy of them.
        if kubectl_errors.interpret(detail)[0] in kubectl_errors.TERMINAL_PATTERNS:
            output += "\n" + unavailable_notice("The cluster is not reachable from here.")
        # `run_kubectl` has to record whether kubectl printed anything *before* its pipe
        # emulator runs, because `_apply_pipes("")` manufactures "(no matching lines)". Neither
        # helm filter does that — both are the identity on input with nothing in it, which
        # `test_a_helm_warning_is_not_a_parse_failure.py` pins so this stays true.
        if stdout:
            output += (
                "\n\nhelm also produced this output before/alongside the error — it may be "
                f"partial, and absence from it is NOT evidence:\n{stdout}"
            )
    else:
        output = stdout or detail or "(no output)"
        if stdout and detail:
            output += (
                f"\n\n[helm also wrote to stderr. helm exited 0, so this is a warning about "
                f"the client, not part of the result:\n{detail}]"
            )

    # ── 5. Output cap ─────────────────────────────────────────────────────────
    if len(output) > _OUTPUT_CAP:
        omitted = len(output) - _OUTPUT_CAP
        # Until 2026-08-24 this said "[... N chars truncated]" — which matches neither string
        # the coordinator prompt tells the model to watch for, so the one reader of this line
        # was never looking for it. The wording is not this module's to choose.
        output = output[:_OUTPUT_CAP] + "\n" + truncation_marker(
            omitted, hint="narrow with -n <namespace> or name a single release"
        )

    logger.debug(f"run_helm: exit={proc.returncode} output_len={len(output)}")
    return output or "(no output)"
