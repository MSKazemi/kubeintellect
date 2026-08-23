r"""Every detector predicate — shipped or NL-authored — must be able to fire.

`test_no_reason_regex_alternative_contains_whitespace` in test_detectors.py exists because a
detector shipped as a permanent no-op (#114: `"^(FailedGetResourceMetric | FailedCompute...)$"`
-- an event `reason` never contains a space). Its own docstring names the gap it could not
close: *"nothing else asserts a predicate can actually fire."*

The obvious way to close that gap does not work, and the failed attempt is worth recording.
Asserting "some string satisfies this regex" is vacuous: the string is generated *from* the
pattern, so `^(NodeNotReady |...)$` happily yields `"NodeNotReady "` and the assertion passes.
Applied to #114's real pattern, that gate stays green. A regex is essentially never
unsatisfiable; it is the *domain* that the pattern misses.

So the property asserted here is the domain one, via `app.detectors.predicate_shape`: enumerate
**every** string a pattern can produce and require each to be a value the cluster actually
emits. `message_regex` is exempt -- an event message is free prose.

Two surfaces, because the same mistake reaches production two ways:

* the shipped playbooks, checked `strict=True` so an exotic pattern gets a human look
  instead of a silent pass;
* `validate_detect_block`, the single gate for NL-authored detectors (ADR-012), where a model
  writing regexes from prose makes #114's mistake more readily than a person reading the
  schema. Before this file that validator accepted four distinct permanent no-ops with zero
  errors, and a dead shadow detector's zero firings read as "the condition never occurred".
"""
from __future__ import annotations

import re
import time

import pytest

from app.detectors.authoring import validate_detect_block
from app.detectors.engine import load_detectors
from app.detectors.predicate_shape import (
    LEGAL_REASON,
    UnsupportedPattern,
    enumerate_samples,
    predicate_liveness_errors,
)
from app.sensorium.observations import Observation


# ── the enumerator is itself under test: one that returned [""] or dropped branches
#    would make every assertion below vacuously true ─────────────────────────────────
@pytest.mark.parametrize(("pattern", "expected"), [
    ("^CrashLoopBackOff$", {"CrashLoopBackOff"}),
    ("^(FailedMount|FailedAttachVolume)$", {"FailedMount", "FailedAttachVolume"}),
    ("NotReady|Unknown", {"NotReady", "Unknown"}),
    ("Insufficient (cpu|memory)", {"Insufficient cpu", "Insufficient memory"}),
    ("^Init:[0-9]+/[0-9]+$", {"Init:0/0"}),
    # the #114 shape — the space must survive into the sample, or nothing can catch it
    ("^(FailedGetResourceMetric | FailedComputeMetricsReplicas)$",
     {"FailedGetResourceMetric ", " FailedComputeMetricsReplicas"}),
])
def test_the_enumerator_produces_exactly_the_language(pattern, expected):
    compiled = re.compile(pattern)
    produced = set(enumerate_samples(compiled))
    assert produced == expected
    for sample in produced:
        assert compiled.search(sample), f"{sample!r} does not match {pattern}"


def test_the_enumerator_refuses_rather_than_guesses():
    """If it cannot expand a pattern it must fail, never return something harmless."""
    with pytest.raises(UnsupportedPattern):
        enumerate_samples(re.compile(r"(?=lookahead)x"))


def test_the_shape_check_rejects_the_bug_that_motivated_this_file():
    """#114's exact pattern must be judged illegal for an event reason."""
    bad = re.compile("^(FailedGetResourceMetric | FailedComputeMetricsReplicas)$")
    assert any(not LEGAL_REASON.match(s) for s in enumerate_samples(bad))
    good = re.compile("^(FailedGetResourceMetric|FailedComputeMetricsReplicas)$")
    assert all(LEGAL_REASON.match(s) for s in enumerate_samples(good))


# ── surface 1: the shipped playbooks ────────────────────────────────────────────────
def _all_predicates():
    return [(d.playbook, i, p)
            for d in load_detectors()
            for i, p in enumerate(d.watch_predicates)]


_PREDICATES = _all_predicates()
_IDS = [f"{pb}[{i}]" for pb, i, _ in _PREDICATES]


@pytest.mark.parametrize(("playbook", "index", "pred"), _PREDICATES, ids=_IDS)
def test_every_shipped_predicate_can_fire(playbook, index, pred):
    errors = predicate_liveness_errors(pred, strict=True)
    assert not errors, f"{playbook} predicate #{index}: {errors[0]}"


@pytest.mark.parametrize(("playbook", "index", "pred"), _PREDICATES, ids=_IDS)
def test_every_shipped_predicate_accepts_an_observation(playbook, index, pred):
    """End-to-end on the real `matches()` — catches a self-contradiction the shape check
    cannot see, and keeps the shape rules honest against the engine they describe."""
    fields: dict = {}
    if pred.kind in ("Pod", "Node"):
        kind = "pod_status" if pred.kind == "Pod" else "node_status"
        fields["status"] = enumerate_samples(pred.status_regex)[0] if pred.status_regex else ""
    else:
        kind = "event"
        fields["event_type"] = "Warning"
        if pred.involved_kind:
            fields["involved_kind"] = pred.involved_kind
        fields["reason"] = (
            enumerate_samples(pred.reason_regex)[0] if pred.reason_regex else "AnyReason"
        )
        fields["message"] = (
            enumerate_samples(pred.message_regex)[0] if pred.message_regex else "any message"
        )
    obs = Observation(kind=kind, cluster_id="test", namespace="default",
                      name="obj-1", fields=fields, ts=time.time())
    assert pred.matches(obs), (
        f"{playbook} predicate #{index} ({pred.kind}) rejects an observation built from its "
        f"own regexes: {obs.fields!r}"
    )


def test_the_inventory_is_actually_covered():
    """Guard the guard: if load_detectors() ever returned nothing, both parametrised tests
    above would pass by generating zero cases."""
    detectors = load_detectors()
    assert len(detectors) == 20, f"detector count changed to {len(detectors)}"
    assert len(_PREDICATES) >= len(detectors), "some detector contributed no predicate"


# ── surface 2: the NL-authoring validator ───────────────────────────────────────────
@pytest.mark.parametrize(("label", "raw"), [
    ("kind in the wrong case — matches() is case-sensitive",
     {"watch_predicates": [{"kind": "pod", "status_regex": "^OOMKilled$"}]}),
    ("a kind the engine has never handled",
     {"watch_predicates": [{"kind": "Deployment", "status_regex": "^Stuck$"}]}),
    ("Pod with no status_regex — matches() has nothing to test",
     {"watch_predicates": [{"kind": "Pod"}]}),
    ("#114's exact mistake, this time written by the model",
     {"watch_predicates": [
         {"kind": "Event",
          "reason_regex": "^(FailedGetResourceMetric | FailedComputeMetricsReplicas)$"}]}),
])
def test_the_validator_refuses_a_detector_that_can_never_fire(label, raw):
    block, errors = validate_detect_block(raw, "nl")
    assert block is None, f"accepted a dead detector: {label}"
    assert errors and "never fire" in errors[0], errors


@pytest.mark.parametrize("raw", [
    {"watch_predicates": [
        {"kind": "Event", "reason_regex": "^(FailedGetResourceMetric|FailedComputeMetricsReplicas)$"}]},
    {"watch_predicates": [{"kind": "Pod", "status_regex": "^Init:[0-9]+/[0-9]+$"}]},
    # a prose message legitimately contains spaces — the shape rule must not touch it
    {"watch_predicates": [
        {"kind": "Event", "reason_regex": "^Failed$",
         "message_regex": "Insufficient (cpu|memory)"}]},
    # an exotic-but-valid regex: unknown is not the same as dead, so it must survive
    {"watch_predicates": [{"kind": "Event", "reason_regex": "^[^ ]+BackOff$"}]},
    {"watch_predicates": [{"kind": "Node", "status_regex": "NotReady|Unknown"}]},
])
def test_the_validator_still_accepts_a_detector_that_can_fire(raw):
    block, errors = validate_detect_block(raw, "nl")
    assert block is not None and not errors, errors


def test_an_unexpandable_pattern_is_unknown_not_dead():
    """The tolerance is deliberate and must stay asserted: refusing an author's valid regex
    would be a worse failure than the gap this closes."""
    from app.detectors.models import WatchPredicate
    pred = WatchPredicate(kind="Event", reason_regex=re.compile(r"^(?!Failed)[A-Za-z]+$"))
    assert predicate_liveness_errors(pred) == []
    with pytest.raises(UnsupportedPattern):
        predicate_liveness_errors(pred, strict=True)
