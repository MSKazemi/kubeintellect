"""Read one `run_kubectl` result: did it run, did it fail, or was it refused?

`run_kubectl` returns a **string** — `proc.stdout or proc.stderr or "(no output)"` — and logs the
exit code at DEBUG before discarding it. So every layer above it has to recover the machine-readable
answer from prose, and doing that badly is how a refusal becomes a success (see
`transactional.py`) or an unreadable cluster becomes a health verdict (see `postcondition.py`).

One classifier, used by both, so the two can never disagree about what a given string meant.

Matching is on **line prefixes**, never substrings anywhere: that is the whole difference between
a kubectl failure and a Deployment named `error-budget-exporter`.
"""

from __future__ import annotations

REFUSED = "refused"  # KubeIntellect blocked it — nothing was sent to the cluster
FAILED = "failed"  # kubectl (or the local tooling) reported an error
OK = "ok"  # nothing says otherwise; the caller's own oracle decides from here

# KubeIntellect's own markers, emitted by the tool layer as the first thing in the string, so
# recognising them is reading a structured marker rather than guessing at prose. Verified against
# the real `run_kubectl` on 2026-08-20: `[Permission Denied]` (readonly key on a write; operator
# key on a high-risk verb), `[Protected]` (infrastructure namespace; cluster-wide mutation),
# `[Unsupported]` (a verb that needs a terminal), and `[Error] kubectl is not installed or not
# found in PATH` — which is reproducible on any machine without kubectl, including this one.
_REFUSAL_PREFIXES = (
    "[permission denied]",
    "[protected]",
    "[unsupported]",
    "[blocked]",
    "[error]",
)

# kubectl writes a failure as its own line. Captured from real kubectl (bitnami/kubectl:latest,
# 2026-08-20): `error: the path "/nope.yaml" does not exist` · `error: unable to decode "STDIN":
# Object 'Kind' is missing in …` · `The connection to the server localhost:8080 was refused - did
# you specify the right host or port?`. `error from server` and `unable to connect to the server`
# are kubectl's other standard error openers, included for coverage; they need a cluster (or a
# broken one) to reproduce and were not measured here.
_FAILURE_PREFIXES = (
    "error:",
    "error from server",
    "the connection to the server",
    "unable to connect to the server",
)

# run_kubectl's own placeholder when a command produced neither stdout nor stderr. It is not an
# observation of anything, so it must never be parsed as one.
NO_OUTPUT = "(no output)"

# The subset of failures that mean the command never reached the API server. A gate whose whole
# job is "the server accepted this" has to tell those apart from "the server rejected this".
_UNREACHABLE_PREFIXES = (
    "the connection to the server",
    "unable to connect to the server",
)


def classify_output(output: str) -> str:
    """REFUSED / FAILED / OK for one `run_kubectl` result.

    Anything unrecognised is OK — not because it certainly worked, but because the caller's own
    oracle is a better authority than a keyword. This classifier exists only to catch the cases
    where trusting the text as an observation would be wrong.
    """
    text = (output or "").strip()
    if not text or text == NO_OUTPUT:
        return FAILED
    for raw in text.splitlines():
        line = raw.strip().lower()
        if not line:
            continue
        if line.startswith(_REFUSAL_PREFIXES):
            return REFUSED
        if line.startswith(_FAILURE_PREFIXES):
            return FAILED
    return OK


def reached_cluster(output: str) -> bool:
    """False iff nothing ever got as far as the API server.

    A refusal is emitted by KubeIntellect itself and a connection error by kubectl's client, so in
    both cases the server never saw the command — and any gate that reports on what the server
    said must not report at all.
    """
    text = (output or "").strip()
    if not text or text == NO_OUTPUT:
        return False
    for raw in text.splitlines():
        line = raw.strip().lower()
        if not line:
            continue
        if line.startswith(_REFUSAL_PREFIXES) or line.startswith(_UNREACHABLE_PREFIXES):
            return False
    return True
