"""`promql:` in a detect block is recorded, never evaluated — so it must not look valid.

The playbook schema advertises three predicate types under `detect:`. Two of them run:
`DetectorEngine.process()` matches `watch_predicates` against the observation stream, and the
periodic tick evaluates `trend_predicates` (ADR-010). **Nothing has ever read
`DetectBlock.promql`** — verified 2026-08-20 by scanning every `.py` in the server package, and
re-asserted mechanically by `test_no_code_path_evaluates_promql` below.

It was nonetheless treated as a real predicate everywhere else:

* `parse_detect_block` accepted it as sufficient to make a block valid;
* `_is_detect_block` counted it when deciding a DB row was a recompilable detector;
* `authoring.py` told the NL-authoring model *"promql: list of instant PromQL strings
  (firing = non-empty result)"* — so ADR-012 could mint a promql-only shadow candidate that
  validates, is staged for human promotion, accrues no precision because it cannot fire, and
  would still never fire once promoted;
* `Finding.source` documents `"promql"` as one of its values, which is unreachable.

**Scope, stated honestly.** All 21 `promql:` queries in the shipped playbooks sit alongside real
`watch_predicates`, so no shipped detector is dead and nothing that fires today stops firing. What
was false is the extra coverage those queries appear to claim, and the validity of a promql-only
detector. This is the same shape as the `kind:` trap already documented in
`docs/agent-behaviors.md` — parses, loads, counts toward the total, passes the schema check, and
matches nothing, ever.

These tests do not assert that PromQL evaluation *should not* exist. If it is built,
`test_no_code_path_evaluates_promql` is the tripwire that says "update this file" — which is the
point of it.
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


class TestAPromqlOnlyDetectorIsRejected:
    def test_parse_returns_none(self):
        assert parse_detect_block("PromqlOnly", dict(_PROMQL)) is None

    def test_it_says_why(self, caplog):
        with caplog.at_level("WARNING"):
            parse_detect_block("PromqlOnly", dict(_PROMQL))
        assert "not evaluated" in caplog.text and "never fire" in caplog.text

    def test_a_db_row_with_only_promql_is_not_a_detect_block(self):
        assert _is_detect_block(dict(_PROMQL)) is False

    def test_nl_authoring_rejects_it_with_a_reason(self):
        from app.detectors.authoring import validate_detect_block
        block, errors = validate_detect_block(dict(_PROMQL), "PromqlOnly")
        assert block is None
        assert errors and "never evaluated" in errors[0], errors

    def test_the_authoring_prompt_does_not_claim_it_fires(self):
        src = (_APP / "detectors" / "authoring.py").read_text()
        assert "firing = non-empty result" not in src
        assert "NOT evaluated" in src


class TestTheEvaluatedTypesAreUnaffected:
    """Guard on the guard — the fix must not narrow what really works."""

    def test_watch_only_still_compiles(self):
        assert parse_detect_block("WatchOnly", dict(_WATCH)) is not None

    def test_watch_plus_promql_still_compiles_and_keeps_the_queries(self):
        block = parse_detect_block("Both", {**_WATCH, **_PROMQL})
        assert block is not None
        assert block.promql == tuple(_PROMQL["promql"]), "the queries are still recorded"

    def test_trend_only_still_compiles(self):
        block = parse_detect_block("TrendOnly", {"trend_predicates": [
            {"metric": "node_filesystem_avail_bytes", "threshold": 0.0}]})
        assert block is not None

    def test_a_db_row_with_watch_predicates_is_still_a_detect_block(self):
        assert _is_detect_block(dict(_WATCH)) is True

    def test_a_consolidation_learned_row_is_still_skipped(self):
        assert _is_detect_block({"derived_from_playbooks": ["X"], "pattern": "y"}) is False


class TestEveryShippedDetectorCanActuallyFire:
    """The invariant that stops a future promql-only playbook from shipping dead."""

    @pytest.mark.parametrize("pb", [p for p in list_playbooks() if p.detect is not None],
                             ids=lambda p: p.name)
    def test_it_has_at_least_one_evaluated_predicate(self, pb):
        assert pb.detect.watch_predicates or pb.detect.trend_predicates, (
            f"{pb.name} declares only promql, which is never evaluated")

    def test_the_shipped_promql_queries_are_still_carried(self):
        """Not deleted — recorded, and the count is pinned so a change is deliberate."""
        total = sum(len(p.detect.promql) for p in list_playbooks() if p.detect is not None)
        assert total == 21, f"shipped promql query count changed: {total}"


def test_no_code_path_evaluates_promql():
    """The finding itself, asserted mechanically.

    If PromQL evaluation is implemented, this test fails — deliberately. Update it together
    with the docs that currently say the queries are declarative.
    """
    allowed = {  # files that may mention .promql without evaluating a detector's queries
        "detectors/models.py",          # parses and stores it
        "memory/consolidation.py",      # exports it verbatim to a DB row
        "detectors/authoring.py",       # documents it to the authoring model
        "tools/prometheus_tool.py",     # a *parameter* name on the generic query helper
        "detectors/agentic_gpu_collector.py",
        "sensorium/observations.py",    # observation field docs
    }
    readers = []
    for path in _APP.rglob("*.py"):
        rel = str(path.relative_to(_APP))
        if rel in allowed:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\bdet(ector)?\.promql\b|\bblock\.promql\b|\.promql\b", line):
                readers.append(f"{rel}:{n}: {line.strip()}")
    assert readers == [], (
        "something now reads DetectBlock.promql — if evaluation was implemented, this file and "
        "the docs describing promql as declarative must be updated:\n" + "\n".join(readers))
