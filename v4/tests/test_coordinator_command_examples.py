"""Prompt examples must not teach unresolved command arguments.

A model copies the shape it is shown. When the coordinator prompt demonstrated
`kubectl get pods -n <ns>` eighteen times, the model emitted `<ns>` literally, the
kubectl guard rejected it for containing shell metacharacters, and the run failed
(#173). The repair was to ground every example in concrete illustrative values.

This gate keeps them grounded. It scans anywhere a placeholder could be read as
part of a command — not only lines that happen to contain the word `kubectl`,
because the shape is just as copyable on a bare `-n` line or in a `key=value`
directive, and two of the lines the original repair fixed were exactly those.
"""
import re

import pytest

from app.agent.nodes.coordinator import _COORDINATOR_SYSTEM
from app.cortex import graph as cortex_graph
from app.tools.output_policy import TRUNCATION_CLAUSE

# `<...>` shapes that are not command arguments and must never be flagged:
#   <none>     — what kubectl actually prints in an empty ENDPOINTS column
#   <findings> — an XML tag the synthesis step is told to expect
#   <state>, <metric> — slots in quoted sentences the model prints to a human,
#                       not arguments it substitutes into a command
_NOT_ARGUMENTS = {"<none>", "<findings>", "<state>", "<metric>"}

# A line is command-ish if it names a CLI this project drives, or carries a flag.
_COMMAND_LINE = re.compile(
    r"(?:\b(?:kubectl|helm|logcli|promtool)\b"      # a CLI we actually invoke
    r"|(?<![\w-])-[a-zA-Z](?![\w-])"                # a short flag: -n, -l, -c
    r"|--[a-z][a-z-]*"                              # a long flag: --tail, --grace-period
    r"|\b[a-z_]+=)"                                 # a key=value directive, e.g. namespace=
)
_PLACEHOLDER = re.compile(r"<[A-Za-z][A-Za-z0-9_.-]*>")

_PROMPTS = {
    "coordinator": _COORDINATOR_SYSTEM,
    "truncation_clause": TRUNCATION_CLAUSE,
    "cortex_gather": cortex_graph._GATHER_SYSTEM,
    "cortex_synthesis": cortex_graph._SYNTHESIS_SYSTEM,
}


def _offenders(prompt: str) -> list[str]:
    hits = []
    for line in prompt.splitlines():
        if not _COMMAND_LINE.search(line):
            continue
        for ph in _PLACEHOLDER.findall(line):
            if ph not in _NOT_ARGUMENTS:
                hits.append(f"{ph}  in: {line.strip()}")
    return hits


@pytest.mark.parametrize("name", sorted(_PROMPTS))
def test_command_examples_have_no_unresolved_metavariables(name):
    assert _offenders(_PROMPTS[name]) == [], _offenders(_PROMPTS[name])


def test_the_gate_would_catch_the_shapes_that_caused_173():
    """The regression this gate exists for, in every shape it reached us in.

    The first is what #173 actually produced. The others are the lines the
    original repair fixed that a `kubectl`-anchored pattern walks straight past —
    without them the gate is green while the thing it guards has regressed.
    """
    assert _offenders("  kubectl get pods -n <ns>")
    assert _offenders("  TARGETED: namespace=<ns>, resource=<name>")
    assert _offenders('  "use narrower filters (e.g. `-n <namespace>`)"')
    assert _offenders("  kubectl delete pod x --grace-period=<N>")


def test_the_gate_does_not_flag_output_sentinels_or_prose():
    """`<none>` is kubectl's own output, not an argument — flagging it would
    push someone to reword a correct instruction to appease the gate."""
    assert _offenders("  flag any service whose ENDPOINTS column is `<none>`") == []
    assert _offenders("  messages contain <findings> XML") == []
    assert _offenders('  Report: "Fix applied — pod still in <state>"') == []
