"""`promql:` in a detect block is EVALUATED since #20 — this file used to pin the opposite.

Until #20 the playbook schema advertised three predicate types under `detect:` and two of them
ran. **Nothing read `DetectBlock.promql`** (verified 2026-08-20), so this file asserted that a
promql-only detector was refused everywhere it could appear, and carried a tripwire,
`test_no_code_path_evaluates_promql`, whose docstring said: *"If PromQL evaluation is
implemented, this test fails — deliberately. Update it together with the docs that currently say
the queries are declarative."* #20 implemented it; this is that update.

What changed, and what each test below now pins:

* `parse_detect_block` accepts a promql-only block — it is a detector now;
* `_is_detect_block` counts `promql` (via the shared `EVALUATED_PREDICATE_KEYS`);
* the tripwire is inverted: the engine MUST read `det.promql`, so deleting the evaluator
  without deleting this file fails here, rather than shipping declarative queries that every
  surface describes as live.

Firing semantics, the error state and the authoring gate are covered by
`test_promql_predicates_fire_or_say_why.py`.
"""
from __future__ import annotations

import pathlib
import re

import pytest
from app.agent.playbooks.loader import list_playbooks
from app.detectors.engine import _is_detect_block
from app.detectors.models import parse_detect_block

_APP = pathlib.Path(__file__).resolve().parents[1] / "packages" / "kubeintellect-server" / "app"

_WATCH = {"watch_predicates": [{"kind": "Pod", "status_regex": "^CrashLoopBackOff$"}]}
_PROMQL = {"promql": ['kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"} == 1']}


class TestAPromqlOnlyDetectorIsADetector:
    def test_parse_returns_a_block(self):
        block = parse_detect_block("PromqlOnly", dict(_PROMQL))
        assert block is not None and block.promql == tuple(_PROMQL["promql"])

    def test_a_db_row_with_only_promql_is_a_detect_block(self):
        assert _is_detect_block(dict(_PROMQL)) is True

    def test_an_empty_promql_list_alone_is_still_nothing(self):
        assert parse_detect_block("Empty", {"promql": []}) is None


class TestTheOtherTypesAreUnaffected:
    def test_watch_only_still_compiles(self):
        assert parse_detect_block("WatchOnly", dict(_WATCH)) is not None

    def test_watch_plus_promql_keeps_both(self):
        block = parse_detect_block("Both", {**_WATCH, **_PROMQL})
        assert block is not None and block.watch_predicates
        assert block.promql == tuple(_PROMQL["promql"])

    def test_trend_only_still_compiles(self):
        block = parse_detect_block("TrendOnly", {"trend_predicates": [
            {"metric": "node_filesystem_avail_bytes", "threshold": 0.0}]})
        assert block is not None

    def test_a_consolidation_learned_row_is_still_skipped(self):
        assert _is_detect_block({"derived_from_playbooks": ["X"], "pattern": "y"}) is False


class TestEveryShippedDetectorFiresWithPromqlOff:
    """PROMQL_DETECTION_ENABLED is off by default, so every shipped detector must still have a
    predicate that runs without it — enabling the flag adds coverage, never creates it."""

    @pytest.mark.parametrize("pb", [p for p in list_playbooks() if p.detect is not None],
                             ids=lambda p: p.name)
    def test_it_has_a_watch_or_trend_predicate(self, pb):
        assert pb.detect.watch_predicates or pb.detect.trend_predicates, (
            f"{pb.name} declares only promql, which runs only with PROMQL_DETECTION_ENABLED")

    def test_the_shipped_promql_queries_are_still_carried(self):
        """The count is pinned so a change is deliberate: 20 queries across the shipped playbooks."""
        total = sum(len(p.detect.promql) for p in list_playbooks() if p.detect is not None)
        assert total == 20, f"shipped promql query count changed: {total}"


class TestNoShippedQueryFiresOnACauseItDoesNotDescribe:
    """`pending_resources` shipped `kube_pod_status_unschedulable == 1` marked AMBIGUOUS. That
    series is 1 for EVERY pod the scheduler could not place — taint, affinity, unbound PVC or
    capacity — so with PROMQL_DETECTION_ENABLED on it fired PendingInsufficientResources for
    causes its own text ("Insufficient cpu/memory") does not describe. The metric carries no
    reason label, so no rewrite of the query can narrow it; the Event predicate (FailedScheduling
    + "Insufficient (cpu|memory)") is the exact condition and is kept."""

    @staticmethod
    def _by_name(name):
        return next(p for p in list_playbooks() if p.name == name)

    def test_pending_resources_carries_no_promql(self):
        pb = self._by_name("PendingInsufficientResources")
        assert pb.detect.promql == ()

    def test_pending_resources_keeps_its_exact_event_predicate(self):
        (pred,) = self._by_name("PendingInsufficientResources").detect.watch_predicates
        assert pred.kind == "Event"
        assert pred.reason_regex.search("FailedScheduling")
        assert pred.message_regex.search("0/3 nodes are available: Insufficient cpu.")
        assert not pred.message_regex.search("node(s) had untolerated taint")

    def test_no_shipped_query_uses_the_causeless_unschedulable_series(self):
        offenders = [p.name for p in list_playbooks() if p.detect is not None
                     and any("kube_pod_status_unschedulable" in q for q in p.detect.promql)]
        assert offenders == []

    def test_no_shipped_playbook_leaves_an_ambiguous_marker_in_its_yaml(self):
        pb_dir = _APP / "agent" / "playbooks"
        marked = [f.name for f in pb_dir.glob("*.yaml")
                  if "AMBIGUOUS" in f.read_text(encoding="utf-8")]
        assert marked == []


def test_the_engine_reads_promql():
    """The inverted tripwire: `DetectorEngine.evaluate_promql` must read each detector's queries."""
    src = (_APP / "detectors" / "engine.py").read_text(encoding="utf-8")
    assert re.search(r"\bdet\.promql\b", src), (
        "the engine no longer reads DetectBlock.promql — if PromQL evaluation was removed, "
        "the docs describing promql as evaluated must be changed back")
    assert "async def evaluate_promql" in src
