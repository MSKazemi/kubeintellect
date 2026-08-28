"""The architecture of KubeIntellect, as a spec the renderer draws and a test can check.

This file exists because the drawing has to come from the code. The website already carries
`website/public/images/architecture.svg` — a Mermaid export from 2026-03-29 whose nodes are
`Task Router`, `Orchestrator`, `Final Aggregator` and `Code Generator`. That is the **V1**
design (ADR-001, frozen, cited by the paper). It contains no sensorium, no detector engine, no
flight recorder, no memory hierarchy, no autonomy ladder and no approval gate, so it does not
describe anything this repository has shipped since. It is also referenced by no page on the
site. It was not reconciled into this file; it was treated as describing a different system.

**The honesty property, and the whole reason for the `flag` field.** Most of what makes V4
interesting is flag-gated, and a majority of those flags are **off by default**. A diagram that
draws them like everything else claims a system nobody is running — the same defect class the
video audit caught on 2026-08-28, where the narration said "read-only by default" about a server
whose `REQUIRE_AUTH` is `False`. So every component here names:

* ``module`` — a repo-relative path that must exist. The box is labelled with it, so a viewer
  can go read the thing being drawn.
* ``flag``   — the ``Settings`` field that gates it, or ``None`` for something always present.
* ``on``     — what this file claims that flag's **declared default** is.

`v4/tests/test_the_architecture_drawing_matches_the_code.py` reads
``Settings.model_fields[flag].default`` and fails if ``on`` disagrees. It reads the *declared*
default rather than `settings.FLAG`, because the latter is whatever the developer's `.env`
happens to say — which is exactly how a drawing drifts without anyone editing it.

Colours come from `../video/render.py`, which sources each value from the site's own tokens.
Nothing here invents a palette.
"""
from __future__ import annotations

from typing import NamedTuple

SRV = "v4/packages/kubeintellect-server/app"


class Node(NamedTuple):
    key: str
    label: str
    module: str            # repo-relative; the test asserts it exists
    flag: str | None       # Settings field name, or None for always-on
    on: bool | str         # the DECLARED default this file claims
    note: str = ""


class Layer(NamedTuple):
    key: str
    title: str
    blurb: str
    nodes: tuple[Node, ...]


LAYERS: tuple[Layer, ...] = (
    Layer(
        "interface", "INTERFACE", "where a human asks, and where the answer comes back",
        (
            Node("kq", "kq CLI", "v4/packages/kube-q/kube_q", None, True,
                 "REPL, findings, digest, replay, postmortem"),
            Node("api", "HTTP + SSE API", f"{SRV}/api/v1", None, True,
                 "typed event stream — one contract, two halves"),
            Node("proto", "ki-protocol", "v4/packages/ki-protocol", None, True,
                 "wire.py emits, events.py consumes"),
        ),
    ),
    Layer(
        "perception", "PERCEPTION", "what it sees without being asked — and zero tokens to see it",
        (
            Node("watch", "Sensorium", f"{SRV}/sensorium/k8s_watcher.py", "SENSORIUM_ENABLED", True,
                 "kubectl --watch on pods + events"),
            Node("detect", "Detector engine", f"{SRV}/detectors/engine.py", "SENSORIUM_ENABLED", True,
                 "compiled detect: predicates fire findings"),
            Node("predict", "Anticipatory detection", f"{SRV}/detectors/models.py",
                 "PREDICTIVE_DETECTION_ENABLED", False, "range-PromQL + OLS ETA, capped at A1"),
            Node("percep", "Perception classifier", f"{SRV}/detectors/perception.py", None, True,
                 "the one thing that separates quiet from deaf"),
        ),
    ),
    Layer(
        "cognition", "COGNITION", "the graph that decides what to look at next",
        (
            Node("graph", "Coordinator graph (V2)", f"{SRV}/agent/workflow.py", None, True,
                 "PLAN -> FETCH (parallel) -> SYNTHESIZE"),
            Node("ctx", "Context fetcher", f"{SRV}/agent/nodes/context_fetcher.py", None, True,
                 "cluster snapshot + playbook trigger match"),
            Node("play", "Playbook library", f"{SRV}/agent/playbooks", "PLAYBOOKS_ENABLED", True,
                 "YAML patterns rendered into the prompt"),
            Node("cortex", "Cortex V4", f"{SRV}/cortex/graph.py", "CORTEX_V4_ENABLED", False,
                 "explicit triage/gather/synthesize; tiered models"),
        ),
    ),
    Layer(
        "governance", "GOVERNANCE", "the part that is not optional",
        (
            Node("gate", "Human approval gate", f"{SRV}/tools/kubectl_tool.py", None, True,
                 "every mutating command interrupts for a human"),
            Node("rbac", "API-key roles", f"{SRV}/api/v1/auth.py", "REQUIRE_AUTH", False,
                 "readonly/operator/admin/superadmin — ONCE KEYS ARE SET"),
            Node("ladder", "Autonomy ladder", f"{SRV}/autonomy/watchtower.py", "AUTONOMY_LEVEL", "A1",
                 "A1 investigates; A3 auto-fix needs an allowlist"),
            Node("ns", "Protected namespaces", f"{SRV}/tools/kubectl_tool.py", None, True,
                 "blocked namespaces are pinned to A0"),
        ),
    ),
    Layer(
        "memory", "MEMORY", "what it remembers between incidents",
        (
            Node("l1", "L1 episodes", f"{SRV}/memory/episodes.py", "MEMORY_HIERARCHY_ENABLED", True,
                 "pg_trgm recall over past incidents"),
            Node("l2", "L2 temporal KG", f"{SRV}/memory/kg.py", "MEMORY_HIERARCHY_ENABLED", True,
                 "Pod -runs_on-> Node, opened and closed over time"),
            Node("cons", "Consolidation worker", f"{SRV}/memory/consolidation.py",
                 "MEMORY_HIERARCHY_ENABLED", True, "promotes patterns; candidates need review"),
            Node("v5", "Memory V5 slices", f"{SRV}/memory/security.py", "MEMORY_SECURITY_HARDENING",
                 False, "P1-P8: hybrid recall, bi-temporal, PPR, RTBF"),
        ),
    ),
    Layer(
        "record", "RECORD", "so the run can be replayed and argued with afterwards",
        (
            Node("fr", "Flight recorder", f"{SRV}/db/flight_recorder.py", "FLIGHT_RECORDER_ENABLED",
                 True, "hash-chained decision_log; replay exits 3 if broken"),
            Node("pm", "Postmortems", f"{SRV}/digest/postmortem.py", "POSTMORTEM_ENABLED", True,
                 "grounded, seq-cited, carries chain_valid"),
            Node("dig", "Morning digest", f"{SRV}/digest", None, True,
                 "says 'quiet' only if something was watching"),
        ),
    ),
    Layer(
        "substrate", "SUBSTRATE", "what it stands on",
        (
            Node("kubectl", "kubectl tool layer", f"{SRV}/tools/kubectl_tool.py", None, True,
                 "reads freely; every write stops at the gate"),
            Node("pg", "PostgreSQL", f"{SRV}/db/schema.sql", None, True,
                 "episodes, KG, decision_log, audit chains"),
            Node("obs", "Prometheus + Loki", f"{SRV}/tools/prometheus_tool.py", None, True,
                 "always in the monitoring namespace"),
            Node("llm", "LLM provider", f"{SRV}/core/llm.py", None, True,
                 "coordinator + subagent tiers"),
        ),
    ),
)

#: Data flow, as (src, dst, label, phase). The animation lights one phase at a time; a phase is
#: a story, not a category. Every endpoint must be a node key -- the test checks that too.
FLOWS: tuple[tuple[str, str, str, int], ...] = (
    # phase 1 — a human asks
    ("kq", "api", "query", 1),
    ("api", "graph", "", 1),
    ("graph", "ctx", "snapshot", 1),
    ("ctx", "kubectl", "get/describe", 1),
    ("ctx", "play", "trigger match", 1),
    ("graph", "l1", "recall", 1),
    ("graph", "llm", "reason", 1),
    ("graph", "api", "answer (SSE)", 1),
    # phase 2 — it wants to change something
    ("graph", "gate", "mutating command", 2),
    ("gate", "rbac", "role check", 2),
    ("gate", "ns", "namespace check", 2),
    ("gate", "kubectl", "only after approval", 2),
    ("gate", "fr", "decision recorded", 2),
    # phase 3 — nobody asked
    ("kubectl", "watch", "--watch stream", 3),
    ("watch", "detect", "observation", 3),
    ("detect", "percep", "finding", 3),
    ("detect", "ladder", "opens A1 investigation", 3),
    ("ladder", "graph", "investigate", 3),
    ("watch", "l2", "entities + edges", 3),
    # phase 4 — afterwards
    ("fr", "pm", "seq-cited narrative", 4),
    ("fr", "dig", "what happened overnight", 4),
    ("l1", "cons", "consolidate", 4),
    ("cons", "l2", "promote", 4),
    ("dig", "kq", "kq digest", 4),
    ("pm", "kq", "kq postmortem", 4),
)

PHASES: tuple[tuple[int, str, str], ...] = (
    (1, "A human asks", "the query path — read, recall, reason, answer"),
    (2, "It wants to change something", "every mutating command stops here"),
    (3, "Nobody asked", "perception runs whether or not you are watching"),
    (4, "Afterwards", "the record is what makes it arguable"),
)


def nodes() -> dict[str, Node]:
    return {n.key: n for layer in LAYERS for n in layer.nodes}


def default_off() -> list[Node]:
    """Components a stock install is NOT running. Drawn as off, or the drawing lies."""
    return [n for n in nodes().values() if n.on is False]
