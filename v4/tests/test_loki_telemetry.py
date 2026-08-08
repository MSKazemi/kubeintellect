"""Unit tests for evaluation/collectors/loki_collector.parse_telemetry()."""
from __future__ import annotations

import json


from evaluation.models import LokiLogLine, LokiLogs
from evaluation.collectors.loki_collector import parse_telemetry, _parse_log_line


# ── Helpers ───────────────────────────────────────────────────────────────────


def _logs(*lines: LokiLogLine) -> LokiLogs:
    return LokiLogs(
        session_id="s1",
        lines=list(lines),
        error_count=0,
        warning_count=0,
        kubectl_commands=[],
        hitl_events=[],
        available=True,
    )


def _line(message: str, fields: dict | None = None, *, ts: int = 0) -> LokiLogLine:
    return LokiLogLine(timestamp_ns=ts, level="INFO", message=message, fields=fields or {})


def _json_line(payload: dict, *, ts: int = 0) -> LokiLogLine:
    """Simulate a JSON-formatted log line (as Loki stores them)."""
    raw = json.dumps(payload)
    level, message, fields = _parse_log_line(raw)
    return LokiLogLine(timestamp_ns=ts, level=level, message=message, fields=fields)


# ── snapshot_complete ─────────────────────────────────────────────────────────

class TestSnapshotComplete:
    def test_extracts_cluster_id(self):
        line = _line(
            "context_fetcher: snapshot_complete",
            {"cluster_id": "kind-local", "snapshot_pod_count": 5,
             "snapshot_has_issues": True, "snapshot_has_warnings": False},
        )
        tel = parse_telemetry(_logs(line))
        assert tel.cluster_id == "kind-local"

    def test_extracts_pod_count(self):
        line = _line(
            "context_fetcher: snapshot_complete",
            {"snapshot_pod_count": 12, "snapshot_has_issues": False,
             "snapshot_has_warnings": False},
        )
        tel = parse_telemetry(_logs(line))
        assert tel.snapshot_pod_count == 12

    def test_extracts_matched_playbooks(self):
        line = _line(
            "context_fetcher: snapshot_complete",
            {"matched_playbooks": ["CrashLoopBackOff", "OOMKilled"]},
        )
        tel = parse_telemetry(_logs(line))
        assert tel.matched_playbooks == ["CrashLoopBackOff", "OOMKilled"]

    def test_no_matched_playbooks_stays_empty(self):
        line = _line("context_fetcher: snapshot_complete", {"snapshot_pod_count": 1})
        tel = parse_telemetry(_logs(line))
        assert tel.matched_playbooks == []


# ── routing_decision ──────────────────────────────────────────────────────────

class TestRoutingDecision:
    def test_extracts_route(self):
        line = _line(
            "coordinator: routing_decision route=direct elapsed_ms=123",
            {"routing_decision": "direct"},
        )
        tel = parse_telemetry(_logs(line))
        assert tel.routing_path == ["direct"]

    def test_multiple_routes_appended(self):
        lines = [
            _line("coordinator: routing_decision", {"routing_decision": "TARGETED"}),
            _line("coordinator: routing_decision", {"routing_decision": "RCA"}),
        ]
        tel = parse_telemetry(_logs(*lines))
        assert tel.routing_path == ["TARGETED", "RCA"]

    def test_missing_field_skipped(self):
        line = _line("coordinator: routing_decision", {})
        tel = parse_telemetry(_logs(line))
        assert tel.routing_path == []


# ── hitl_classification ───────────────────────────────────────────────────────

class TestHITLClassification:
    def test_extracts_hitl_entry(self):
        line = _line(
            "run_kubectl: hitl_classification verb=delete risk=high",
            {"hitl_verb": "delete", "hitl_risk_level": "high", "hitl_cmd": "kubectl delete pod foo"},
        )
        tel = parse_telemetry(_logs(line))
        assert len(tel.hitl_classifications) == 1
        assert tel.hitl_classifications[0]["hitl_verb"] == "delete"
        assert tel.hitl_classifications[0]["hitl_risk_level"] == "high"

    def test_empty_fields_not_appended(self):
        line = _line("run_kubectl: hitl_classification", {})
        tel = parse_telemetry(_logs(line))
        assert tel.hitl_classifications == []


# ── investigation_plan_emitted ────────────────────────────────────────────────

class TestInvestigationPlan:
    def test_extracts_steps(self):
        steps = ["Check pod status", "Inspect events", "Review logs"]
        line = _line(
            "investigation_plan_emitted session=abc step_count=3",
            {"session_id": "abc", "steps": steps},
        )
        tel = parse_telemetry(_logs(line))
        assert tel.investigation_plan == steps

    def test_empty_steps_not_set(self):
        line = _line("investigation_plan_emitted session=abc step_count=0", {"steps": []})
        tel = parse_telemetry(_logs(line))
        assert tel.investigation_plan == []

    def test_last_plan_wins(self):
        first = _line("investigation_plan_emitted", {"steps": ["step A"]})
        second = _line("investigation_plan_emitted", {"steps": ["step B", "step C"]})
        tel = parse_telemetry(_logs(first, second))
        assert tel.investigation_plan == ["step B", "step C"]


# ── rca_outcome_written ───────────────────────────────────────────────────────

class TestRcaOutcomeWritten:
    def test_extracts_reflexion_write_entry(self):
        line = _line(
            "rca_outcome_written session=abc confidence=0.95",
            {"session_id": "abc", "cluster_id": "kind-local",
             "namespace": "staging", "confidence": 0.95, "verified": True},
        )
        tel = parse_telemetry(_logs(line))
        assert len(tel.reflexion_writes) == 1
        entry = tel.reflexion_writes[0]
        assert entry["cluster_id"] == "kind-local"
        assert entry["namespace"] == "staging"
        assert entry["confidence"] == 0.95

    def test_multiple_writes_accumulated(self):
        lines = [
            _line("rca_outcome_written", {"cluster_id": "c1", "confidence": 0.9}),
            _line("rca_outcome_written", {"cluster_id": "c1", "confidence": 0.8}),
        ]
        tel = parse_telemetry(_logs(*lines))
        assert len(tel.reflexion_writes) == 2

    def test_empty_fields_not_appended(self):
        line = _line("rca_outcome_written", {})
        tel = parse_telemetry(_logs(line))
        assert tel.reflexion_writes == []


# ── failure_hints_loaded ──────────────────────────────────────────────────────

class TestFailureHintsLoaded:
    def test_extracts_hint_read_entry(self):
        line = _line(
            "failure_hints_loaded count=3",
            {"hint_count": 3, "cluster_id": "kind-local"},
        )
        tel = parse_telemetry(_logs(line))
        assert len(tel.reflexion_reads) == 1
        assert tel.reflexion_reads[0]["hint_count"] == 3
        assert tel.reflexion_reads[0]["cluster_id"] == "kind-local"

    def test_empty_fields_not_appended(self):
        line = _line("failure_hints_loaded count=0", {})
        tel = parse_telemetry(_logs(line))
        assert tel.reflexion_reads == []


# ── kubectl_error_interpreted ─────────────────────────────────────────────────

class TestKubectlErrorHints:
    def test_extracts_pattern_name(self):
        line = _line(
            "kubectl_error_interpreted pattern=image_pull_backoff exit_code=1",
            {"pattern": "image_pull_backoff", "exit_code": 1},
        )
        tel = parse_telemetry(_logs(line))
        assert "image_pull_backoff" in tel.kubectl_error_hints

    def test_multiple_hints_accumulated(self):
        lines = [
            _line("kubectl_error_interpreted", {"pattern": "oom_killed"}),
            _line("kubectl_error_interpreted", {"pattern": "crashloop"}),
        ]
        tel = parse_telemetry(_logs(*lines))
        assert len(tel.kubectl_error_hints) == 2
        assert "oom_killed" in tel.kubectl_error_hints
        assert "crashloop" in tel.kubectl_error_hints

    def test_missing_pattern_not_appended(self):
        line = _line("kubectl_error_interpreted", {})
        tel = parse_telemetry(_logs(line))
        assert tel.kubectl_error_hints == []


# ── Empty / unrecognised logs ─────────────────────────────────────────────────

class TestEmptyInput:
    def test_no_lines_returns_empty_telemetry(self):
        tel = parse_telemetry(_logs())
        assert tel.cluster_id is None
        assert tel.matched_playbooks == []
        assert tel.investigation_plan == []
        assert tel.routing_path == []
        assert tel.reflexion_writes == []
        assert tel.reflexion_reads == []
        assert tel.kubectl_error_hints == []

    def test_unrecognised_messages_ignored(self):
        line = _line("some_other_event", {"foo": "bar"})
        tel = parse_telemetry(_logs(line))
        assert tel.routing_path == []
        assert tel.matched_playbooks == []


# ── _parse_log_line helper ────────────────────────────────────────────────────

class TestParseLogLine:
    def test_json_line_extracts_level_and_message(self):
        raw = json.dumps({"level": "WARNING", "message": "something bad", "session_id": "s1"})
        level, msg, fields = _parse_log_line(raw)
        assert level == "WARNING"
        assert msg == "something bad"
        assert fields["session_id"] == "s1"

    def test_plain_error_line(self):
        level, msg, fields = _parse_log_line("ERROR something went wrong")
        assert level == "ERROR"
        assert fields == {}

    def test_plain_info_line(self):
        level, msg, fields = _parse_log_line("coordinator: routing_decision route=direct")
        assert level == "INFO"

    def test_json_uses_levelname_fallback(self):
        raw = json.dumps({"levelname": "debug", "message": "trace"})
        level, _, _ = _parse_log_line(raw)
        assert level == "DEBUG"
