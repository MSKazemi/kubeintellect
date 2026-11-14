"""
run_kubectl — the single execution surface for all Kubernetes operations.

Safety layers (in order):
  0. Placeholder guard           — reject unresolved planning notation (`<pod>`, `{ns}`, `POD_NAME`)
  1. Shell injection prevention  — reject dangerous shell metacharacters
  2. YAML pre-validation         — validate stdin YAML before touching cluster
  3. Risk classification         — destructive verbs trigger LangGraph interrupt
  4. subprocess with shell=False — no shell interpolation
  5. Pipe emulation              — | grep handled in Python (no shell needed)
  6. Namespace output filter     — strip blocked namespaces from get-namespaces output
  7. Output cap                  — truncate at 8 000 chars
"""
from __future__ import annotations

import hashlib
import os
import re
import json
import shlex
import subprocess
import threading
from collections import OrderedDict, deque
from typing import Annotated

import yaml
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool
from langgraph.types import interrupt

from app.core.config import settings
from app.tools import kubectl_errors
from app.tools.output_policy import mark_unavailable, unavailable_notice
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

# ── Unresolved placeholder guard (#173) ───────────────────────────────────────
# The coordinator has emitted planning notation as a real argument — `kubectl describe node
# <node-name>` batched alongside the `get nodes` that would have named the node. The `<`/`>`
# form is already refused by `_SHELL_METACHAR`, but as "disallowed shell characters", which
# tells the model nothing about what it did wrong; `{namespace}` and `POD_NAME` are refused by
# nothing and reach kubectl, which fails with a NotFound the model reads as evidence. This guard
# only *narrows* what executes: it never admits a command another gate refuses.
#
# Shapes, matched outside the excluded spans below:
#   angle   `<pod-name>`, `pod/<pod>`        — scanned in quoted text too (it is refused anyway)
#   dollar  `$NODE`, `${NODE}`               — likewise
#   brace   `{namespace}`, `{{pod}}`         — unquoted text only; the inside is a bare name, so
#                                              jsonpath `{.items[*]}` and JSON never match
#   caps    `POD_NAME`, `YOUR_NAMESPACE`     — only as a positional operand (or its `kind/NAME`
#                                              suffix) or a `-n`/`-c` value: object, namespace
#                                              and container names are lowercase RFC 1123, so
#                                              an UPPER_SNAKE value there can never resolve
# Excluded: the value of `-o`/`--output`/`--template`/`--sort-by` (jsonpath, go-template and
# custom-columns legitimately carry `{name}`-like text, and an unquoted jsonpath with spaces is
# followed until its braces balance); quoted text for the brace and caps shapes (a JSON patch or
# an annotation value); everything after `--` (the container's own command line); and the caps
# shape under `kubectl config`, whose context names are free-form.
_PLACEHOLDER_ANGLE = re.compile(r"<[A-Za-z_][\w.-]*>")
_PLACEHOLDER_DOLLAR = re.compile(r"\$\{?[A-Za-z_]\w*\}?")
_PLACEHOLDER_BRACE = re.compile(r"\{\{?\s*[A-Za-z_][\w-]*\s*\}\}?")
_PLACEHOLDER_CAPS = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
_TEMPLATE_VALUE_FLAGS = ("-o", "--output", "--template", "--sort-by")
_IDENTIFIER_VALUE_FLAGS = ("-n", "--namespace", "-c", "--container")


def _quote_aware_tokens(command: str) -> list[tuple[str, str]] | None:
    """Split on unquoted whitespace into ``(text, unquoted_text)`` pairs, or None if a quote is
    left open. ``text`` drops the quote characters; ``unquoted_text`` also drops what they
    enclosed. `shlex` cannot say which part of a token was quoted, and that is the exclusion."""
    tokens: list[tuple[str, str]] = []
    text: list[str] = []
    bare: list[str] = []
    quote = ""
    started = False
    for ch in command:
        if quote:
            if ch == quote:
                quote = ""
            else:
                text.append(ch)
        elif ch in ("'", '"'):
            quote, started = ch, True
        elif ch.isspace():
            if started:
                tokens.append(("".join(text), "".join(bare)))
            text, bare, started = [], [], False
        else:
            text.append(ch)
            bare.append(ch)
            started = True
    if quote:
        return None
    if started:
        tokens.append(("".join(text), "".join(bare)))
    return tokens


def _template_flag_value(tok: str) -> str | None:
    """The inline value of a template-carrying flag (`-o=X`, `-oX`, `--output=X`), "" when the
    value is the next token, or None when `tok` is not one of those flags."""
    for flag in _TEMPLATE_VALUE_FLAGS:
        if tok == flag:
            return ""
        if tok.startswith(flag + "="):
            return tok[len(flag) + 1:]
    if tok.startswith("-o") and not tok.startswith("--") and len(tok) > 2:
        return tok[2:]
    return None


def _unresolved_placeholder_in(tokens: list[tuple[str, str]]) -> str | None:
    """The first placeholder in a quote-aware token list, or None. See the table above."""
    verb = _extract_verb([t for t, _ in tokens])
    depth = 0           # unbalanced `{` in a template value that spilled across tokens
    skip_value = False  # the previous token was a template flag whose value is this token
    prev = ""
    for text, bare in tokens:
        if skip_value or depth > 0:
            skip_value = False
            depth = max(0, depth + bare.count("{") - bare.count("}"))
            prev = text
            continue
        if text == "--" and bare == "--":
            return None
        # Detected on the full text, so `--template='{{.x}}'` is known to carry its value inline;
        # braces are counted on the unquoted text, so a quoted `{"\n"}` does not unbalance them.
        inline = _template_flag_value(text)
        if inline is not None:
            if inline == "":
                skip_value = True
            else:
                bare_inline = _template_flag_value(bare) or ""
                depth = max(0, bare_inline.count("{") - bare_inline.count("}"))
            prev = text
            continue
        for shape in (_PLACEHOLDER_ANGLE, _PLACEHOLDER_DOLLAR):
            hit = shape.search(text)
            if hit:
                return hit.group(0)
        hit = _PLACEHOLDER_BRACE.search(bare)
        if hit:
            return hit.group(0)
        if verb != "config" and text == bare:
            candidate: str | None = None
            if prev in _IDENTIFIER_VALUE_FLAGS:
                candidate = text
            elif "=" in text and text.split("=", 1)[0] in _IDENTIFIER_VALUE_FLAGS:
                candidate = text.split("=", 1)[1]
            elif not text.startswith("-") and "=" not in text and (
                not prev.startswith("-") or "=" in prev
            ):
                candidate = text.rsplit("/", 1)[-1]
            if candidate and _PLACEHOLDER_CAPS.fullmatch(candidate):
                return candidate
        prev = text
    return None


def _unresolved_placeholder(command: str) -> str | None:
    """The first unresolved placeholder in a kubectl command line, or None.

    An unparseable line (an open quote) returns None: the parser after this reports it.
    """
    tokens = _quote_aware_tokens(command)
    return _unresolved_placeholder_in(tokens) if tokens else None


def _unresolved_placeholder_in_args(args: list[str]) -> str | None:
    """Same rule for an already-split argv (no quoting left to honour), `kubectl` optional."""
    argv = args if args and args[0] == "kubectl" else ["kubectl", *args]
    return _unresolved_placeholder_in([(a, a) for a in argv])


def _placeholder_message(placeholder: str, command: str) -> str:
    return (
        f"Command contains an unresolved placeholder {placeholder!r}: {command!r}. "
        "That is planning notation, not a real name, so kubectl was not run. Resolve the "
        "actual identifier first — take it from the cluster snapshot or an earlier result, or "
        "list the resource (for example `kubectl get nodes -o wide` or `kubectl get pods -A`) "
        "and wait for that result — then call run_kubectl again with the concrete name. Do not "
        "guess a name."
    )


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


#: The container fields that `set image` / `set resources` change. Matched as whole field names (or whole
#: JSON-patch path segments), never as substrings: `imagePullPolicy` and `imagePullSecrets` are other fields.
# `command` and `args` added 2026-11-14: replacing a container's program is the same kind of change as replacing its
# image. Measured: an unattended session tried to patch a failing Job's command to print success (r4), and another
# made a stub Job that did (r1, r3) and reported the job fixed.
_GATED_CONTAINER_FIELDS = frozenset({"image", "resources", "command", "args"})

_GATED_FIELD_WORD = re.compile(
    r"(?<![A-Za-z0-9_])(?:image|resources)(?![A-Za-z0-9_])"
    # `command`/`args` only as a key (quoted or YAML), so a ConfigMap key `startup-command` is not mistaken for a
    # container's program (independent review F13); JSON-patch paths are checked by the parsed walk.
    r"|(?<![A-Za-z0-9_-])[\"']?(?:command|args)[\"']?\s*:"
)

#: Kinds whose manifest carries a pod template or pod spec, i.e. the ones where `image` / `resources` are workload fields.
_POD_BEARING_KINDS = frozenset({
    "pod", "deployment", "statefulset", "daemonset", "replicaset", "replicationcontroller", "job", "cronjob",
})


def _touches_gated_field(node: object) -> bool:
    """True if a parsed patch/manifest names an `image` or `resources` field anywhere.

    Handles all three payload shapes through one walk: a merge/strategic patch (nested mappings), a manifest, and an
    RFC 6902 JSON patch (a list of `{op, path, value}`), where the field is a *path segment* rather than a key.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _GATED_CONTAINER_FIELDS:
                return True
            if key in ("path", "from") and isinstance(value, str) and _GATED_CONTAINER_FIELDS & set(value.split("/")):
                return True
            if _touches_gated_field(value):
                return True
    elif isinstance(node, list):
        return any(_touches_gated_field(item) for item in node)
    return False


def _patch_payload(args: list[str]) -> tuple[bool, str | None]:
    """(readable, payload) for a `kubectl patch`. Unreadable means the patch comes from a file or names no payload.

    The tool cannot open a file on the server and a patch with no payload is malformed, so both are reported as
    unreadable and the caller fails closed.
    """
    for i, tok in enumerate(args):
        if tok in ("-p", "--patch"):
            return (True, args[i + 1]) if i + 1 < len(args) else (False, None)
        if tok.startswith("--patch="):
            return True, tok.split("=", 1)[1]
        if tok.startswith("-p=") or (tok.startswith("-p") and len(tok) > 2 and tok[2] in "{[\"'"):
            return True, tok[3:] if tok.startswith("-p=") else tok[2:]
        if tok == "--patch-file" or tok.startswith("--patch-file="):
            return False, None
    return False, None


def _changes_container_image_or_resources(verb: str, args: list[str], stdin: str | None) -> bool:
    """True when a `patch` / `apply` / `replace` changes a workload's container `image` or `resources`.

    `set image` and `set resources` are always-confirm, but the same change spelled as `patch` or `apply` was not:
    measured 2026-11-12 with a real model, a JSON patch replacing `/spec/template/spec/containers/0/image` ran under
    auto-approve with no prompt while `kubectl set image` for the same field stopped and asked. The gate keyed on the
    spelling, not the effect. Fail-closed where the payload cannot be read.

    NOT covered here, deliberately: `rollout undo` (restores an earlier revision, which can change the image); `edit`
    needs an editor and cannot run through this tool. `create` and `run` of a workload are always-confirm through
    `_creates_a_workload` instead: a new workload is a substitute for the one under investigation, whatever it runs.
    """
    if verb == "patch":
        readable, payload = _patch_payload(args)
        if not readable or payload is None:
            return True
        # Text backstop, independent of any parser: PyYAML and kubectl's own YAML reader can disagree about a
        # malformed flow mapping (`{image:b:1}` is ONE key to PyYAML), and a gate that trusts one parser's view is
        # only as strong as the two parsers' agreement. Whole-word match, so `imagePullPolicy` does not trip it.
        if _GATED_FIELD_WORD.search(payload):
            return True
        # The parse below catches what the text cannot: JSON escapes such as "image" decode to `image`.
        try:
            parsed = json.loads(payload)
        except ValueError:
            try:
                parsed = yaml.safe_load(payload)
            except yaml.YAMLError:
                return True
        return not isinstance(parsed, (dict, list)) or _touches_gated_field(parsed)
    if verb in ("apply", "replace"):
        return any(
            str(doc.get("kind", "")).lower() in _POD_BEARING_KINDS and _touches_gated_field(doc)
            for doc in _manifest_docs(stdin)
        )
    return False


# `kubectl create <subcommand>` forms that make a workload.
_WORKLOAD_CREATE_SUBCOMMANDS = frozenset({"deployment", "deploy", "job", "cronjob", "cj"})


def _creates_a_workload(verb: str, args: list[str], stdin: str | None) -> bool:
    """True for `run`, `create <workload>` and `create -f -` of a pod-bearing manifest.

    Measured 2026-11-14: in 2 of 4 unattended re-runs a session created `report-new`, a Job whose command printed
    SUCCESS, verified the stub instead of the failing Job, and reported the job fixed. A repair changes the object
    under investigation; a new workload is a substitute for it and needs a human. Supporting objects (a ConfigMap,
    the repair the config faults need) are not workloads and still run unattended.
    """
    if verb in ("run", "debug"):
        # `debug` adds an ephemeral container, or with --copy-to makes a new pod: a substitute either way (review F13).
        return True
    if verb != "create":
        return False
    if _operand_after_verb(args) in _WORKLOAD_CREATE_SUBCOMMANDS:
        return True
    return any(str(doc.get("kind", "")).lower() in _POD_BEARING_KINDS for doc in _manifest_docs(stdin))


def _requires_always_confirm(verb: str, args: list[str], stdin: str | None = None) -> bool:
    """True for actions whose blast radius is too large to auto-approve.

    Always-confirm actions trigger an HITL interrupt even when
    `hitl_bypass=True` (auto-approve session). The user must explicitly
    confirm; there is no way to silently auto-approve them.
    """
    if verb == "drain":
        return True
    if _changes_container_image_or_resources(verb, args, stdin):
        return True
    if _creates_a_workload(verb, args, stdin):
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
    # A delete that names no object but a whole class of them (`--all`, `-A`) has a blast radius the
    # namespace-level rules above only cover when the TARGET is a namespace. Measured 2026-11-12 on a real
    # cluster through this tool: `kubectl delete pods --all -n ns` ran under auto-approve with no prompt and
    # replaced every pod. A named delete, or one scoped by a label selector, is still a deliberate, narrower
    # request and still honours auto-approve; only the unscoped forms are always-confirm.
    if verb == "delete" and _is_unscoped_delete(args):
        return True
    return False


def _is_unscoped_delete(args: list[str]) -> bool:
    """True for `delete --all` / `delete -A`: no object is named and no selector narrows the set."""
    for arg in args:
        if arg == "--all" or arg.lower() == "--all=true":
            return True
    return _is_all_namespaces(args)


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
        # Normalised for the comparison only (kubectl reads `--a_b` as `--a-b`), at every position: a `--` can be a
        # flag's value, so tokens after it may still be kubectl flags (fifth review pass).
        name = tok.split("=", 1)[0]
        canonical = name.replace("_", "-") if name.startswith("--") else name
        if canonical in _CONNECTION_FLAGS or canonical.startswith("--as"):
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
# Fields the API server owns. Every live object carries them, a restore cannot use them (`kubectl apply`
# ignores or rejects them), and the secret redactor treats a UUID as a token: left in, `metadata.uid`
# became `<redacted-token>` and cleared `restorable` for essentially every capture. Measured 2026-11-12 on a
# stock Deployment. The existing fixtures omitted these fields, which is why nothing caught it.
_SERVER_MANAGED_METADATA = ("uid", "resourceVersion", "creationTimestamp", "generation", "selfLink",
                            "managedFields", "ownerReferences")


def _strip_server_managed(text: str) -> str:
    """Drop API-server-owned fields from `kubectl get -o yaml` output.

    Returns the text unchanged when it cannot be parsed as plain mappings, so the worst case is the
    previous behaviour, never a silently altered capture.
    """
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return text
    if not docs or any(not isinstance(d, dict) for d in docs):
        return text

    def scrub(node):
        if isinstance(node, dict):
            meta = node.get("metadata")
            if isinstance(meta, dict):
                for key in _SERVER_MANAGED_METADATA:
                    meta.pop(key, None)
            if "apiVersion" in node and "kind" in node:
                node.pop("status", None)  # observed state: not part of what a restore applies
            for value in node.values():
                scrub(value)
        elif isinstance(node, list):
            for value in node:
                scrub(value)

    for doc in docs:
        scrub(doc)
    return yaml.safe_dump_all(docs, sort_keys=False, default_flow_style=False)


_ROLLBACK_MAX_CHARS = 4000
# How many objects one rollback point will fetch. The cap bounds latency on a large
# `apply -f -`; it was previously an unnamed `[:5]` slice, so objects past it were
# dropped with nothing recording that the capture was partial (#190).
_ROLLBACK_MAX_TARGETS = 5


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


def _stays_within_namespace(verb: str, args: list[str], stdin: str | None, bound: str) -> bool:
    """True only when every namespace the command touches is `bound`, shown by the command itself.

    Fail-closed on purpose. A command that names no namespace runs in kubectl's *default* namespace, a
    cluster-scoped object has none, and `-A` is every namespace: none of those can be shown to be inside
    `bound`, so none of them keeps the bypass. A stdin manifest contributes the namespaces it declares.
    """
    if _is_all_namespaces(args):
        return False
    names = {n.lower() for n in _targeted_namespaces(verb, args)}
    flagged = _extract_namespace(args, verb)
    if flagged:
        names.add(flagged.lower())
    names |= _manifest_namespaces(stdin)
    return bool(names) and names == {bound.lower()}


# Verbs whose `--` introduces a command for a container; for every other verb a bare `--` is refused.
_COMMAND_PASSING_VERBS = frozenset({"exec", "run", "debug", "create", "attach"})

# Reads that a bound session points at its own namespace when the command names none. Writes are deliberately
# absent: a write that names no namespace (or targets a cluster-scoped object) must keep failing the binding check
# in `_stays_within_namespace`, and an injected `-n` would make it look as if it stayed inside.
_BOUND_READ_VERBS = frozenset({"get", "describe", "logs", "top", "events", "wait"})


def _bound_read_namespace(verb: str, args: list[str], config: RunnableConfig | None) -> str | None:
    """The namespace to give a read that names none, in a session bound to one; None to leave it alone.

    Measured 2026-11-14: a session bound to `h3-2` read `kubectl get job report -o yaml`, kubectl looked in its
    default namespace, answered NotFound for a Job that existed in `h3-2`, and the model spent 73 cycles
    "recreating" it. The investigation is about one namespace; a read that does not say otherwise belongs there.
    """
    bound = (config.get("configurable") or {}).get("bound_namespace") if config else None
    if not bound or verb not in _BOUND_READ_VERBS:
        return None
    if verb in DESTRUCTIVE_VERBS or _destructive_verbs_in(args) or _is_write_verb(verb, args):
        return None
    if _is_all_namespaces(args) or _extract_namespace(args, verb):
        return None
    return str(bound)


def _with_namespace(args: list[str], namespace: str) -> list[str]:
    """`args` with `-n namespace` added before any `--` (what follows `--` belongs to the container command)."""
    if "--" in args:
        i = args.index("--")
        return [*args[:i], "-n", namespace, *args[i:]]
    return [*args, "-n", namespace]


# ── Per-session memory of an auto-approved session's writes ─────────────────
# Measured 2026-11-14: an unattended session sent the same rejected `kubectl apply` 73 times and nothing noticed. Kept
# per `thread_id` for auto-approved sessions only (a chat has a human approving every write, who may retry after
# fixing something outside the session). Bounded so a long-running server cannot grow it without limit.
_GUARD_MAX_SESSIONS = 512
_guard_lock = threading.Lock()
_guard: OrderedDict[str, dict] = OrderedDict()


def reset_session_guard() -> None:
    """Test helper: forget every session's write history."""
    with _guard_lock:
        _guard.clear()


def _guard_session(thread_id: str) -> dict:
    record = _guard.get(thread_id)
    if record is None:
        record = {"failed": {}, "writes": 0, "executed": 0, "history": deque(maxlen=8), "stuck": False,
                  "ledger": deque(maxlen=12)}
        _guard[thread_id] = record
        while len(_guard) > _GUARD_MAX_SESSIONS:
            _guard.popitem(last=False)
    _guard.move_to_end(thread_id)
    return record


_DRY_RUN_FLAGS = frozenset({"--dry-run", "--dry-run=client", "--dry-run=server"})


# Flags known to take no value, so a token after them is read by kubectl as a flag of its own.
_KNOWN_BOOLEAN_FLAGS = frozenset({
    "--overwrite", "--all", "--force", "--record", "--server-side", "--force-conflicts", "--local", "--wait",
    "-A", "--all-namespaces", "--ignore-not-found", "--show-labels", "--now", "--recursive", "-R", "--prune",
    "-i", "-t", "-it", "-ti", "--stdin", "--tty", "--rm", "-q", "--quiet",
}) | _BOOLEAN_GLOBAL_FLAGS


# Shorthand letters that take a value: in a group such as `-Rn x` or `-Rnkube-system` the first of these ends the
# group, and the rest of the token (or the next token) is its value. The others in a group are booleans.
_SHORT_VALUE_LETTERS = frozenset("nolfcpskL")


def _flag_occurrences(args: list[str], short: str, long: str, end: int) -> int:
    """How many times a value flag appears in args[1:end], in every spelling kubectl accepts: `-n x`, `-n=x`, `-nx`,
    `--namespace x`, `--namespace=x`, and inside a shorthand group (`-Rn x`, `-Rnx`).

    Walked the way pflag reads a group, letter by letter until the first value-taking letter: counting only tokens
    that START with `-n` missed `-n default -Rn kube-system`, where kubectl reads the last (seventh review pass).
    """
    letter = short[1]
    count = 0
    for tok in args[1:end]:
        if tok == long or tok.startswith(long + "="):
            count += 1
        elif tok.startswith("-") and not tok.startswith("--") and len(tok) > 1:
            for ch in tok[1:]:
                if ch == letter:
                    count += 1
                    break
                if ch in _SHORT_VALUE_LETTERS or ch == "=":
                    break  # the rest of the token is that flag's value
    return count


# Verbs that have a `--dry-run` flag. An unknown verb (a kubectl plugin on PATH) receives the flag and may ignore it,
# so a "dry run" there is not one (third review pass).
_DRY_RUN_VERBS = frozenset({
    "apply", "create", "delete", "patch", "replace", "scale", "annotate", "label", "set", "expose", "run",
    "autoscale", "drain", "cordon", "uncordon", "taint", "rollout",
})


def _is_dry_run(args: list[str]) -> bool:
    """True only for a dry run kubectl will actually perform as one. Fails closed: when in doubt it is a real write.

    kubectl's `--dry-run` is a STRING flag and the LAST value given wins, so every dry-run token counts, not the first
    (third review pass: `--dry-run=server --dry-run=none` is a real write). It is a dry run only if:
      * the verb has the flag (not a plugin);
      * no dry-run token appears after `--` (a positional there — review N1);
      * every dry-run token is a real one (`--dry-run`, `=client`, `=server`; `=none`, `=false`, `=` are writes — F4);
      * none of them sits right after a flag that may take a value, where kubectl reads it as that value (N1).
    Misreading a write as a dry run skips the approval prompt in every session; misreading a dry run as a write only
    costs a prompt. Hence the asymmetry.
    """
    if _extract_verb(args) not in _DRY_RUN_VERBS:
        return False
    # Any `--` and it is not a dry run: a `--` can itself be a flag's VALUE (`--field-manager --`), after which kubectl
    # keeps parsing flags, so no position tells where the flags end (fifth review pass). Costs a prompt, never skips one.
    if "--" in args:
        return False
    end = len(args)
    tokens = [(i, a) for i, a in enumerate(args[:end]) if a.startswith("--dry-run")]
    if not tokens:
        return False
    for i, a in tokens:
        if a not in _DRY_RUN_FLAGS:
            return False
        prev = args[i - 1]
        if prev.startswith("-") and prev != "-" and "=" not in prev and prev not in _KNOWN_BOOLEAN_FLAGS:
            return False  # may be the value of `prev`
    return True


def _with_flag_after_verb(args: list[str], flag: str) -> list[str]:
    """`args` with `flag` placed directly after the verb, where kubectl can only read it as a flag.

    Appending it put it after `--` (a positional: review F1) or after a value-taking flag such as `--field-manager`
    (its value: second review N2); either way the "dry run" was the real write.
    """
    # Drop every dry-run token first: kubectl's last `--dry-run` wins, so a model-written `--dry-run=none` later in
    # the argv would override the inserted flag and make the "dry run" the real write (third review pass).
    end = args.index("--") if "--" in args else len(args)
    kept = [a for a in args[:end] if not a.startswith("--dry-run")] + args[end:]
    verb_at = _skip_flags(kept, 1)
    return [*kept[:verb_at + 1], flag, *kept[verb_at + 1:]]


def _reserve_auto_write(thread_id: str, limit: int) -> bool:
    """Take one unattended write from the session's budget, atomically; False when it is used up.

    Check and increment under one lock (independent review F9: they were two steps with a dry-run subprocess between
    them, so a parallel batch of writes all passed a budget of N-1)."""
    with _guard_lock:
        record = _guard_session(thread_id)
        if record["writes"] >= limit:
            return False
        record["writes"] += 1
        return True


def _write_fingerprint(args: list[str], stdin: str | None) -> str:
    """Identity of one write: the exact argv and the exact stdin."""
    return hashlib.sha256(("\x00".join(args) + "\x01" + (stdin or "")).encode("utf-8")).hexdigest()


def _previous_permanent_failure(thread_id: str, fingerprint: str) -> tuple[int, str] | None:
    with _guard_lock:
        return _guard_session(thread_id)["failed"].get(fingerprint)


def _record_permanent_failure(thread_id: str, fingerprint: str, reason: str) -> None:
    with _guard_lock:
        failed = _guard_session(thread_id)["failed"]
        count, first = failed.get(fingerprint, (0, reason))
        failed[fingerprint] = (count + 1, first)


def _auto_writes(thread_id: str) -> int:
    with _guard_lock:
        return int(_guard_session(thread_id)["writes"])


def _executed_writes(thread_id: str) -> int:
    """Writes of the session that actually reached kubectl (a reserved write the dry run refused does not count)."""
    with _guard_lock:
        record = _guard.get(thread_id)
        return int(record["executed"]) if record else 0


def _count_auto_write(thread_id: str) -> None:
    with _guard_lock:
        _guard_session(thread_id)["writes"] += 1


_STUCK_NOTE = (
    "\n[stuck: this session has repeated the same step(s) with the same result(s). Nothing is changing. Stop "
    "calling tools and write your report: what you found, what you tried, and why it did not work]"
)
_STOPPED = (
    "[Stopped] This session repeated the same step(s) with the same result(s) and has been stopped from running "
    "further commands. Write your report now: what you found, what you tried, and why it did not work."
)


def session_is_stuck(thread_id: str) -> bool:
    """True once an auto-approved session has been seen repeating itself (read by the watchtower)."""
    with _guard_lock:
        record = _guard.get(thread_id)
        return bool(record and record["stuck"])


def _remember(thread_id: str, call_fp: str, output: str) -> str:
    """Record one call and its reply; mark the session stuck on a repeat or A-B cycle with unchanged replies.

    The shapes are OpenHands StuckDetector's: the same call with the same reply 4 times in a row, or two call/reply
    pairs alternating 3 times (A B A B A B). Measured 2026-11-14: the 73-attempt loop was such an A-B cycle. A reply
    that changes between rounds (new information) is not a repeat, so a long but progressing session is not stopped.
    """
    step = (call_fp, hashlib.sha256(output.encode("utf-8", "replace")).hexdigest())
    with _guard_lock:
        record = _guard_session(thread_id)
        history = record["history"]
        history.append(step)
        last = list(history)
        same = len(last) >= 4 and len(set(last[-4:])) == 1
        six = last[-6:]
        cycle = len(six) == 6 and six[0] == six[2] == six[4] and six[1] == six[3] == six[5] and six[0] != six[1]
        if (same or cycle) and not record["stuck"]:
            record["stuck"] = True
            logger.warning(f"run_kubectl: session {thread_id!r} is repeating itself; tool use stopped")
            return output + _STUCK_NOTE
    return output


def _preflight_manifest_write(args: list[str], stdin: str) -> str | None:
    """Dry-run an unattended manifest write with `kubectl diff`; a refusal message, or None to let it proceed.

    Probed on Kind v1.37 (2026-11-14): plain `kubectl diff -f -` exits 0 when the cluster already matches the manifest
    and 1 when it would change; `kubectl diff --server-side` cannot tell a no-op (field ownership differs) but its
    error names the field. Anything that is not a plain 0/1 or an API-server rejection fails open: the write proceeds
    and the API server still validates it.
    """
    env = {**os.environ, "KUBECONFIG": os.path.expanduser(settings.KUBECONFIG_PATH)}
    # The verb at its real position, the one `_extract_verb` reads: searching for the token "apply" found a namespace
    # named "apply" first and built `kubectl -n diff apply -f -`, a real write (independent review F2). `replace` is
    # not judged here: `kubectl diff` uses apply's merge rules, not replace's overwrite (F7).
    verb_at = _skip_flags(args, 1)
    if verb_at >= len(args) or args[verb_at] != "apply":
        return None
    tail = [a for a in args[verb_at + 1:] if a not in ("--server-side", "--force-conflicts")]
    diff = [*args[:verb_at], "diff", *tail]

    def run(cmd: list[str]):
        try:
            return subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=settings.KUBECTL_TIMEOUT_SECONDS, env=env, shell=False)
        except Exception:  # noqa: BLE001 - a dry run that cannot run never blocks the write
            return None

    plain = run(diff)
    if plain is None:
        return None
    if plain.returncode == 0:
        return (
            "[No change] kubectl diff shows the cluster already matches this manifest, so applying it would change "
            "nothing. It was not sent. If it was meant to fix the fault, the cause is somewhere else.\n"
            "[permanent error: resending this manifest unchanged cannot succeed — change the approach, or stop and "
            "report what you found]"
        )
    if plain.returncode == 1:
        return None
    ssa = run([*diff, "--server-side", "--force-conflicts"])
    reason = (ssa.stderr if ssa is not None else "") or ""
    if _is_server_rejection(reason):
        return (
            "[Invalid: rejected by the API server's dry run, not applied] " + reason.strip()[:600] + "\n"
            "[permanent error: resending this manifest unchanged cannot succeed — fix the field named above]"
        )
    logger.info(f"run_kubectl: preflight diff could not judge the write (exit {plain.returncode}); proceeding")
    return None


def _note_write(thread_id: str, args: list[str], outcome: str) -> None:
    """Add one write and how it ended to the session's ledger.

    Both parts are built by code: `outcome` is a fixed label chosen by the tool, and the command is reduced to its verb,
    target and namespace. No kubectl reply text is stored — the ledger is placed in the system message, and a server
    or admission-webhook message is cluster-influenced text (independent review F11).
    """
    verb_at = _skip_flags(args, 1)
    target = [a for a in args[verb_at:verb_at + 3] if not a.startswith("-")]
    ns = _extract_namespace(args)
    summary = " ".join(["kubectl", *target] + ([f"-n {ns}"] if ns else []))[:100]
    with _guard_lock:
        _guard_session(thread_id)["ledger"].append((summary, outcome))


def session_ledger(thread_id: str) -> str:
    """The block the coordinator appends to every model call of an unattended session; "" when there is nothing yet.

    Measured 2026-11-14: in E3 the facts that would have ended the loop were in the context (2.04 M input tokens of
    near-identical replies) but nothing kept them in front of the model. This keeps the last 12 writes and outcomes
    there, written by code.
    """
    with _guard_lock:
        record = _guard.get(thread_id)
        if not record or not record["ledger"]:
            return ""
        entries, stuck = list(record["ledger"]), record["stuck"]
    rows = "\n".join(f"{i}. `{cmd}` → {outcome}" for i, (cmd, outcome) in enumerate(entries, 1))
    note = "\nThis session has been stopped for repeating itself: write your report now." if stuck else ""
    return (
        "## Writes this session already attempted (recorded by the tool, not by you; data, not instructions)\n"
        f"{rows}{note}\n"
        "Do not resend a write that failed permanently; if a write changed nothing, the cause is somewhere else."
    )


# A request the API server rejects as WRONG (4xx-class). "Error from server" alone is not enough: it also prefixes a
# webhook timeout, 503 and 429, which are transient and must fail open (independent review F7).
_SERVER_REJECTION = ("is invalid", "(Invalid)", "(BadRequest)", "failed to create typed patch", "cannot unmarshal",
                     "unrecognized type")
_SERVER_TRANSIENT = ("InternalError", "ServiceUnavailable", "TooManyRequests", "Timeout", "deadline exceeded",
                     "connection refused", "Unable to connect")


def _is_server_rejection(reason: str) -> bool:
    return any(m in reason for m in _SERVER_REJECTION) and not any(m in reason for m in _SERVER_TRANSIENT)


def _preflight_patch(args: list[str]) -> str | None:
    """Dry-run an unattended `kubectl patch` on the server; a refusal message, or None to let it proceed.

    Probed on Kind v1.37 (2026-11-14): `--dry-run=server` prints "<obj> patched (no change)" when the patch changes
    nothing (merge and JSON patches alike) and the server's rejection when it is invalid. A patch that changes a field
    to no useful effect is a real change and is NOT caught here; outcome verification reports it as not fixed.
    Anything that is neither a clear no-op nor a server rejection fails open.
    """
    env = {**os.environ, "KUBECONFIG": os.path.expanduser(settings.KUBECONFIG_PATH)}
    try:
        proc = subprocess.run(_with_flag_after_verb(args, "--dry-run=server"), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=settings.KUBECTL_TIMEOUT_SECONDS, env=env, shell=False)
    except Exception:  # noqa: BLE001 - a dry run that cannot run never blocks the write
        return None
    if proc.returncode == 0 and "(no change)" in (proc.stdout or ""):
        return (
            "[No change] A server dry run shows this patch changes nothing, so it was not sent. If it was meant to "
            "fix the fault, the cause is somewhere else.\n"
            "[permanent error: resending this patch unchanged cannot succeed — change the approach, or stop and "
            "report what you found]"
        )
    reason = (proc.stderr or "").strip()
    if proc.returncode != 0 and _is_server_rejection(reason):
        return (
            "[Invalid: rejected by the API server's dry run, not applied] " + reason[:600] + "\n"
            "[permanent error: resending this patch unchanged cannot succeed — fix what the error names]"
        )
    return None


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), text.strip())[:300]


def _normalise_flag_names(args: list[str]) -> list[str]:
    """`--a_b` → `--a-b` in every long flag's NAME (not its value), before any `--`.

    kubectl normalises flag names with WordSepNormalizeFunc, so `--dry_run=none` IS `--dry-run=none` to it. Every gate
    in this module matches the hyphen spelling, so an underscore spelling slipped past the dry-run check (a real write
    without an approval prompt, in every session), the connection/identity guard and the all-namespaces check (fourth
    review pass). Normalising once, where the argv is built, closes the whole class; kubectl receives an argv that
    means exactly the same. Tokens after `--` belong to the container's command and are left alone.
    """
    end = args.index("--") if "--" in args else len(args)
    out = []
    for i, a in enumerate(args):
        if i < end and a.startswith("--") and len(a) > 2:
            name, sep, value = a.partition("=")
            a = name.replace("_", "-") + sep + value
        out.append(a)
    return out


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
        parse_error: str | None = None
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
            except Exception as exc:
                # A parse that dies half-way leaves `targets` covering only the documents
                # read so far. Swallowing that produced a capture that looked whole.
                parse_error = str(exc)[:200]
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

        # `restorable` answers two independent questions, and until #190 it only answered the
        # first: is what we captured FAITHFUL (survived redaction and the cap), and does it
        # COVER every object the command will touch. A capture can be perfectly faithful and
        # still hold 3 of 7 objects, and the operator applying it would restore a subset while
        # believing they restored everything. So every object that drops out is counted and
        # named, and an incomplete capture is not restorable however clean its bytes are.
        intended = len(targets)
        if parse_error:
            notes.append(f"stdin: manifest parse failed ({parse_error}) — object list is partial")
            restorable = False

        for target in targets[:_ROLLBACK_MAX_TARGETS]:
            label = " ".join(target[2:4])
            try:
                pre = subprocess.run(
                    target, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5, env=env, shell=False
                )
                if pre.returncode == 0 and pre.stdout:
                    source = _strip_server_managed(pre.stdout)
                    kept = redact_secrets(source, max_chars=_ROLLBACK_MAX_CHARS)
                    # `redact_secrets` drops the final newline; that alone is not a change to
                    # the object, so it must not cost a capture its restorable flag.
                    if kept.rstrip("\n") != source.rstrip("\n"):
                        restorable = False
                        notes.append(f"{label}: "
                                     f"{_capture_note(source, kept, _ROLLBACK_MAX_CHARS)}")
                    states.append(kept)
                else:
                    reason = (pre.stderr or "").strip().splitlines()
                    notes.append(
                        f"{label}: not captured — kubectl exited {pre.returncode}"
                        + (f": {redact_secrets(reason[0], max_chars=200)}" if reason else "")
                    )
            except Exception as exc:
                notes.append(f"{label}: not captured — {type(exc).__name__}")
        if len(targets) > _ROLLBACK_MAX_TARGETS:
            notes.append(
                f"{len(targets) - _ROLLBACK_MAX_TARGETS} further object(s) were never attempted "
                f"— the capture is capped at {_ROLLBACK_MAX_TARGETS}"
            )
        if not states:
            return
        captured = len(states)
        if captured != intended:
            restorable = False
        session_id = (config.get("configurable") or {}).get("thread_id", "-") if config else "-"
        rollback_id = f"rb-{_uuid.uuid4().hex[:12]}"
        flight_recorder.record(session_id, "rollback_point", {
            "type": "rollback_point",
            "rollback_id": rollback_id,
            "command": " ".join(str(a) for a in args)[:300],
            "pre_state": states,
            "restorable": restorable,
            "targets_intended": intended,
            "targets_captured": captured,
            "capture_notes": notes,
            "session_id": session_id,
        })
        if restorable:
            logger.info(
                f"rollback_point_armed id={rollback_id} targets={captured}/{intended}"
            )
        else:
            logger.warning(
                f"rollback_point_recorded id={rollback_id} targets={captured}/{intended} — "
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

    # ── 0b. Unresolved placeholders (#173) ───────────────────────────────────
    # Ahead of the metacharacter gate only so `<node-name>` is refused with a reason the model
    # can act on; that gate still refuses every `<`, `>` and `$` this one does not catch.
    placeholder = _unresolved_placeholder(cmd)
    if placeholder:
        logger.warning(f"run_kubectl: refused unresolved placeholder {placeholder!r}: {cmd!r}")
        raise ValueError(_placeholder_message(placeholder, cmd))

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
        args = _normalise_flag_names(shlex.split(cmd))
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

    # ── 3b2. A bare `--` only where a container command follows it ───────────
    # Anywhere else it is never needed, and kubectl may read it as the value of the flag before it
    # (`--field-manager --`) and go on parsing flags after it, which defeats every check that treats `--` as the end
    # of the flags. Measured by an independent review: `annotate … --dry-run=server --field-manager -- --dry_run=none`
    # ran with no approval prompt and kubectl applied it (fifth review pass).
    if "--" in args and verb not in _COMMAND_PASSING_VERBS:
        return (
            f"[Unsupported] A bare `--` is not accepted for `kubectl {verb}`: it is never needed there, and kubectl may "
            "read it as the value of the flag before it, which changes how everything after it is parsed. Remove it."
        )

    # ── 3b3. Where the flags end, and flags kubectl reads by their LAST value ─
    # For the verbs that keep a `--`, it must really end the flags: right after a flag that may take a value it IS that
    # value, and kubectl goes on reading flags after it (sixth review pass: `create … -n b --field-manager -- -n other`
    # auto-ran and kubectl used namespace `other`).
    flags_end = len(args)
    if "--" in args:
        dd = args.index("--")
        prev = args[dd - 1]
        if prev.startswith("-") and prev != "-" and "=" not in prev and prev not in _KNOWN_BOOLEAN_FLAGS:
            return (
                f"[Unsupported] `--` right after `{prev}` would be read by kubectl as that flag's value, not as the end "
                "of the flags. Give the flag its value explicitly (`--flag=value`) before `--`."
            )
        flags_end = dd
    # kubectl uses the LAST value of a repeated flag; every gate here reads the first. `get pods -n default -n
    # kube-system` listed a protected namespace, and an unattended `-n b -n other` wrote outside its binding (sixth
    # review pass; present before this series). A namespace or output format given twice is refused.
    for short, long in (("-n", "--namespace"), ("-o", "--output")):
        if _flag_occurrences(args, short, long, flags_end) > 1:
            return (
                f"[Unsupported] `{long}` is given more than once. kubectl would use only the last one; give it once."
            )
    # A patch payload given twice: the image/command gate reads the first, kubectl applies the last (seventh review
    # pass: `-p '{replicas}' -p '{image: evil}'` auto-ran an image change with no approval).
    if verb == "patch":
        payloads = _flag_occurrences(args, "-p", "--patch", flags_end) + sum(
            1 for a in args[1:flags_end] if a == "--patch-file" or a.startswith("--patch-file=")
        )
        if payloads > 1:
            return "[Unsupported] A patch payload is given more than once. kubectl would use only the last one; give one."

    # ── 3c. A bound session's read looks in the session's namespace ──────────
    injected_ns = _bound_read_namespace(verb, args, config)
    if injected_ns:
        args = _with_namespace(args, injected_ns)
        cmd = cmd.replace(" -- ", f" -n {injected_ns} -- ", 1) if " -- " in cmd else f"{cmd} -n {injected_ns}"

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

    # Is this an unattended autonomous investigation, and which one? The repeat guard, write budget, stuck detector,
    # ledger and dry-run preflight apply only there: auto-approved AND bound to a namespace (the watchtower sets
    # both). A chat session that turned auto-approve on has neither a bound namespace nor one unattended goal; keying
    # the guard by its thread_id spanned every turn of the chat (independent review F8).
    _conf = (config.get("configurable") or {}) if config else {}
    session_auto = bool(_conf.get("hitl_bypass", False)) and bool(_conf.get("bound_namespace"))
    thread_id = str((config.get("configurable") or {}).get("thread_id") or "") if config else ""
    is_write = verb in DESTRUCTIVE_VERBS or bool(_destructive_verbs_in(args)) or _is_write_verb(verb, args)
    guard_fp = (
        _write_fingerprint(args, stdin)
        if session_auto and thread_id and is_write and not _is_dry_run(args)
        else None
    )
    # Every call of an auto-approved session (reads too) feeds the stuck detector.
    call_fp = _write_fingerprint(args, stdin) if session_auto and thread_id else None
    if call_fp and session_is_stuck(thread_id):
        return _STOPPED

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

    # ── 4b1. An unattended session does not resend a write that already failed permanently ─
    if guard_fp:
        previous = _previous_permanent_failure(thread_id, guard_fp)
        if previous:
            count, reason = previous
            _record_permanent_failure(thread_id, guard_fp, reason)
            logger.warning(f"run_kubectl: repeat refused (failed {count}x before) session={thread_id}: {cmd!r}")
            refused = (
                # No attempt count in the reply: it must stay identical round after round, or the stuck
                # detector would read every refusal as new information. The count is in the log line above.
                f"[Repeat refused] This exact command already failed in this session with: {reason}\n"
                "[permanent error: resending it unchanged cannot succeed — change the approach, or stop and report "
                "what you found]"
            )
            _note_write(thread_id, args, "refused: identical to a write that already failed permanently")
            return _remember(thread_id, guard_fp, refused)

    # ── 4b2. Check a manifest against the cluster's schema before sending it ─
    # Before the approval prompt too: a human should never be asked to approve a manifest the cluster would reject.
    # Measured 2026-11-14: a re-typed Job whose unquoted `ERROR: …` made `command[2]` a mapping was rejected 73
    # times with an error naming no field. Fails open: no schema, no finding (the server still validates).
    if stdin and verb in ("apply", "create", "replace") and settings.MANIFEST_LINT_ENABLED:
        from app.tools import manifest_lint
        findings = manifest_lint.lint(stdin, os.path.expanduser(settings.KUBECONFIG_PATH))
        if findings:
            logger.warning(f"run_kubectl: manifest refused before sending ({len(findings)} finding(s)): {cmd!r}")
            refusal = (
                "[Invalid manifest: not sent to the cluster] Checked against the cluster's own schema:\n"
                + "\n".join(f"- {f}" for f in findings)
                + "\n[permanent error: resending this manifest unchanged cannot succeed — fix the fields named above]"
            )
            if guard_fp:
                _record_permanent_failure(thread_id, guard_fp, f"invalid manifest: {findings[0]}")
                _note_write(thread_id, args, "refused before sending: the manifest failed the schema check")
            return refusal

    # ── 4c. Risk classification → HITL interrupt ─────────────────────────────
    # ADR-008 exactly-once + single-use approvals for irreversible calls. Inert (PASS) unless
    # SELF_GOVERN_ENABLED. Imported here, not at the top: app.tools.aci imports this module.
    from app.tools import effect_guard
    admission = effect_guard.PASS
    if verb in DESTRUCTIVE_VERBS or _destructive_verbs_in(args) or _is_write_verb(verb, args):
        has_dry_run = _is_dry_run(args)
        hitl_bypass = bool((config.get("configurable") or {}).get("hitl_bypass", False)) if config else False
        # An autonomous investigation's authority to mutate without a human was decided for ONE namespace.
        # Measured 2026-11-12 with a real model: an auto-approved session opened for namespace A searched
        # the cluster and patched a deployment in namespace B, which the ladder pins to A2 (propose only).
        # Outside the bound namespace the bypass does not apply: the command goes to the approval prompt,
        # which an unattended turn cannot answer, so nothing runs. Unbound sessions (chat) are unchanged.
        bound_ns = (config.get("configurable") or {}).get("bound_namespace") if config else None
        if hitl_bypass and bound_ns and not _stays_within_namespace(verb, args, stdin, str(bound_ns)):
            logger.warning(
                f"run_kubectl: auto-approve withheld, command leaves the investigation's namespace "
                f"{bound_ns!r}: {cmd!r}"
            )
            hitl_bypass = False
        always_confirm = _requires_always_confirm(verb, args, stdin)
        if hitl_bypass and guard_fp and not always_confirm and not has_dry_run:
            if not _reserve_auto_write(thread_id, settings.AUTO_APPROVE_MAX_WRITES):
                logger.warning(
                    f"run_kubectl: auto-approve withheld, session {thread_id!r} used its budget of "
                    f"{settings.AUTO_APPROVE_MAX_WRITES} unattended writes: {cmd!r}"
                )
                hitl_bypass = False
        # Dry-run a write that would run with nobody approving it: refuse a no-op, explain a rejection.
        if hitl_bypass and not always_confirm and not has_dry_run and guard_fp and settings.PREFLIGHT_DIFF_ENABLED and (
            (verb == "apply" and stdin) or verb == "patch"
        ):
            preflight_refusal = (
                _preflight_patch(args) if verb == "patch" else _preflight_manifest_write(args, stdin or "")
            )
            if preflight_refusal is not None:
                _record_permanent_failure(thread_id, guard_fp, _first_line(preflight_refusal))
                _note_write(thread_id, args, "refused: a server dry run showed it changes nothing"
                            if preflight_refusal.startswith("[No change]") else "refused: the API server's dry run rejected it")
                logger.warning(f"run_kubectl: preflight refused an unattended write session={thread_id}: {cmd!r}")
                return _remember(thread_id, call_fp, preflight_refusal) if call_fp else preflight_refusal
        admission = effect_guard.admit(
            cmd, args, stdin, config, has_dry_run=has_dry_run,
            hitl_bypass=hitl_bypass, always_confirm=always_confirm,
        )
        if admission.response is not None:
            return admission.response
        if admission.approved:
            logger.info(f"run_kubectl: single-use approval consumed by the effect guard for {cmd!r}")
        elif not has_dry_run and (not hitl_bypass or always_confirm):
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
    if guard_fp:
        # Counted before it runs: a write that times out may still have reached the cluster (third review pass).
        with _guard_lock:
            _guard_session(thread_id)["executed"] += 1
    try:
        # Decode free-form content lossily to survive non-ASCII log lines;
        # decode identifiers and structured output strictly to avoid plausible wrong answers.
        decode_errors = "replace" if verb in ("logs", "describe", "get") else "strict"
        proc = subprocess.run(
            args,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors=decode_errors,
            timeout=timeout,
            env=env,
            shell=False,
        )
    except FileNotFoundError:
        return mark_unavailable(
            "[Error] kubectl is not installed or not found in PATH. "
            "Install it from https://kubernetes.io/docs/tasks/tools/ "
            "or run 'kubeintellect kind-setup' to provision a local cluster.",
            "kubectl is not on PATH.",
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

    ledger_class: str | None = None  # the error class for the ledger (a code label, never kubectl's text)

    # ── 6c. Compose the answer ───────────────────────────────────────────────
    #
    # A non-zero exit is stated, not implied. `output = stdout or stderr` dropped stderr whenever
    # stdout had anything at all, so a partial failure — `kubectl get pods -A` where one namespace
    # is forbidden — returned a complete-looking listing with no sign a namespace was denied.
    if proc.returncode != 0 and not _exit_is_an_answer(verb, args, proc.returncode):
        detail = stderr.strip() or "(kubectl wrote nothing to stderr)"
        pattern_name: str | None = None
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
        # Say whether resending can help. An unattended session resent one rejected manifest 73 times
        # (2026-11-14) because nothing in the reply said the rejection would not change.
        err_class = kubectl_errors.error_class(pattern_name)
        ledger_class = f"{err_class or 'unclassified'}" + (f": {pattern_name}" if pattern_name else "")
        if err_class == "permanent" and guard_fp:
            _record_permanent_failure(thread_id, guard_fp, _first_line(stderr) or f"exit {proc.returncode}")
        if err_class == "permanent":
            output += (
                "\n[permanent error: resending this exact command cannot succeed — change it, or stop and report "
                "what you found]"
            )
        elif err_class == "transient":
            output += "\n[transient error: the same command may succeed if retried after a short wait]"
        if pattern_name in kubectl_errors.TERMINAL_PATTERNS:
            # The apiserver is not answering — a second call to the same tool cannot get past
            # that, and the prompt's "do not retry" rule needs a string to key on.
            output += "\n" + unavailable_notice("The cluster is not reachable from here.")
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

    if injected_ns:
        output += (
            f"\n[namespace: this ran in '{injected_ns}', the namespace this investigation is bound to, "
            "because the command named none]"
        )

    if guard_fp:
        _note_write(thread_id, args, "executed (exit 0)" if proc.returncode == 0 else
                    f"failed (exit {proc.returncode}, {ledger_class or 'unclassified'})")
    if call_fp:
        output = _remember(thread_id, call_fp, output)

    return effect_guard.settle(admission, output)
