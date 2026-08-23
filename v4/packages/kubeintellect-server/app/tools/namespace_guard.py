"""The namespace blocklist, for the datasource tools that query the cluster indirectly.

`run_kubectl` and `run_helm` reach the cluster through a command line, so they enforce
`KUBECTL_BLOCKED_NAMESPACES` by parsing `-n`. `query_loki` and `query_prometheus` reach the
same clusters' data through a *query language*, where the namespace is a label matcher — and
they enforced nothing at all until 2026-08-20. `kubectl logs -n kube-system <pod>` was refused
while `{namespace="kube-system"}` returned the same lines, which matters most for Loki: logs
are where credentials appear in plaintext.

Two gates, because a query language cannot be guarded by reading the query alone:

1. **Input** — a query that *names* a blocked namespace in a positive matcher is refused
   outright, so the user gets the same clear `[Protected]` answer kubectl gives them.
2. **Output** — every returned stream/series is dropped if its own `namespace` label is
   blocked. This is the load-bearing gate: it works on ground truth reported by the
   datasource, so it also catches `{app="nginx"}` matching a pod that happens to run in
   `kube-system`, and any regex matcher the input gate could not evaluate.

**Known residual, stated rather than implied.** A result carrying *no* `namespace` label
passes through: node- and cluster-level metrics legitimately have none, and dropping them
would break ordinary monitoring. So an aggregation that discards the label
(`sum(rate({app="x"}[5m]))`) can still return a *number* computed over a blocked namespace.
No log line, no label, no resource name — a scalar. Closing that would mean rejecting
aggregate queries entirely; it is a deliberate trade, not an oversight.

Not applied to `query_prometheus_series` (nor its series-only wrapper
`query_prometheus_range_raw`): the caller is the detector engine (ADR-010),
whose PromQL comes from human-reviewed playbooks, not from a chat message, and which is
*supposed* to watch protected namespaces for node and control-plane problems.
"""
from __future__ import annotations

import re

from app.core.config import settings

# `namespace="x"`, `namespace =~ "kube-.*"`, and the negative forms we deliberately ignore.
_MATCHER_RE = re.compile(r'\bnamespace\s*(=~|!~|!=|=)\s*"([^"]*)"')


def blocked_namespace_in_query(query: str) -> str | None:
    """Return the blocked namespace a query positively selects, or None.

    Only positive matchers count: `namespace!="kube-system"` is a request to *exclude* it,
    not to read it. A regex matcher is refused when it fully matches a blocked namespace —
    best-effort, because an arbitrary regex cannot be decided here. The output filter is
    what makes that acceptable.
    """
    blocked = settings.kubectl_blocked_namespaces
    for op, value in _MATCHER_RE.findall(query):
        if op == "=":
            if value.lower() in blocked:
                return value.lower()
        elif op == "=~":
            try:
                pattern = re.compile(value)
            except re.error:
                continue
            for ns in blocked:
                if pattern.fullmatch(ns):
                    return ns
    return None


# Every container a datasource puts its labels in. Loki uses `stream` for log results and
# `metric` for metric results; Prometheus always uses `metric`.
_LABEL_CONTAINERS = ("stream", "metric", "labels")


def series_labels(item: dict, hint: str | None = None) -> dict:
    """The label set of one result, wherever the datasource put it.

    **The guard must not depend on a guess about the query.** `label_key` used to be
    authoritative, and `query_loki` chose it by testing whether the LogQL text *starts with*
    one of eight function names. Seven of ten ordinary metric expressions failed that test —
    including `sum by (namespace) (rate(...))`, the most idiomatic form there is — so the
    result was routed as a log query and filtered against `stream`, a key a Loki *matrix*
    payload does not have. `{}.get("namespace", "")` is `""`, `""` is in no blocklist, and
    every series passed, `kube-system` included. Looking in each known container instead makes
    the filter independent of how the request was classified.
    """
    if not isinstance(item, dict):
        # A `scalar`/`string` Prometheus result is a bare `[ts, "v"]` pair, so its entries are
        # not mappings. Reading `.get` on one raised `AttributeError` straight out of the tool:
        # the namespace guard, whose whole job is to make an answer safe, was what destroyed it.
        # Such an entry carries no labels at all, so there is nothing here to protect.
        return {}
    keys = ((hint,) if hint else ()) + _LABEL_CONTAINERS
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def drop_blocked_series(results: list[dict], label_key: str | None = None) -> tuple[list[dict], int]:
    """Split datasource results into (allowed, number dropped) by their namespace label.

    `label_key` is a *hint* about where this datasource usually puts labels; every known
    container is consulted regardless, so a mis-hinted call cannot switch the guard off.
    """
    blocked = settings.kubectl_blocked_namespaces
    allowed = [
        r for r in results
        if str(series_labels(r, label_key).get("namespace", "")).lower() not in blocked
    ]
    return allowed, len(results) - len(allowed)


def drop_blocked_table_rows(output: str) -> tuple[str, int]:
    """Drop rows of a cluster-wide kubectl **table** whose first column is a blocked namespace.

    `kubectl get pods --all-namespaces` prepends a NAMESPACE column, so the row itself says
    where it came from. This is the one filter with more than one caller: `run_kubectl` runs it
    on the output of a tool call, and `context_fetcher` runs it on the pre-fetched snapshot that
    is pasted into every prompt. Until 2026-08-20 only the first of those existed, and the
    second was the same `kubectl get pods -A` with no filter at all — so a question the tool
    would have refused was answered from the prompt.

    Returns the kept text and the number of rows removed; the caller decides where the
    `withheld_note` sentence goes, because a listing embedded in a code fence and a listing
    returned to an agent put it in different places.
    """
    blocked = settings.kubectl_blocked_namespaces
    kept, dropped = [], 0
    for line in output.splitlines(keepends=True):
        parts = line.split()
        if parts and parts[0].lower() in blocked:
            dropped += 1
            continue
        kept.append(line)
    return "".join(kept), dropped


def protected_message(namespace: str) -> str:
    return (
        f"[Protected] Access to namespace '{namespace}' is not permitted. "
        f"This applies to logs and metrics exactly as it applies to kubectl."
    )


def all_withheld_message(dropped: int) -> str:
    """Every result was dropped — say so, rather than naming a namespace we never selected."""
    return (
        f"[Protected] All {dropped} result(s) belong to a namespace in "
        f"KUBECTL_BLOCKED_NAMESPACES and were withheld. Nothing else matched."
    )


# Where the notice goes in a json/yaml payload. A `[Protected]` sentence appended after
# `json.dumps` is *not* JSON — `kubectl get pods -A -o json` has returned a document that
# `json.loads` rejects with "Extra data" since the -A filter was written, and nothing tested
# it. Structured output carries the notice as a field; only text formats get a trailing line.
WITHHELD_KEY = "withheldByPolicy"


def withheld_sentence(dropped: int, noun: str = "result") -> str:
    return (
        f"[Protected] {dropped} {noun}(s) withheld — they belong to a namespace in "
        f"KUBECTL_BLOCKED_NAMESPACES. This listing is NOT the complete set."
    )


def annotate_withheld(doc: dict, dropped: int, noun: str = "result") -> dict:
    """Record the withholding *inside* a structured document, keeping it parseable."""
    if dropped:
        doc[WITHHELD_KEY] = withheld_sentence(dropped, noun)
    return doc


def withheld_note(dropped: int, noun: str = "result") -> str:
    """The one sentence every filter that removes rows must append.

    **A filtered listing and a complete listing are the same bytes.** `kubectl get pods -A`
    said so from the start, but until 2026-08-20 the namespace listing itself, the `helm list`
    filter and their json/yaml/describe variants removed rows in silence — so an agent asked
    *does the monitoring namespace exist?* ran `kubectl get namespaces`, received a list it had
    no way to know was short, and answered **no**. A direct `kubectl get ns monitoring` is
    refused out loud; the listing was the one path that lied by omission.

    `noun` names what was removed, because "3 result(s)" reads as three pods when the caller
    asked for namespaces.
    """
    return "\n" + withheld_sentence(dropped, noun)
