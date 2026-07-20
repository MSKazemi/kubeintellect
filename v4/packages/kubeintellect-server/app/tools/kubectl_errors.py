"""kubectl error interpreter.

Pattern-matches common kubectl failure modes on stderr and returns a single-line
hint to append. The original error is always preserved verbatim by the caller —
this module only produces the diagnostic suggestion.

Designed for non-zero exit codes only. Successful kubectl output is never
interpreted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _Pattern:
    name: str           # short identifier for structured logs / metrics
    regex: re.Pattern
    hint: str


_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern(
        "namespace_not_found",
        re.compile(r'namespaces?\s+"[^"]+"\s+not\s+found', re.IGNORECASE),
        "→ Namespace does not exist — run `kubectl get ns` to list valid namespaces.",
    ),
    _Pattern(
        "container_not_found",
        re.compile(r"container\s+\S+\s+is\s+not\s+valid|container\s+not\s+found", re.IGNORECASE),
        "→ Multi-container pod — specify the container with `-c <name>`.",
    ),
    _Pattern(
        "pod_not_found",
        re.compile(r'pods?\s+"[^"]+"\s+not\s+found', re.IGNORECASE),
        "→ Pod may have been rescheduled — re-run `kubectl get pods -n <ns>` to find the new name.",
    ),
    _Pattern(
        "not_found_generic",
        re.compile(r"\(NotFound\)|not found", re.IGNORECASE),
        "→ Resource not found — verify the namespace (`-n`) and the exact name.",
    ),
    _Pattern(
        "forbidden",
        re.compile(r"\(Forbidden\)|forbidden:", re.IGNORECASE),
        "→ Insufficient RBAC permissions — check the user/serviceaccount role bindings.",
    ),
    _Pattern(
        "apiserver_unreachable",
        re.compile(r"connection refused|no route to host|i/o timeout", re.IGNORECASE),
        "→ kube-apiserver unreachable — check cluster connectivity and kubeconfig.",
    ),
    _Pattern(
        "unable_to_connect",
        re.compile(r"unable to connect to the server", re.IGNORECASE),
        "→ Cluster unreachable — kubeconfig may be stale or the context is wrong.",
    ),
    _Pattern(
        "missing_resource_type",
        re.compile(r"the server could not find the requested resource", re.IGNORECASE),
        "→ CRD or API group missing on this cluster — verify with `kubectl api-resources`.",
    ),
    _Pattern(
        "crd_not_recognized",
        re.compile(r"error: unable to recognize", re.IGNORECASE),
        "→ CRD not installed — apply the operator/CRD manifest before this resource.",
    ),
    _Pattern(
        "dns_lookup_failed",
        re.compile(r"dial tcp .* lookup", re.IGNORECASE),
        "→ DNS resolution failed for the apiserver host — check resolv.conf or VPN.",
    ),
    _Pattern(
        "method_not_allowed",
        re.compile(r"MethodNotAllowed", re.IGNORECASE),
        "→ Verb not allowed for this resource — check `kubectl explain <resource>`.",
    ),
    _Pattern(
        "immutable_field",
        re.compile(r"cannot patch .* immutable|field is immutable", re.IGNORECASE),
        "→ Field is immutable — use `kubectl replace` or recreate the resource.",
    ),
    _Pattern(
        "etcd_recovering",
        re.compile(r"etcdserver:\s+(leader changed|request timed out)", re.IGNORECASE),
        "→ etcd is recovering — retry in a few seconds.",
    ),
    _Pattern(
        "yaml_parse_error",
        re.compile(r"error converting YAML to JSON|yaml: unmarshal errors", re.IGNORECASE),
        "→ YAML syntax error — validate with `kubectl apply --dry-run=client`.",
    ),
    _Pattern(
        "concurrent_modification",
        re.compile(
            r"Operation cannot be fulfilled .* the object has been modified",
            re.IGNORECASE,
        ),
        "→ Resource was updated mid-flight — re-fetch and retry the patch.",
    ),
)


def interpret(stderr: str) -> tuple[str | None, str | None]:
    """Return (pattern_name, hint) if any pattern matches, else (None, None).

    The caller is responsible for appending the hint to the original output.
    The original error text is never modified or replaced.
    """
    if not stderr:
        return None, None
    for pat in _PATTERNS:
        if pat.regex.search(stderr):
            return pat.name, pat.hint
    return None, None


def annotate(output: str) -> tuple[str, str | None]:
    """Append a hint to ``output`` when a known pattern is detected.

    Returns ``(annotated_output, pattern_name)``. ``pattern_name`` is ``None``
    when no pattern matched — useful for structured-log telemetry.
    """
    pattern_name, hint = interpret(output)
    if hint is None:
        return output, None
    if hint in output:  # already annotated; don't double-append
        return output, pattern_name
    return f"{output.rstrip()}\n{hint}", pattern_name
