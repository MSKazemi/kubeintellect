"""Unit tests for replay-fixture export (v5 specs/04) + the end-to-end tie-in
from OTel mutation spans → fixtures → Wilson-LCB promotion decision."""

from __future__ import annotations

from app.autonomy.promotion_fixtures import (
    POSTCONDITION_KIND,
    export_events,
    redact_span_row,
)
from app.autonomy.promotion_stats import evaluate_promotion
from app.db import otel_spans


def _mut_row(episode_id, seq, action, day, *, held, critical=False, incident_type="generic"):
    p = otel_spans.mutation_span_payload(
        episode_id, seq, action=action, hypothesis_span_ids=["h"], evidence_span_ids=["e"],
    )
    row = {"kind": otel_spans.SPAN_KIND, "episode_id": episode_id, "seq": seq,
           "payload": p, "created_day": day}
    outcome = {"kind": POSTCONDITION_KIND, "episode_id": episode_id, "seq": seq + 1,
               "payload": {"mutation_span_id": p["span_id"], "held": held,
                           "critical": critical, "incident_type": incident_type}}
    return row, outcome


def test_export_groups_by_action_class_and_maps_outcomes():
    rows = []
    for i in range(3):
        m, o = _mut_row(f"ep-{i}", 0, "scale", day=float(i), held=True)
        rows += [m, o]
    m, o = _mut_row("ep-x", 0, "restart", day=0.0, held=False)
    rows += [m, o]

    by_class = export_events(rows)
    assert set(by_class) == {"scale", "restart"}
    assert len(by_class["scale"]) == 3
    assert all(e.success for e in by_class["scale"])
    assert by_class["restart"][0].success is False


def test_absent_postcondition_is_conservative_failure():
    m, _o = _mut_row("ep-1", 0, "scale", day=0.0, held=True)
    by_class = export_events([m])  # outcome row omitted
    assert by_class["scale"][0].success is False


def test_export_is_deterministic():
    rows = []
    for i in range(5):
        m, o = _mut_row(f"ep-{i}", 0, "scale", day=float(i), held=True)
        rows += [m, o]
    assert export_events(list(rows)) == export_events(list(reversed(rows)))


def test_redaction_drops_everything_but_promotion_fields():
    p = otel_spans.mutation_span_payload("ep-1", 0, action="scale",
                                         hypothesis_span_ids=["h"], evidence_span_ids=["e"])
    p["attributes"]["secret_note"] = "do-not-export password=hunter2"
    row = {"kind": otel_spans.SPAN_KIND, "episode_id": "ep-1", "seq": 0, "payload": p}
    safe = redact_span_row(row)
    assert "secret_note" not in safe["payload"]["attributes"]
    assert safe["payload"]["attributes"]["ki.action"] == "scale"
    assert "ki.links.hypothesis" not in safe["payload"]["attributes"]  # link ids dropped too


def test_end_to_end_spans_to_fixtures_to_promotion_decision():
    """40 clean, diverse, aged scale mutations should clear L2->L3 through the whole chain."""
    rows = []
    for i in range(40):
        m, o = _mut_row(
            f"ep-{i}", 0, "scale",
            day=(40.0 * i / 39),                 # spans 40 days
            held=True,
            incident_type=["a", "b", "c", "d"][i % 4],
        )
        rows += [m, o]
    by_class = export_events(rows)
    decision = evaluate_promotion("L2->L3", by_class["scale"], now_days=40.0)
    assert decision.promote is True
    assert decision.reasons == []


def test_end_to_end_a_critical_outcome_blocks_promotion():
    rows = []
    for i in range(40):
        m, o = _mut_row(f"ep-{i}", 0, "scale", day=(40.0 * i / 39), held=True,
                        critical=(i == 5), incident_type=["a", "b", "c", "d"][i % 4])
        rows += [m, o]
    by_class = export_events(rows)
    decision = evaluate_promotion("L2->L3", by_class["scale"], now_days=40.0)
    assert decision.promote is False
    assert any("critical" in r for r in decision.reasons)
