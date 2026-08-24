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
import json
import shlex
import subprocess
from typing import Annotated

import yaml
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool
from langgraph.types import interrupt

from app.core.config import settings
from app.tools import kubectl_errors
from app.tools.namespace_guard import (
    annotate_withheld,
    drop_blocked_table_rows,
    withheld_note,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── Risk tables ───────────────────────────────────────────────────────────────

_HIGH_RISK = {
    "delete", "drain", "replace", "taint",
    # `cp` reads or writes arbitrary paths inside a container — including mounted Secrets,
    # which is the credential block bypassed by another route. `debug` attaches an ephemeral
    # container and, with `node/…`, gives a privileged pod on the node itself.
    "cp", "debug",
}
_MEDIUM_RISK = {
    "patch", "apply", "scale", "exec", "cordon", "uncordon", "create", "run", "set",
    # Mutating verbs that were in no risk set at all until 2026-08-20, so a read-only key
    # could run them with no approval prompt: object mutation, rollout control, resource
    # creation, and two ways into a running container or an internal service.
    "label", "annotate", "expose", "autoscale", "port-forward", "attach",
    "evict", "certificate",
    # `rollout` is deliberately absent: `rollout status`/`history` are reads and must stay
    # available to a read-only key. Its write subcommands are caught by `_is_write_verb`,
    # which is subcommand-aware, and land on "medium" through the fail-closed branch of
    # `_classify_risk`. Listing it here would gate the reads too.
}
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

# Verbs with no side effects. This is an ALLOWLIST and the default is "write": a verb absent
# from it is treated as a mutation and gated, so a kubectl release that adds a verb — or a verb
# nobody thought to enumerate — fails closed instead of being waved through as a read.
#
# It used to be a dead set (defined, never referenced) that also listed `rollout` as read-only.
# Meanwhile the risk tables were a deny-list, which meant every verb they did not name was
# treated as harmless: measured 2026-08-20, a read-only API key could run `label`, `annotate`,
# `rollout restart`, `rollout undo`, `cp`, `debug`, `expose`, `autoscale`, `port-forward` and
# `attach` with no approval prompt at all.
_READ_ONLY_VERBS = {
    "get", "describe", "logs", "top", "diff", "explain", "events",
    "version", "cluster-info", "api-resources", "api-versions",
    "wait", "kustomize", "completion", "help", "options",
}

# Verbs whose safety depends on the subcommand: `rollout status` reads, `rollout restart` does
# not. Anything not listed here is a write, so `rollout pause` or a future subcommand is gated.
_READ_ONLY_SUBCOMMANDS = {
    "rollout": {"status", "history"},
    "config": {"view", "get-contexts", "current-context", "get-clusters", "get-users"},
    "certificate": set(),
    # `auth can-i` and `auth whoami` ask questions. `kubectl auth reconcile` **writes** — it
    # creates and updates Roles, RoleBindings, ClusterRoles and ClusterRoleBindings from a
    # manifest. `auth` sat in the read-only verb set above because of `can-i`, so measured
    # 2026-08-20 a **readonly** API key ran `kubectl auth reconcile -f -` with a
    # ClusterRoleBinding granting `cluster-admin` and it executed with no approval prompt,
    # while `kubectl create -f -` carrying the identical manifest was refused. The one role
    # that is meant to hold no privilege could grant itself every privilege.
    "auth": {"can-i", "whoami"},
}


def _exit_is_an_answer(verb: str, args: list[str], code: int) -> bool:
    """True when kubectl used a non-zero exit to *answer*, not to report a failure.

    Two of the read verbs do this, both documented, both the ordinary outcome:

      * `kubectl diff` — exit 0 no differences, **exit 1 differences found**, >1 kubectl or the
        diff program failed. Finding differences is the entire point of running it.
      * `kubectl auth can-i` — **exit 1 when the answer is "no"**, with `no` on stdout.

    Measured 2026-08-24: treating those as failures wrapped a complete diff, and an authoritative
    `no`, in *"it may be partial, and absence from it is NOT evidence"* — and left `can-i`
    asymmetric, a clean `yes` against an `no` the agent is told not to trust. Only the listed code
    counts; a higher one is still an error, which is exactly how kubectl documents `diff`.
    """
    if verb == "diff":
        return code == 1
    if verb == "auth" and _operand_after_verb(args) == "can-i":
        return code == 1
    return False


def _is_write_verb(verb: str, args: list[str]) -> bool:
    """True when the command can change something. Unknown verbs count as writes."""
    if not verb:
        return False
    if verb in _READ_ONLY_SUBCOMMANDS:
        i = _skip_flags(args, args.index(verb) + 1) if verb in args else len(args)
        sub = args[i] if i < len(args) else ""
        return sub not in _READ_ONLY_SUBCOMMANDS[verb]
    return verb not in _READ_ONLY_VERBS

# kubectl edit requires an interactive terminal that is never available in the container.
_REJECTED_VERBS = {"edit"}

# Read-only against the cluster is not the same as read-only against *what may be read* — the
# same distinction `run_helm` makes about `helm get manifest`. `kubectl cluster-info dump` walks
# every namespace and prints pod specs, events and container logs, so it returns the contents of
# the namespaces the blocklist exists to withhold. None of the namespace filters reach it: the
# verb names no resource type, so `_extract_resource_type` is None and `_filter_namespace_output`
# and `_filter_all_namespaces_output` both pass it straight through. Measured 2026-08-20, a
# **readonly** key ran `kubectl cluster-info dump --all-namespaces` unfiltered. There is no
# per-object shape to filter here — it is a concatenated dump — so it is refused, on the same
# rule `_unfilterable_format_message` applies to `-o custom-columns`. Bare `cluster-info`, which
# prints the control-plane endpoints, is untouched.
_REJECTED_SUBCOMMANDS: dict[str, set[str]] = {"cluster-info": {"dump"}}

# ── Shell injection guard ─────────────────────────────────────────────────────
# Pipe (|) is intentionally excluded — it is handled in Python via _apply_pipes.
# Backslash (\) is intentionally excluded: it appears in valid jsonpath
# separators like {"\n"} and {"\t"}, and is harmless because we run with
# shell=False (backslashes are passed literally to kubectl, not interpreted
# by a shell).
_SHELL_METACHAR = re.compile(r"[;&`$<>]")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _classify_risk(verb: str, args: list[str] | None = None) -> str:
    if verb in _HIGH_RISK:
        return "high"
    if verb in _MEDIUM_RISK:
        return "medium"
    # Fail closed: an unrecognised verb that is not a known read is a write we have not
    # classified, and "unclassified" must not mean "free".
    if args is not None and _is_write_verb(verb, args):
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
    # `args[2]` is the operand only when the command is written verb-first with nothing in
    # between. `kubectl delete -n prod namespace shop` — the *natural* way to write it — put
    # `-n` there, and `kubectl -n prod set image deploy/api …` put `prod` there. Measured
    # 2026-08-20, seven ordinary renderings returned False, including `delete --force namespace`,
    # `delete --ignore-not-found pv` and `set --record image`. This is the one gate that fires
    # **through** `hitl_bypass`, so on an auto-approve session a cascading namespace delete ran
    # with no prompt at all while the same command without the flag stopped and asked. The
    # sibling parser `_extract_resource_type` was corrected for exactly this trap; this one was
    # missed, so both now share `_operand_after_verb`.
    operand = _operand_after_verb(args)
    if verb == "set" and operand in _ALWAYS_CONFIRM_SET_SUBCOMMANDS:
        return True
    if verb == "delete" and operand.split("/")[0] in _ALWAYS_CONFIRM_DELETE_TARGETS:
        return True
    return False


def _normalise(command: str) -> str:
    """Strip leading 'kubectl' duplication if the LLM included it twice."""
    cmd = command.strip()
    if not cmd.startswith("kubectl"):
        cmd = f"kubectl {cmd}"
    return cmd


# kubectl global flags that consume the following token as their value. Needed so a flag's
# *value* is never mistaken for the subcommand: in `kubectl -n prod delete deploy api`, the
# token after `kubectl` is `-n` and the token after that is `prod` — neither is the verb.
_VALUE_FLAGS = frozenset({
    "-n", "--namespace", "-o", "--output", "--context", "--cluster", "--user", "--kubeconfig",
    "--server", "-s", "--token", "--as", "--as-group", "--as-uid", "--request-timeout",
    "--cache-dir", "-l", "--selector", "--field-selector", "--certificate-authority",
    "--client-certificate", "--client-key", "--tls-server-name", "--username", "--password",
    "-f", "--filename", "--log-flush-frequency", "--v", "--profile", "--profile-output",
    "--chunk-size",
})

# kubectl's **boolean** global flags. pflag accepts a boolean as a bare token, so a boolean listed
# in `_VALUE_FLAGS` above makes `_skip_flags` consume the *next* token as its value — and that
# token is the verb. `--warnings-as-errors` was listed there. Measured 2026-08-20 on an
# auto-approve session, prefixing it was enough for `get secrets` and `get sa` to return
# credential data and for `delete namespace shop` to run with no always-confirm prompt: every
# gate in this module reads the verb from that one walk, so one wrong row in the table moved all
# of them at once. The two sets are asserted disjoint in
# `tests/test_a_boolean_flag_is_not_a_value_flag.py` so a future addition cannot repeat it.
_BOOLEAN_GLOBAL_FLAGS = frozenset({
    "--warnings-as-errors", "--insecure-skip-tls-verify",
    "--disable-compression", "--match-server-version",
})


# Flags that decide **which cluster the command talks to and as whom**. Every other gate in this
# module reasons about the *verb*, the *resource* and the *namespace* — what is being asked. None
# of them look at where the answer comes from or under whose identity, and until 2026-08-20
# nothing did: `kubectl get pods --as=system:masters -A`,
# `--server=http://attacker.example.com:8080`, `--kubeconfig=/tmp/other.conf` and
# `--insecure-skip-tls-verify` all reached `subprocess.run` byte-for-byte.
#
# These are **deployment configuration, not arguments**. The server pins the cluster via
# `KUBECONFIG` (see `_kubectl_env`), and the in-app role comes from the run config. A caller that
# can override either has stepped outside every guarantee this file makes — the role check still
# passes, the namespace filter still runs, and both are now reasoning about a different cluster's
# answer, or an attacker's.
#
# Refused rather than stripped: silently dropping a flag would answer a different question than
# the one asked, which is the failure mode pass 84 closed for filtered listings.
_CONNECTION_FLAGS = frozenset({
    # identity / impersonation
    "--as", "--as-group", "--as-uid",
    "--user", "--username", "--password", "--token",
    # which cluster
    "--server", "-s", "--kubeconfig", "--context", "--cluster",
    # transport trust
    "--insecure-skip-tls-verify", "--certificate-authority",
    "--client-certificate", "--client-key", "--tls-server-name",
})


def _authorised_identity(config: RunnableConfig | None) -> str | None:
    """The one impersonation token the *application* placed on this command, if any.

    The rule pass 89 wrote is "the caller does not choose the identity". The v5 capability
    sandbox (`app/tools/aci/sandbox.py`) is not a caller: it is app code narrowing the identity
    to a ServiceAccount that holds strictly fewer rights, and impersonation is the entire
    mechanism by which it does so. Blanket-refusing `--as` therefore turned that sandbox off —
    measured 2026-08-20, `run_as("get pods -n prod", "read-only")` returned
    `[Protected] '--as' is not permitted.` and reached kubectl not at all, **with HITL already
    bypassed on the grounds that the impersonated RBAC was the guard**.

    So the exemption is an exact-match on a value carried in the run config, which arrives the
    same way `hitl_bypass` and `user_role` do — injected by the graph, never writable by a model.
    A token that merely *looks* like the sandbox's is not enough, and any second identity or
    connection flag alongside it still fails: `--as-group=system:masters` next to a legitimate
    `--as` is precisely the escape `sandbox.run_as` refuses to build.
    """
    if not config:
        return None
    value = (config.get("configurable") or {}).get("sandbox_identity")
    return value if isinstance(value, str) and value.startswith("--as=") else None


def _identity_is_authorised(tokens: list[str], authorised: str) -> bool:
    """True iff every connection/identity token in the command is exactly `authorised`."""
    found = [
        tok for tok in tokens
        if tok.startswith("-")
        and (tok.split("=", 1)[0] in _CONNECTION_FLAGS or tok.split("=", 1)[0].startswith("--as"))
    ]
    return found == [authorised]


def _connection_flag_in(tokens: list[str]) -> str | None:
    """The first connection/identity flag in the command, or None.

    Matched on the flag *name*, so `--as=x` and `--as x` are the same thing. The `--as` family is
    additionally matched by prefix: every kubectl impersonation flag is spelled `--as…`, so a
    future one is refused without this list being updated. That prefix rule is the only part of
    this check that is not an enumeration — stated plainly because pass 88 closed a bypass that
    existed for exactly this reason, and the rest of this set *is* an enumeration of kubectl's
    documented global flags.
    """
    for tok in tokens:
        if not tok.startswith("-"):
            continue
        name = tok.split("=", 1)[0]
        if name in _CONNECTION_FLAGS or name.startswith("--as"):
            return name
    return None


def _skip_flags(tokens: list[str], start: int) -> int:
    """Index of the first token from `start` that is neither a flag nor a flag's value."""
    i = start
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-"):
            return i
        # `--flag=value` carries its value inline; `--flag value` consumes the next token.
        if "=" not in tok and tok in _VALUE_FLAGS:
            i += 2
        else:
            i += 1
    return len(tokens)


def _operand_index(tokens: list[str]) -> int:
    """Index of the verb's operand, or `len(tokens)` when the command has none.

    Skip the global flags to reach the verb, then skip the verb's own flags to reach what it
    acts on. Kept as an index as well as a value because the namespace guard needs to keep
    reading positional arguments *after* the operand.
    """
    return _skip_flags(tokens, _skip_flags(tokens, 1) + 1)


def _operand_after_verb(tokens: list[str]) -> str:
    """The first token after the verb that is neither a flag nor a flag's value, lowercased.

    That token is the resource kind (`delete namespace shop`) or the subcommand
    (`set image …`). Every gate that reasons about *what* is being acted on needs it, and a
    fixed index finds it only in the one rendering where no flag comes first.
    """
    i = _operand_index(tokens)
    return tokens[i].lower() if i < len(tokens) else ""


def _extract_verb(tokens: list[str]) -> str:
    """Return the kubectl subcommand verb, ignoring any global flags that precede it.

    This used to be `tokens[1]`, which is correct only when the command is written with the
    verb first. `kubectl -n prod delete deployment api` is equally valid kubectl and put `-n`
    in that position — so the verb read as `-n`, which is in no risk set, no role set and no
    rejected set. Measured 2026-08-20: that single reordering let a **read-only** API key
    delete Deployments and PVCs, drain nodes, and read every Secret, because every gate in
    `run_kubectl` keys off this one value. See `_destructive_verbs_in()` for the backstop.
    """
    i = _skip_flags(tokens, 1)
    return tokens[i] if i < len(tokens) else ""


def _destructive_verbs_in(tokens: list[str]) -> set[str]:
    """Every destructive verb appearing as a bare token anywhere in the command.

    Defence in depth, deliberately independent of position. `_extract_verb` above is the
    correct parse; this is the check that survives a parse we did not anticipate. It matches
    whole tokens only, so `-l app=delete` and `--dry-run=delete` do not trip it.
    """
    return {tok for tok in tokens[1:] if tok in DESTRUCTIVE_VERBS}


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


# ── grep emulation ────────────────────────────────────────────────────────────
# Boolean flags, and the flags that consume a value. The distinction is the whole point:
# a value-taking flag whose value is not consumed leaves that value in the *pattern*.
_GREP_BOOL = {"-v", "-i", "-E", "-F", "-w", "-x", "-c", "-n", "-o", "-s", "-a"}
_GREP_VALUE = {"-A", "-B", "-C", "-m", "-e"}
_GREP_LONG = {
    "--invert-match": "-v", "--ignore-case": "-i", "--extended-regexp": "-E",
    "--fixed-strings": "-F", "--word-regexp": "-w", "--line-regexp": "-x",
    "--count": "-c", "--line-number": "-n", "--only-matching": "-o",
    "--no-messages": "-s", "--text": "-a",
    "--after-context": "-A", "--before-context": "-B", "--context": "-C",
    "--max-count": "-m", "--regexp": "-e",
}


class _Grep:
    """The flags this emulator understands, parsed the way grep parses them."""

    def __init__(self) -> None:
        self.opts: set[str] = set()
        self.values: dict[str, str] = {}
        self.patterns: list[str] = []
        self.operands: list[str] = []


def _parse_grep(tokens: list[str], segment: str) -> _Grep:
    """Parse `grep` arguments, rejecting any flag this emulator does not implement.

    Until 2026-08-20 this loop skipped every token starting with `-` that was not one of
    `-v`, `-i`, `-E`. Two consequences, both measured against the system grep:

    * **A value-taking flag left its value in the pattern.** `grep -A 3 Traceback` searched
      for the pattern ``"3 Traceback"``, matched nothing, and returned *(no matching lines)*
      where real grep returns five. `-A/-B/-C` is the standard idiom for reading a stack trace
      out of a log, so the agent was told the traceback it was looking at did not exist.
    * **Combined short flags were dropped entirely.** `-iv` is neither `-i` nor `-v`, so
      `grep -iv info` ran as `grep info` — the exact **complement** of the requested set.

    Both failed silently, which the original docstring already ruled out for the non-grep case
    ("raises ValueError so the LLM knows to ask differently rather than silently getting wrong
    results"). The same rule now applies to grep's own flags: anything unimplemented is named
    and refused.
    """
    g = _Grep()
    i, end_of_flags = 1, False
    while i < len(tokens):
        tok = tokens[i]
        if end_of_flags or tok == "-" or not tok.startswith("-"):
            g.operands.append(tok)
            i += 1
            continue
        if tok == "--":
            end_of_flags = True
            i += 1
            continue
        if tok.startswith("--"):
            name, _, attached = tok.partition("=")
            short = _GREP_LONG.get(name)
            if short is None:
                raise ValueError(
                    f"grep in pipe segment {segment!r} uses {name}, which this pipe emulator "
                    f"does not implement. Supported: {_grep_supported()}."
                )
            if short in _GREP_VALUE:
                if not attached:
                    if i + 1 >= len(tokens):
                        raise ValueError(f"grep option {name} in {segment!r} needs a value.")
                    attached = tokens[i + 1]
                    i += 1
                _take_grep_value(g, short, attached, segment)
            else:
                g.opts.add(short)
            i += 1
            continue
        # Short cluster: -iv, -A3, -vA 3 …
        j = 1
        while j < len(tok):
            short = f"-{tok[j]}"
            if short in _GREP_VALUE:
                rest = tok[j + 1:]
                if not rest:
                    if i + 1 >= len(tokens):
                        raise ValueError(f"grep option {short} in {segment!r} needs a value.")
                    rest = tokens[i + 1]
                    i += 1
                _take_grep_value(g, short, rest, segment)
                j = len(tok)
                break
            if short not in _GREP_BOOL:
                raise ValueError(
                    f"grep in pipe segment {segment!r} uses {short}, which this pipe emulator "
                    f"does not implement. Supported: {_grep_supported()}."
                )
            g.opts.add(short)
            j += 1
        i += 1
    return g


def _grep_supported() -> str:
    return " ".join(sorted(_GREP_BOOL | _GREP_VALUE))


def _take_grep_value(g: _Grep, short: str, value: str, segment: str) -> None:
    if short == "-e":
        g.patterns.append(value)
        return
    if not value.lstrip("+-").isdigit():
        raise ValueError(
            f"grep option {short} in pipe segment {segment!r} needs a number, got {value!r}."
        )
    g.values[short] = value


def _grep_regex(g: _Grep, segment: str) -> re.Pattern[str]:
    """Build one regex from every pattern, honouring -F/-w/-x/-i.

    Divergence from grep, on purpose: several bare operands are joined into one pattern.
    Real grep would treat the second as a *file*, and there are no files in a pipe segment —
    so an unquoted multi-word pattern is the only thing it can have meant.
    """
    parts = g.patterns or ([" ".join(g.operands)] if g.operands else [])
    parts = [p for p in parts if p != ""]
    if not parts:
        raise ValueError(f"grep in pipe segment {segment!r} has no pattern.")
    if "-F" in g.opts:
        parts = [re.escape(p) for p in parts]
    body = "|".join(f"(?:{p})" for p in parts)
    if "-x" in g.opts:
        body = f"^(?:{body})$"
    elif "-w" in g.opts:
        body = rf"\b(?:{body})\b"
    try:
        return re.compile(body, re.IGNORECASE if "-i" in g.opts else 0)
    except re.error as exc:
        raise ValueError(f"grep pattern in {segment!r} is not a valid regex: {exc}") from exc


def _run_grep(output: str, g: _Grep, compiled: re.Pattern[str]) -> str:
    lines = output.splitlines()
    invert = "-v" in g.opts
    hits = [n for n, ln in enumerate(lines) if bool(compiled.search(ln)) != invert]
    if "-m" in g.values:
        hits = hits[: int(g.values["-m"])]

    if "-c" in g.opts:
        return f"{len(hits)}\n"
    if not hits:
        return "(no matching lines)"
    if "-o" in g.opts and not invert:
        found = [m for n in hits for m in compiled.findall(lines[n])]
        flat = [f if isinstance(f, str) else next((x for x in f if x), "") for f in found]
        return "".join(f"{f}\n" for f in flat) or "(no matching lines)"

    ctx = int(g.values.get("-C", 0))
    after = int(g.values.get("-A", ctx))
    before = int(g.values.get("-B", ctx))
    wanted: dict[int, bool] = {}
    for n in hits:
        for k in range(max(0, n - before), min(len(lines), n + after + 1)):
            wanted.setdefault(k, False)
        wanted[n] = True

    # grep prints the `--` group separator only when context was requested; without it,
    # non-adjacent matches follow one another directly.
    separate = bool(after or before)
    out, prev = [], None
    for n in sorted(wanted):
        if separate and prev is not None and n != prev + 1:
            out.append("--\n")
        sep = ":" if wanted[n] else "-"
        out.append(f"{n + 1}{sep}{lines[n]}\n" if "-n" in g.opts else f"{lines[n]}\n")
        prev = n
    return "".join(out)


def _apply_pipes(output: str, pipe_segments: list[str]) -> str:
    """Apply a list of pipe segments (e.g. ['grep foo', 'grep -v bar']) to output.

    Only grep is supported. Any other command — and any grep flag this emulator does not
    implement — raises ValueError so the LLM knows to ask differently rather than silently
    getting wrong results.
    """
    for segment in pipe_segments:
        tokens = shlex.split(segment.strip())
        if not tokens or tokens[0] != "grep":
            raise ValueError(
                f"Pipe segment {segment!r} contains disallowed shell characters or unsupported command. "
                "Only 'grep' is allowed after '|'."
            )
        g = _parse_grep(tokens, segment)
        output = _run_grep(output, g, _grep_regex(g, segment))
    return output


# Shorthands that never take a value, so a single-dash group continues past them. Measured from
# `kubectl <verb> --help` across every subcommand of kubectl v1.36.4 rather than recalled: a
# boolean prints `=false`, a value flag prints `=''` or `=[]`.
_KUBECTL_BOOLEAN_SHORTHANDS = frozenset("AhiqRtw")

# `-f` and `-p` are the only two letters that are boolean in one subcommand and a value flag in
# every other: `--follow`/`--previous` on `kubectl logs`, `--filename`/`--patch` elsewhere. The
# verb is what decides, so a caller that knows it passes it; one that does not stops at those
# letters rather than guessing, because guessing wrong invents a namespace out of a filename
# (`-fns.yaml` would otherwise read as `-n s.yaml`).
_VERB_BOOLEAN_SHORTHANDS = {"logs": frozenset("fp")}


def _flag_value(args: list[str], short: str, long: str, verb: str | None = None) -> str | None:
    """The value of a flag, in every form pflag accepts.

    `-o json`, `-o=json`, `-ojson`, `--output json`, `--output=json` are one flag. Every place
    that reads a flag value needs all of them, so there is one parser rather than one per caller —
    the `-n` reader and the `-o` reader drifted apart exactly this way, and the `-o` one was
    still missing the attached form when it filtered a namespace listing.

    **Those five were not all of them.** pflag also accepts a *combined shorthand group*, and
    until 2026-08-24 this read none of that family — it looked for an argument that began with
    `-n`, and `-Rn kube-system` begins with `-R`. Verified against kubectl v1.36.4 itself:
    `-Rn kube-system`, `-Rnkube-system` and `-Rn=kube-system` all parse, `-Rwn` fails on the
    *`w`* alone (so the group is decomposed left to right), and `-Rn` with nothing after it says
    `flag needs an argument: 'n' in -n` (so `-n` really does consume the next token). Every one
    of them was invisible here, which meant `kubectl exec -itn kube-system pod -- sh` reached
    `_check_protected_access` with **no namespace at all** — the guard did not decide kube-system
    was allowed, it never saw it. Same defect as the 2026-08-20 `-nkube-system` one, one form out.

    A group is walked left to right, exactly as pflag walks it: the first letter that takes a
    value swallows the rest of the group, so scanning the whole group for the target letter would
    be wrong — `-ojson` contains an `n`, and reading it as `--namespace` would take whatever came
    next. The walk therefore stops at any letter not known to be boolean.
    """
    letter = short.lstrip("-")
    booleans = _KUBECTL_BOOLEAN_SHORTHANDS | _VERB_BOOLEAN_SHORTHANDS.get(verb or "", frozenset())
    for i, arg in enumerate(args):
        if arg in (short, long):
            return args[i + 1] if i + 1 < len(args) else None
        if arg.startswith(f"{long}="):
            return arg.split("=", 1)[1] or None
        # A single-dash group. Guard on `--` first: `--no-headers` and `--overwrite` begin with a
        # dash and the same letter but are different flags. `-` alone is stdin, not a group.
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            group = arg[1:]
            for j, char in enumerate(group):
                if char == letter:
                    rest = group[j + 1:]
                    if rest.startswith("="):
                        return rest[1:] or None
                    if rest:
                        return rest
                    return args[i + 1] if i + 1 < len(args) else None
                if char not in booleans:
                    break  # this letter takes a value; the rest of the group is that value
    return None


def _extract_namespace(args: list[str], verb: str | None = None) -> str | None:
    """Extract the -n / --namespace value, in every form kubectl accepts.

    kubectl parses its flags with pflag, which accepts a shorthand's value **attached** to it,
    with or without an `=`. All four of these are the same command:

        -n kube-system    --namespace kube-system    --namespace=kube-system
        -n=kube-system    -nkube-system

    This function used to read only the first three, so the last two returned None and the
    protected-namespace check in `_check_protected_resources` was skipped entirely — the guard
    did not decide the namespace was allowed, it never saw one. Measured 2026-08-20 through the
    real tool: `kubectl get pods -n kube-system` was refused for admin and operator, while
    `kubectl get pods -nkube-system` **ran**, and an admin's `kubectl delete pod x -nkube-system`
    was downgraded from an outright `[Protected]` refusal to an ordinary approval prompt. Same
    protected namespace, same intent, two characters of whitespace apart.
    """
    return _flag_value(args, "-n", "--namespace", verb)


def _targeted_namespaces(verb: str, args: list[str]) -> list[str]:
    """Every namespace named as the command's *target*, e.g. `kubectl delete ns kube-system`.

    `_extract_namespace` reads `-n/--namespace`, which is where a namespace usually appears —
    but when the resource type *is* a namespace, the name is a positional argument instead.
    Measured 2026-08-20: `kubectl delete pod x -n kube-system` was refused outright while
    `kubectl delete namespace kube-system` only reached an approval prompt, for the same
    protected namespace. The docs say infrastructure namespaces are blocked including reads,
    so the syntax should not decide which is true.

    This returned only the *first* positional name, and only when the name was a separate
    token. kubectl accepts neither restriction, and measured 2026-08-20 both extra renderings
    reached the cluster unrefused:

        kubectl delete ns/kube-system            -> no target found (name is inside the operand)
        kubectl delete ns shop kube-system       -> target "shop" only; kube-system unseen
        kubectl get ns/kube-system               -> a read of a protected namespace, ungated

    So it returns a list now: a guard that reports one of two answers is a guard that misses
    the second. Empty for a bare `kubectl get namespaces` — listing them is allowed, and the
    output is filtered by `_filter_namespace_output`.
    """
    if _extract_resource_type(verb, args) not in ("namespace", "namespaces", "ns"):
        return []
    resource_at = _operand_index(args)
    if resource_at >= len(args):
        return []
    out: list[str] = []
    # `ns/kube-system` carries the name inside the operand token itself.
    inline = args[resource_at].split("/", 1)
    if len(inline) == 2 and inline[1]:
        out.append(inline[1].lower())
    # `delete ns shop kube-system` names as many as it likes, positionally, with flags allowed
    # anywhere between them.
    i = _skip_flags(args, resource_at + 1)
    while i < len(args):
        out.append(args[i].split("/")[-1].lower())
        i = _skip_flags(args, i + 1)
    return out


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
    if verb not in _resource_verbs:
        return None
    # Same positional trap as the verb: `kubectl -n prod get secrets` put `prod` here, so the
    # Secret block never fired and kubectl ran. `_operand_after_verb` finds the verb, then the
    # first token after it that is not a flag or a flag's value — and it is shared with
    # `_requires_always_confirm`, which carried the uncorrected version of this same parse.
    operand = _operand_after_verb(args)
    # Handle "resource/name" shorthand (e.g. "deployment/myapp")
    return operand.split("/")[0] or None


# kubectl's own short names for the resource types the blocklist cares about. The guard matches
# the literal token the model typed, and `kubectl get sa` is exactly as valid as
# `kubectl get serviceaccounts` — measured 2026-08-20, `sa` returned ServiceAccounts through the
# block. Only credential-relevant aliases are listed; this is a security floor, not an attempt to
# mirror `kubectl api-resources`.
_RESOURCE_ALIASES = {
    "sa": "serviceaccounts",
    "secret": "secrets",
    "serviceaccount": "serviceaccounts",
}


def _resource_spellings(resource: str | None) -> set[str]:
    """Every spelling of `resource` that must be tested against the blocklist.

    kubectl accepts a resource as `name`, `name/instance`, and the fully-qualified
    `name.version.group` (`secrets.v1.`) — and accepts documented short names. Each of those
    reached the cluster while the blocklist saw a string it did not recognise. Both the raw
    token and the canonical name are returned, so an operator who blocked a short name in their
    own configuration keeps working.

    Singular and plural are the same resource to kubectl (`get configmap` and `get configmaps`
    are one command), so both are derived here. Measured 2026-08-20, they were not: with
    `KUBECTL_BLOCKED_RESOURCES="configmap"`, `kubectl get configmaps -n shop` was **allowed** —
    and `configmaps` is the spelling `kubectl api-resources` prints. That symmetry is applied to
    the configured entries as well as to the typed token (see `_blocked_resources()`), so it no
    longer matters which of the two an operator wrote.

    **What this does not do.** kubectl's short names are per-type and come from API discovery;
    they cannot be derived from a string. `_RESOURCE_ALIASES` covers the credential types this
    guard has a floor for, and nothing else — so an operator who blocks `configmaps` has *not*
    blocked `cm`. That limit is documented next to the setting rather than papered over with a
    guessed table.
    """
    if not resource:
        return set()
    raw = resource.lower()
    base = raw.split(".")[0]          # `secrets.v1.` → `secrets`
    out = {raw, base}
    out.add(_RESOURCE_ALIASES.get(base, base))
    for form in list(out):
        out |= _number_forms(form)
    out.discard("")
    return out


# Kubernetes resource names are regular English, so singular ⇄ plural is derivable — but only
# with the -es / -ies rules. Naive `+ "s"` gets `configmaps` right and `ingresses` wrong, which
# would leave the operator's `ingress` entry blocking the singular alone.
_ES_SUFFIXES = ("s", "x", "z", "ch", "sh")


def _number_forms(word: str) -> set[str]:
    """`{singular, plural}` for a resource name. Over-generation is harmless: an extra string
    that names no resource simply never matches anything kubectl would accept."""
    if not word:
        return set()
    out = {word}
    # singular → plural
    if word.endswith(_ES_SUFFIXES):
        out.add(word + "es")
    elif word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        out.add(word[:-1] + "ies")
    else:
        out.add(word + "s")
    # plural → singular
    if word.endswith("ies") and len(word) > 3:
        out.add(word[:-3] + "y")
    elif word.endswith("es") and word[:-2].endswith(_ES_SUFFIXES):
        out.add(word[:-2])
    elif word.endswith("s") and len(word) > 1:
        out.add(word[:-1])
    return out


def _blocked_resources() -> frozenset[str]:
    """The blocklist, expanded through the same spelling rules as the typed token.

    Both sides of the comparison, not one: `_resource_spellings` folds case and plural for the
    token the model typed, and this does it for what the operator configured.
    """
    out: set[str] = set()
    for entry in settings.kubectl_blocked_resources:
        out |= _resource_spellings(entry)
    return frozenset(out)


def _noting_namespaces(text: str, dropped: int) -> str:
    """Append the withheld note when a namespace listing came back short.

    Without it the caller cannot tell a filtered listing from a complete one, and a listing is
    the only evidence it has: `kubectl get ns monitoring` is refused out loud, but
    `kubectl get ns` used to return a short list in silence and an agent would report the
    namespace as absent.
    """
    return text + withheld_note(dropped, "namespace") if dropped else text


# kubectl's *fixed-shape* outputs — the only ones whose layout is decided by kubectl and can
# therefore be filtered. `name` and `jsonpath` are handled separately (see the callers).
_FIXED_SHAPE_FORMATS = frozenset({"", "wide", "json", "yaml"})


def _unfilterable_format_message(out_format: str, listing: str) -> str:
    """Refuse an output format whose shape the *caller* chose.

    `-o custom-columns`, `-o go-template`, `-o jsonpath` and their `-file` variants render
    whatever the caller asked for, in whatever order. There is no column, field or path the
    filter can rely on, so the row belonging to a protected namespace is indistinguishable from
    any other.

    Until 2026-08-20 these fell through to the branch that assumes a kubectl table with NAMESPACE
    as the first column, and were returned **whole and unannotated**. The check was a deny-list of
    the two formats someone thought of. `run_helm`'s docstring already records why that shape is
    wrong, about the sibling verb check: *an allowlist turns a parser bug into a usability
    complaint; a deny-list turns the same bug into a bypass.* The verbs were inverted; the output
    format was not.
    """
    return (
        f"[Protected] '-o {out_format}' renders the output you choose, so results from "
        f"infrastructure namespaces cannot be separated out of {listing}. Re-run with "
        "-o json (filtered per item), or narrow the query with -n <namespace>."
    )


def _filter_structured_namespaces(output: str, out_format: str, blocked: frozenset[str]) -> str:
    """Drop blocked namespaces from a `-o json` / `-o yaml` listing.

    This used to `return output` unchanged, with the comment *"too complex to strip reliably;
    blocked at execution anyway"*. The second half was false: `kubectl get namespaces -o json`
    names no individual namespace, so `_targeted_namespaces` correctly returns [] and the
    command runs — nothing blocks it at execution, and the filter every other output format
    applies was skipped. `-o json` is also the format a model asks for most readily.

    Fails closed: if the payload cannot be parsed it is replaced, never returned unfiltered.
    """
    try:
        doc = json.loads(output) if out_format == "json" else yaml.safe_load(output)
    except (ValueError, yaml.YAMLError):
        doc = None
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        return (
            "[Protected] This namespace listing could not be parsed, so blocked namespaces "
            "could not be stripped from it. Re-run without -o json/-o yaml."
        )
    kept = [
        item for item in doc["items"]
        if not (isinstance(item, dict)
                and str((item.get("metadata") or {}).get("name", "")).lower() in blocked)
    ]
    dropped = len(doc["items"]) - len(kept)
    doc["items"] = kept
    annotate_withheld(doc, dropped, "namespace")
    return (
        json.dumps(doc, indent=2) if out_format == "json"
        else yaml.safe_dump(doc, default_flow_style=False, sort_keys=False)
    )


def _filter_described_namespaces(output: str, blocked: frozenset[str]) -> str:
    """Drop the blocks of a `kubectl describe namespaces` output that describe blocked ones.

    `describe` was not filtered at all: `_filter_namespace_output` passed a hardcoded `"get"` to
    `_extract_resource_type`, so for any other verb the resource came back None and the whole
    filter was skipped. `kubectl describe namespaces` therefore returned the labels, annotations,
    quotas and limit ranges of every namespace the listing was supposed to hide.

    Each entry starts with `Name:` at column zero; every nested line is indented, so that is a
    reliable block boundary.
    """
    blocks: list[list[str]] = []
    for line in output.splitlines(keepends=True):
        if line.startswith("Name:") or not blocks:
            blocks.append([])
        blocks[-1].append(line)
    kept = []
    for block in blocks:
        head = block[0]
        name = head.split(":", 1)[1].strip().lower() if head.startswith("Name:") else ""
        if name not in blocked:
            kept.append("".join(block))
    return _noting_namespaces("".join(kept), len(blocks) - len(kept))


def _filter_namespace_output(verb: str, args: list[str], output: str) -> str:
    """Strip blocked namespaces from a namespace listing, in every output format.

    A bare `kubectl get namespaces` is deliberately allowed — an agent has to be able to see the
    shape of the cluster — and the blocked ones are removed from the answer instead. That promise
    only held for three of the six formats until 2026-08-20.
    """
    if _extract_resource_type(verb, args) not in ("namespaces", "namespace", "ns"):
        return output

    blocked = settings.kubectl_blocked_namespaces
    out_format = _flag_value(args, "-o", "--output", verb)

    if verb == "describe":
        return _filter_described_namespaces(output, blocked)

    lines = output.splitlines(keepends=True)

    if out_format == "name":
        kept = [l for l in lines if l.strip().split("/")[-1].lower() not in blocked]
        return _noting_namespaces("".join(kept), len(lines) - len(kept))
    if out_format in ("json", "yaml"):
        return _filter_structured_namespaces(output, out_format, blocked)
    if (out_format or "") not in _FIXED_SHAPE_FORMATS:
        # `jsonpath` used to be filtered here by splitting the output on whitespace and dropping
        # tokens equal to a blocked name. That works for exactly one jsonpath — the one whose
        # output is bare names separated by spaces. Measured 2026-08-20, three ordinary ones
        # defeated it and returned every protected namespace **with no withheld note**, because
        # the name was no longer a whole token:
        #     {range .items[*]}{.metadata.name}{","}{end}      -> default,kube-system,monitoring,
        #     {range .items[*]}{.metadata.name}{"="}{.status.phase}{"\n"}{end}
        #     {range .items[*]}{.metadata.name}{":"}{.status.phase}{"\n"}{end}
        # jsonpath renders whatever the caller asked for, exactly like `custom-columns` and
        # `go-template` — which this same function already refuses, and which the
        # `--all-namespaces` sibling refuses for jsonpath too. One rule for all three.
        return _unfilterable_format_message(out_format or "", "a namespace listing")
    # default table: keep header + rows whose first column isn't blocked
    result, dropped = [], 0
    for line in lines:
        parts = line.split()
        if not parts or parts[0] == "NAME" or parts[0].lower() not in blocked:
            result.append(line)
        else:
            dropped += 1
    return _noting_namespaces("".join(result), dropped)


# `-f`/`-k` name a manifest KubeIntellect never sees. `-f -` is the supported form: the YAML
# arrives as the tool's `stdin` argument, is validated, is read by the protected-access checks,
# and is shown to the human in the approval prompt. A path or a URL is none of those things.
_MANIFEST_SOURCE_FLAGS = frozenset({"-f", "--filename", "-k", "--kustomize"})
# `-f` means `--follow` here, not `--filename`. Everywhere else it is a manifest source.
_FOLLOW_VERBS = frozenset({"logs"})
# Verbs whose target comes after a subcommand (`kubectl rollout restart deploy/api`).
_SUBCOMMAND_VERBS = frozenset({"rollout"})

# Size cap for one captured pre-state. Anything larger is truncated, and a truncated object is
# not a restore point — the capture records that rather than letting it pass as one.
_ROLLBACK_MAX_CHARS = 4000


def _external_manifest_source(verb: str, args: list[str]) -> str | None:
    """The value of a `-f`/`-k` that is not stdin, or None if every manifest source is `-`.

    Accepts every spelling pflag does — `-f x`, `-f=x`, `-fx`, `--filename x`, `--filename=x` —
    for the same reason `_extract_namespace` has to: a guard that reads one spelling of a flag
    guards one spelling of a command.
    """
    i = 1
    while i < len(args):
        tok = args[i]
        name, value = tok, None
        if tok.startswith("-") and "=" in tok:
            name, value = tok.split("=", 1)
        elif not tok.startswith("--") and tok[:2] in ("-f", "-k") and len(tok) > 2:
            name, value = tok[:2], tok[2:]
        if name in _MANIFEST_SOURCE_FLAGS and not (name in ("-f", "--filename")
                                                   and verb in _FOLLOW_VERBS):
            if value is None:
                value = args[i + 1] if i + 1 < len(args) else ""
                i += 1
            if value != "-":
                return value
        i += 1
    return None


def _manifest_docs(stdin: str | None) -> list[dict]:
    """Every mapping document in a stdin manifest, including the items of a `kind: List`.

    `kubectl apply -f -` names no resource and no namespace on the command line — both live in
    the YAML. Everything that reads the *command* is therefore blind to what the command
    actually targets, which is why the two functions below exist.

    Parsing is best-effort by design: `_validate_stdin_yaml` has already rejected unparseable
    stdin by the time this runs, so an exception here cannot be reached in the normal flow, and
    returning `[]` for a document shape we do not understand only means the argv-based checks
    still apply.
    """
    if not stdin:
        return []
    try:
        docs = list(yaml.safe_load_all(stdin))
    except yaml.YAMLError:
        return []
    out: list[dict] = []
    stack = [d for d in docs if isinstance(d, dict)]
    while stack:
        doc = stack.pop()
        out.append(doc)
        items = doc.get("items")
        if isinstance(items, list):
            stack.extend(i for i in items if isinstance(i, dict))
    return out


def _manifest_kinds(stdin: str | None) -> set[str]:
    """The `kind:` of every document in a stdin manifest."""
    return {str(d["kind"]) for d in _manifest_docs(stdin) if d.get("kind")}


def _manifest_namespaces(stdin: str | None) -> set[str]:
    """Every `metadata.namespace` named in a stdin manifest.

    Deliberately does **not** walk into a Pod spec's `volumes[].secret` or `envFrom` — mounting
    a Secret is what a Pod is for, and blocking that would break ordinary deployments. The
    boundary is what the manifest *is* and *where it goes*, which is exactly what the argv
    checks cover for the non-manifest forms of the same command.
    """
    out: set[str] = set()
    for doc in _manifest_docs(stdin):
        meta = doc.get("metadata")
        if isinstance(meta, dict) and meta.get("namespace"):
            out.add(str(meta["namespace"]).lower())
    return out


def _blocked_resource_hit(verb: str, args: list[str], stdin: str | None = None) -> str | None:
    """The protected resource this command touches, named on the command line or in a manifest."""
    blocked = _blocked_resources()
    resource = _extract_resource_type(verb, args)
    if _resource_spellings(resource) & blocked:
        return resource
    for kind in sorted(_manifest_kinds(stdin)):
        if _resource_spellings(kind) & blocked:
            return kind
    return None


def _is_all_namespaces(args: list[str]) -> bool:
    """True if the command targets every namespace at once.

    `-A` is boolean, so kubectl accepts the bare flag and the explicit `=true`/`=false` forms.
    An explicit `false` is not a cluster-wide request.
    """
    for arg in args:
        if arg in ("-A", "--all-namespaces"):
            return True
        if arg.startswith("--all-namespaces="):
            return arg.split("=", 1)[1].strip().lower() not in ("false", "0", "no")
    return False


def _all_namespaces_hit(verb: str, args: list[str]) -> str | None:
    """Refuse a *mutation* that targets every namespace at once.

    The protected-namespace check asks "which namespace does this command name?" — and a
    command naming *all* of them names none in particular, so until 2026-08-20 the check
    simply did not fire. Measured: `kubectl delete pods -n kube-system` was refused outright
    while `kubectl delete pods --all-namespaces` reached the approval prompt and, once
    approved, would have deleted pods in `kube-system`, `monitoring` and `kubeintellect` —
    the product's own namespace.

    Reads are not refused here: `kubectl get pods -A` is how an agent sees the shape of a
    cluster. Their *output* is filtered instead, by `_filter_all_namespaces_output`.
    """
    if not _is_all_namespaces(args) or not _is_write_verb(verb, args):
        return None
    return (
        "[Protected] A cluster-wide mutation (--all-namespaces) is not permitted: it would "
        "reach infrastructure namespaces that are individually blocked. Narrow it with "
        "-n <namespace>."
    )


def _filter_all_namespaces_output(verb: str, args: list[str], output: str) -> str:
    """Drop rows belonging to blocked namespaces from a cluster-wide listing.

    `kubectl get pods -n kube-system` is refused, so `kubectl get pods -A` must not return the
    same rows. Every cluster-wide output carries the namespace — as the first table column, as
    `metadata.namespace`, or as a `Namespace:` field — except `-o name` and `-o jsonpath`,
    whose shape is chosen by the caller and carries no namespace at all. Those two are refused
    rather than passed through unfiltered, which is the same fail-closed choice the structured
    namespace filter makes on a payload it cannot parse.
    """
    if not _is_all_namespaces(args):
        return output

    blocked = settings.kubectl_blocked_namespaces
    out_format = _flag_value(args, "-o", "--output", verb)

    if out_format == "name" or (out_format and "jsonpath" in out_format):
        return (
            f"[Protected] '-o {out_format}' with --all-namespaces carries no namespace, so "
            "results from infrastructure namespaces cannot be separated out. Re-run without "
            "-o (the table lists NAMESPACE), or narrow the query with -n <namespace>."
        )
    if out_format in ("json", "yaml"):
        return _filter_structured_by_namespace(output, out_format, blocked)
    if verb == "describe":
        return _filter_described_by_namespace(output, blocked)
    if (out_format or "") not in _FIXED_SHAPE_FORMATS:
        return _unfilterable_format_message(out_format or "", "a cluster-wide listing")

    # Default table and -o wide: --all-namespaces prepends a NAMESPACE column. The row filter
    # itself lives in `namespace_guard` because `context_fetcher` needs the same one.
    result, dropped = drop_blocked_table_rows(output)
    return result + _withheld_note(dropped) if dropped else result


def _filter_structured_by_namespace(output: str, out_format: str, blocked: frozenset[str]) -> str:
    """Filter a json/yaml listing by each item's metadata.namespace. Fails closed."""
    try:
        doc = json.loads(output) if out_format == "json" else yaml.safe_load(output)
    except (json.JSONDecodeError, yaml.YAMLError):
        return (
            "[Protected] The cluster-wide result could not be parsed, so results from "
            "infrastructure namespaces could not be separated out. Narrow the query with "
            "-n <namespace>."
        )
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        # Hardening, not a measured leak: `get -A` always renders a List, so this branch has no
        # reproduction. It used to `return output` — the one shape in this function that was not
        # fail-closed, which is the same "everything else must be safe" assumption that made
        # `-o custom-columns` a bypass one branch above.
        return (
            "[Protected] The cluster-wide result was not a list of items, so results from "
            "infrastructure namespaces could not be separated out. Narrow the query with "
            "-n <namespace>."
        )

    items = doc.get("items") or []
    kept = [
        i for i in items
        if str((i.get("metadata") or {}).get("namespace", "")).lower() not in blocked
    ]
    dropped = len(items) - len(kept)
    if not dropped:
        return output
    doc["items"] = kept
    # The notice goes *in* the document. Appending it after `json.dumps` produced output that
    # `json.loads` rejects with "Extra data" — the one filter that told the truth broke the
    # format it was telling it in.
    annotate_withheld(doc, dropped)
    return (
        json.dumps(doc, indent=2) if out_format == "json"
        else yaml.safe_dump(doc, default_flow_style=False)
    )


def _filter_described_by_namespace(output: str, blocked: frozenset[str]) -> str:
    """Drop whole `describe` blocks whose `Namespace:` field is protected."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in output.splitlines(keepends=True):
        if line.startswith("Name:") and current:
            blocks.append(current)
            current = []
        current.append(line)
    if current:
        blocks.append(current)

    kept, dropped = [], 0
    for block in blocks:
        ns = ""
        for line in block:
            if line.startswith("Namespace:"):
                ns = line.split(":", 1)[1].strip().lower()
                break
        if ns and ns in blocked:
            dropped += 1
            continue
        kept.extend(block)
    result = "".join(kept)
    return result + _withheld_note(dropped) if dropped else result


# One wording for every filter in the project, kubectl and observability alike. This was a
# byte-identical private copy of `namespace_guard.withheld_note`; two copies of the sentence
# that tells the caller its answer is incomplete is one copy too many.
_withheld_note = withheld_note


def _check_protected_access(verb: str, args: list[str], stdin: str | None = None) -> str | None:
    """
    Return an error string if the command targets a protected namespace or
    resource type. Returns None if the command is allowed to proceed.

    All operations on infrastructure namespaces are blocked, including reads.
    Secrets and serviceaccounts are blocked regardless of namespace.

    Both checks read the **manifest as well as the command line**. `kubectl apply -f -` puts the
    resource in `kind:` and the namespace in `metadata.namespace:`, so a check that only parses
    argv sees neither. Measured 2026-08-20: `kubectl apply -f -` with a Pod whose
    `metadata.namespace` was `kube-system` reached only an approval prompt, while the identical
    intent written `kubectl apply -f - -n kube-system` was refused outright — and the tool's own
    `_REJECTED_VERBS` message *recommends* the stdin form as the way to do this.
    """
    hit = _blocked_resource_hit(verb, args, stdin)
    # Secrets and serviceaccounts are fully blocked regardless of verb — they
    # would expose credentials and tokens even to read-only viewers.
    if hit:
        return (
            f"[Protected] Access to '{hit}' is not permitted through KubeIntellect. "
            "Kubernetes Secrets and ServiceAccount tokens are shielded from inspection "
            "to protect cluster credentials."
        )

    # All operations on infrastructure namespaces are blocked — whether the namespace is named
    # with -n, as the command's positional target (`kubectl delete ns kube-system`), or in a
    # manifest's `metadata.namespace`.
    all_ns_err = _all_namespaces_hit(verb, args)
    if all_ns_err:
        return all_ns_err

    blocked = settings.kubectl_blocked_namespaces
    candidates = [_extract_namespace(args, verb), *_targeted_namespaces(verb, args)]
    candidates.extend(sorted(_manifest_namespaces(stdin)))
    for ns in candidates:
        # Case-folded on both sides: the blocklist is folded in `config.py`, and the value here
        # comes from an LLM-written command line or a manifest, not from a validated API object.
        if ns and ns.strip().lower() in blocked:
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


def _capture_note(before: str, after: str, cap: int) -> str:
    """Say what redaction/truncation did to one captured object, in an operator's terms."""
    from app.utils.redact import count_redactions

    if after.endswith("[...]"):
        return f"truncated at {cap} chars (object is {len(before)} chars)"
    # `count_redactions` owns the marker vocabulary. This function used to carry its own
    # copy of three of the six, so a capture whose only redaction was a PEM private key or a
    # Secret's `data:` block reported, in full, "redacted" — least said where most was taken.
    dropped, marks = count_redactions(after)
    parts = []
    if dropped:
        parts.append(f"{dropped} line(s) dropped")
    if marks:
        parts.append(f"{marks} value(s) replaced")
    return "redacted: " + ", ".join(parts) if parts else "redacted"


def _capture_rollback_point(verb: str, args: list, stdin: str | None, config, env: dict) -> None:
    """Best-effort pre-state capture for the Safety Sandwich (never raises).

    Records a `rollback_point` into the flight recorder: the rollback_id, the mutating command,
    and the current YAML of the targeted objects.

    ⚠️ **A capture is evidence; it is a restore point only when `restorable` is true.** The YAML
    is redacted (it lands in a database) and capped, and both transformations can leave something
    that must not be piped into `kubectl apply -f -`. Measured against real kubectl output
    (bitnami/kubectl:latest, 2026-08-20):

    * a Secret loses its `kind:` line — the word "secret" is a redaction keyword — so kubectl
      answers ``Object 'Kind' is missing`` and nothing can be restored;
    * a ConfigMap whose values are token-shaped stays **valid and applies cleanly**, having had
      every value replaced with ``<redacted-token>`` — a successful restore that destroys the
      configuration it was supposed to protect;
    * anything over the cap (the project's own chart values.yaml is 7.4 KB) is cut mid-line and
      no longer parses.

    So each capture is compared with what kubectl actually produced, and the record says which
    of the two it is. Redaction is not negotiable — the alternative is credentials in Postgres —
    but claiming restorability that is not there is.
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
            # `rollout` carries a subcommand before its target, so dropping only the verb leaves
            # `kubectl get restart deployment/api -o yaml` — a command kubectl rejects, which the
            # best-effort wrapper would swallow, arming nothing while appearing to arm something.
            if verb in _SUBCOMMAND_VERBS and tail:
                sub_at = _skip_flags(["kubectl", *tail], 1) - 1
                if 0 <= sub_at < len(tail):
                    del tail[sub_at]
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
                # `kubectl label pod api-1 tier=web` ends in key=value pairs, which are not
                # resource names — a Kubernetes name cannot contain `=`. Left in, they produced
                # `kubectl get pod api-1 tier=web -o yaml`, which kubectl rejects, so the
                # best-effort wrapper swallowed the error and armed nothing.
                if "=" in token and "/" not in token:
                    continue
                keep.append(token)
            if keep:
                targets.append(["kubectl", "get", *keep, "-o", "yaml"])

        if not targets:
            return
        states = []
        notes: list[str] = []
        restorable = True
        for target in targets[:5]:
            try:
                pre = subprocess.run(
                    target, capture_output=True, text=True, timeout=5, env=env, shell=False
                )
                if pre.returncode == 0 and pre.stdout:
                    kept = redact_secrets(pre.stdout, max_chars=_ROLLBACK_MAX_CHARS)
                    # `redact_secrets` drops the final newline; that alone is not a change to
                    # the object, so it must not cost a capture its restorable flag.
                    if kept.rstrip("\n") != pre.stdout.rstrip("\n"):
                        restorable = False
                        notes.append(f"{' '.join(target[2:4])}: "
                                     f"{_capture_note(pre.stdout, kept, _ROLLBACK_MAX_CHARS)}")
                    states.append(kept)
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
            "restorable": restorable,
            "capture_notes": notes,
            "session_id": session_id,
        })
        if restorable:
            logger.info(f"rollback_point_armed id={rollback_id} targets={len(states)}")
        else:
            logger.warning(
                f"rollback_point_recorded id={rollback_id} targets={len(states)} — "
                f"NOT restorable, do not apply it: {'; '.join(notes)}"
            )
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
                "The jsonpath expression appears to be truncated — use -o json "
                "instead (-o custom-columns cannot be filtered by namespace)."
            ) from exc
        raise ValueError(f"Could not parse command: {exc}") from exc

    # ── 3b. Connection and identity are the deployment's, not the caller's ───
    conn_flag = _connection_flag_in(args)
    authorised = _authorised_identity(config)
    if conn_flag and not (authorised and _identity_is_authorised(args, authorised)):
        logger.warning(f"run_kubectl: refused connection/identity override {conn_flag!r}: {cmd!r}")
        return (
            f"[Protected] '{conn_flag}' is not permitted. Which cluster this connects to, and "
            "the identity it uses, are fixed by the deployment — they are not part of a query. "
            "Ask the question without it."
        )

    verb = _extract_verb(args)

    # ── 4. Rejected verbs (non-interactive, always fail in container) ─────────
    if verb in _REJECTED_VERBS:
        return (
            f"[Unsupported] 'kubectl {verb}' requires an interactive terminal which is "
            "not available. Use 'kubectl patch' or 'kubectl apply -f -' with stdin instead."
        )

    if _operand_after_verb(args) in _REJECTED_SUBCOMMANDS.get(verb, set()):
        return (
            f"[Protected] 'kubectl {verb} {_operand_after_verb(args)}' returns the contents of "
            "every namespace in one payload, including the infrastructure namespaces this "
            "deployment withholds, and it has no per-object shape that can be filtered. "
            "Query the namespace you need with -n <namespace>."
        )

    # ── 4a. Role check ────────────────────────────────────────────────────────
    # readonly   : all writes blocked
    # operator   : medium-risk allowed (HITL-gated); high-risk blocked
    # admin      : everything allowed (HITL-gated); infra namespaces blocked, reads included
    # superadmin : everything allowed (HITL-gated); no namespace restrictions at all
    #              (reads of infra namespaces included) - but never the resource block
    user_role = "admin"
    if config:
        user_role = (config.get("configurable") or {}).get("user_role", "admin")

    # Both checks consider any destructive verb present, not only the parsed one. If a command
    # shape ever slips past `_extract_verb` again, it fails closed here instead of executing
    # with the privileges of a verb we did not recognise.
    present = _destructive_verbs_in(args)
    # "read-only" is defined by what the key may *do*, so the test is "is this a write", not
    # "is this verb on a list of writes we remembered to enumerate".
    if user_role == "readonly" and (verb in DESTRUCTIVE_VERBS or present or _is_write_verb(verb, args)):
        denied = verb if (verb in DESTRUCTIVE_VERBS or _is_write_verb(verb, args)) else sorted(present)[0]
        return (
            f"[Permission Denied] Your API key has read-only access. "
            f"The '{denied}' operation requires an operator or admin API key."
        )
    high = present & _HIGH_RISK
    if user_role == "operator" and (verb in _HIGH_RISK or high):
        denied = verb if verb in _HIGH_RISK else sorted(high)[0]
        return (
            f"[Permission Denied] Your API key has operator access. "
            f"The '{denied}' operation requires an admin API key."
        )

    # ── 4a2. Manifest sources KubeIntellect cannot read ───────────────────────
    # `kubectl apply -f https://…/m.yaml` and `-f /path/x.yaml` put the manifest somewhere the
    # gates cannot reach. Measured 2026-08-20: both ran, the protected-resource and
    # protected-namespace checks saw an empty command, and the approval prompt showed the human
    # `stdin: null` with a summary that was just the command line — there was nothing to review,
    # because kubectl fetches the content *after* approval, from inside the cluster. The URL form
    # is also unreviewed egress from the pod. `-f -` with stdin is the supported form and is what
    # the `kubectl edit` rejection message already tells users to use.
    external = _external_manifest_source(verb, args)
    if external is not None:
        return (
            f"[Unsupported] Reading a manifest from {external!r} is not permitted — KubeIntellect "
            "cannot inspect it, so the protected-namespace and protected-resource checks would be "
            "skipped and the approval prompt would show you nothing to review. Use "
            f"'kubectl {verb} -f -' and pass the YAML as stdin instead."
        )

    # ── 4b. Protected namespace / resource check ──────────────────────────────
    # Runs before HITL so users never even get an approval prompt for
    # commands that would expose internal credentials.
    # superadmin bypasses the namespace block ENTIRELY - for any verb, so reads of
    # infrastructure namespaces too, not only writes - but never the resource block
    # (secrets/serviceaccounts remain shielded for all roles).
    protected_err = _check_protected_access(verb, args, stdin)
    if protected_err and user_role == "superadmin":
        # Re-run the check considering only the resource block (not ns block). This must use the
        # same manifest-aware helper as the check itself — a re-check that reads only argv would
        # hand superadmin back the very bypass the check just closed.
        if not _blocked_resource_hit(verb, args, stdin):
            protected_err = None
    if protected_err:
        logger.warning(f"run_kubectl: blocked protected access: {cmd!r}")
        return protected_err

    # ── 4c. Risk classification → HITL interrupt ─────────────────────────────
    if verb in DESTRUCTIVE_VERBS or _destructive_verbs_in(args) or _is_write_verb(verb, args):
        has_dry_run = any(
            flag in args for flag in ("--dry-run=client", "--dry-run=server", "--dry-run")
        )
        hitl_bypass = bool((config.get("configurable") or {}).get("hitl_bypass", False)) if config else False
        always_confirm = _requires_always_confirm(verb, args)
        if not has_dry_run and (not hitl_bypass or always_confirm):
            hidden = sorted(_destructive_verbs_in(args))
            effective = verb if (verb in DESTRUCTIVE_VERBS or not hidden) else hidden[0]
            risk = "high" if always_confirm else _classify_risk(effective, args)
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
    # "Before every mutating kubectl command" is what flight-recorder.md promises, so the test
    # has to be the same one the HITL gate uses. This was `verb in _HIGH_RISK or verb in
    # _MEDIUM_RISK` — the pre-2026-08-20 deny-list — so `kubectl rollout restart|undo|pause`,
    # the ordinary way to restart a workload, and any verb this build does not know armed no
    # rollback point at all while the docs said every mutation did.
    if _is_write_verb(verb, args):
        if not has_dry_run:
            _capture_rollback_point(verb, args, stdin, config, env)

    logger.debug(f"run_kubectl: {cmd}")

    timeout = (
        settings.KUBECTL_DESTRUCTIVE_TIMEOUT_SECONDS
        if _classify_risk(verb, args) == "high"
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

    stdout, stderr = proc.stdout or "", proc.stderr or ""
    # Whether *kubectl* printed anything, decided before the emulator runs: `_apply_pipes` turns
    # an empty string into "(no matching lines)", and reporting that as output kubectl produced
    # would be a claim about the cluster made by our own grep.
    kubectl_printed = bool(stdout)
    logger.debug(
        f"run_kubectl: exit={proc.returncode} stdout_len={len(stdout)} "
        f"stderr_len={len(stderr)} cmd={cmd}"
    )

    # ── 6. Pipe emulation (grep) ─────────────────────────────────────────────
    #
    # Applies to STDOUT ONLY, which is what a real shell does — `kubectl … | grep x` never feeds
    # kubectl's stderr to grep. This used to run over the merged stdout-or-stderr string, so a
    # command that FAILED had its error grepped away: measured 2026-08-24, an RBAC `Forbidden`
    # piped through `grep Running` returned `(no matching lines)` — byte-for-byte the same answer
    # a successful listing with nothing running gives. The agent reads that as evidence about the
    # cluster when it was never allowed to look, and the error hint added just above it (which
    # correctly identified the denial) went with it.
    if pipe_segments:
        stdout = _apply_pipes(stdout, pipe_segments)

    # ── 6b. Strip blocked namespaces from namespace listings ─────────────────
    # Listing filters parse a listing, so they see stdout only — never an error message.
    stdout = _filter_namespace_output(verb, args, stdout)
    stdout = _filter_all_namespaces_output(verb, args, stdout)

    # ── 6c. Compose the answer ───────────────────────────────────────────────
    #
    # A non-zero exit is stated, not implied. `output = stdout or stderr` dropped stderr whenever
    # stdout had anything at all, so a partial failure — `kubectl get pods -A` where one namespace
    # is forbidden — returned a complete-looking listing with no sign a namespace was denied.
    if proc.returncode != 0 and not _exit_is_an_answer(verb, args, proc.returncode):
        detail = stderr.strip() or "(kubectl wrote nothing to stderr)"
        # Append a single-line hint when the stderr matches a known pattern. The original error
        # is preserved verbatim.
        if settings.KUBECTL_ERROR_HINTS_ENABLED:
            detail, pattern_name = kubectl_errors.annotate(detail)
            if pattern_name:
                logger.info(
                    "kubectl_error_interpreted "
                    f"pattern={pattern_name} exit_code={proc.returncode} cmd={cmd!r}",
                    extra={"pattern": pattern_name, "exit_code": proc.returncode},
                )
        output = f"[kubectl exited {proc.returncode}] {detail}"
        if kubectl_printed and stdout.strip():
            output += (
                f"\n\nkubectl also produced this output before/alongside the error — it may be "
                f"partial, and absence from it is NOT evidence:\n{stdout}"
            )
    else:
        output = stdout or stderr or "(no output)"

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
