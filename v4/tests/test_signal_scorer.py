"""Unit tests for evaluation/scorers/signal_scorer.py."""
from __future__ import annotations

from evaluation.models import EvalResult, LangfuseTrace, LokiLogs

# ── Helpers ───────────────────────────────────────────────────────────────────


def _result(
    *,
    final_text: str = "Here are the pods.",
    had_error: bool = False,
    error_message: str | None = None,
    tool_calls: list[str] | None = None,
    tool_errors: list[str] | None = None,
    had_hitl: bool = False,
    hitl_commands: list[str] | None = None,
    latency_ms: float = 5_000.0,
) -> EvalResult:
    return EvalResult(
        session_id="s1",
        query_id="q1",
        messages=[],
        final_text=final_text,
        events=[],
        had_hitl=had_hitl,
        had_error=had_error,
        error_message=error_message,
        latency_ms=latency_ms,
        start_ts=0.0,
        end_ts=1.0,
        tool_calls=tool_calls or [],
        tool_errors=tool_errors or [],
        status_phases=[],
        hitl_commands=hitl_commands or [],
    )


def _query(
    *,
    category: str = "pod_health",
    expected_tools: list[str] | None = None,
    expects_write: bool = False,
    messages: list[dict] | None = None,
) -> dict:
    return {
        "id": "q1",
        "category": category,
        "expected_tools": expected_tools or [],
        "expects_write": expects_write,
        "messages": messages or [{"role": "user", "content": "list pods"}],
    }


def _trace(*, has_error: bool = False, error_messages: list[str] | None = None) -> LangfuseTrace:
    return LangfuseTrace(
        trace_id="t1",
        session_id="s1",
        total_tokens=500,
        total_latency_ms=5_000.0,
        generations=[],
        tool_spans=[],
        has_error=has_error,
        error_messages=error_messages or [],
        available=True,
    )


def _loki(*, error_count: int = 0) -> LokiLogs:
    return LokiLogs(
        session_id="s1",
        lines=[],
        error_count=error_count,
        warning_count=0,
        kubectl_commands=[],
        hitl_events=[],
        available=True,
    )


def _score(result=None, query=None, trace=None, logs=None, cluster_resolved=None):
    from evaluation.scorers.signal_scorer import score_signals
    return score_signals(
        query=query or _query(),
        result=result or _result(),
        trace=trace,
        logs=logs,
        cluster_resolved=cluster_resolved,
    )


# ── Completion ────────────────────────────────────────────────────────────────

class TestCompletion:
    def test_final_text_without_error_is_1(self):
        s = _score(result=_result(final_text="All pods running.", had_error=False))
        assert s.completion == 1.0

    def test_had_error_is_0(self):
        s = _score(result=_result(final_text="", had_error=True))
        assert s.completion == 0.0

    def test_empty_final_text_is_0(self):
        s = _score(result=_result(final_text="", had_error=False))
        assert s.completion == 0.0

    def test_error_note_appended(self):
        s = _score(result=_result(had_error=True, error_message="stream timeout"))
        assert any("stream timeout" in n for n in s.notes)


# ── Tool correctness ──────────────────────────────────────────────────────────

class TestToolCorrectness:
    def test_no_expectation_gives_1(self):
        s = _score(query=_query(expected_tools=[]))
        assert s.tool_correctness == 1.0

    def test_all_expected_tools_called(self):
        s = _score(
            query=_query(expected_tools=["run_kubectl", "query_prometheus"]),
            result=_result(tool_calls=["run_kubectl", "query_prometheus"]),
        )
        assert s.tool_correctness == 1.0

    def test_half_expected_tools_called(self):
        s = _score(
            query=_query(expected_tools=["run_kubectl", "query_prometheus"]),
            result=_result(tool_calls=["run_kubectl"]),
        )
        assert s.tool_correctness == 0.5

    def test_no_expected_tools_called(self):
        s = _score(
            query=_query(expected_tools=["run_kubectl"]),
            result=_result(tool_calls=[]),
        )
        assert s.tool_correctness == 0.0

    def test_extra_tools_noted_but_not_penalised(self):
        s = _score(
            query=_query(expected_tools=["run_kubectl"]),
            result=_result(tool_calls=["run_kubectl", "query_loki"]),
        )
        assert s.tool_correctness == 1.0
        assert any("Extra" in n for n in s.notes)


# ── Error-free ────────────────────────────────────────────────────────────────

class TestErrorFree:
    def test_clean_run_is_1(self):
        s = _score(result=_result(), trace=_trace(), logs=_loki())
        assert s.error_free == 1.0

    def test_had_error_subtracts_heavily(self):
        s = _score(result=_result(had_error=True))
        assert s.error_free < 0.6

    def test_tool_errors_reduce_score(self):
        s = _score(result=_result(tool_errors=["run_kubectl"]))
        assert s.error_free < 1.0

    def test_loki_errors_reduce_score(self):
        s = _score(result=_result(), logs=_loki(error_count=2))
        assert s.error_free < 1.0

    def test_score_floored_at_zero(self):
        s = _score(
            result=_result(had_error=True, tool_errors=["a", "b", "c", "d", "e", "f", "g"]),
            trace=_trace(has_error=True),
            logs=_loki(error_count=10),
        )
        assert s.error_free == 0.0


# ── HITL discipline ───────────────────────────────────────────────────────────

class TestHITLDiscipline:
    def test_no_hitl_is_1(self):
        s = _score(result=_result(had_hitl=False))
        assert s.hitl_discipline == 1.0

    def test_unexpected_hitl_is_half(self):
        s = _score(
            query=_query(messages=[{"role": "user", "content": "list all pods"}]),
            result=_result(had_hitl=True, hitl_commands=["kubectl delete pod foo"]),
        )
        assert s.hitl_discipline == 0.5

    def test_expected_hitl_for_write_query_is_1(self):
        s = _score(
            query=_query(
                messages=[{"role": "user", "content": "delete the stuck pod"}],
            ),
            result=_result(had_hitl=True, hitl_commands=["kubectl delete pod foo"]),
        )
        assert s.hitl_discipline == 1.0

    def test_expected_hitl_via_flag(self):
        s = _score(
            query=_query(expects_write=True),
            result=_result(had_hitl=True),
        )
        assert s.hitl_discipline == 1.0


# ── Latency ───────────────────────────────────────────────────────────────────

class TestLatency:
    def test_zero_latency_is_1(self):
        s = _score(result=_result(latency_ms=0.0), query=_query(category="pod_health"))
        assert s.latency == 1.0

    def test_at_baseline_gives_zero(self):
        # pod_health baseline = 25_000 ms
        s = _score(result=_result(latency_ms=25_000.0), query=_query(category="pod_health"))
        assert s.latency == 0.0

    def test_over_baseline_floored_at_zero(self):
        s = _score(result=_result(latency_ms=50_000.0), query=_query(category="pod_health"))
        assert s.latency == 0.0

    def test_half_baseline(self):
        # pod_health baseline = 25_000 ms; half → 0.5
        s = _score(result=_result(latency_ms=12_500.0), query=_query(category="pod_health"))
        assert abs(s.latency - 0.5) < 0.01

    def test_unknown_category_uses_default_baseline(self):
        s = _score(result=_result(latency_ms=0.0), query=_query(category="unknown_cat"))
        assert s.latency == 1.0


# ── Cluster resolved ──────────────────────────────────────────────────────────

class TestClusterResolved:
    def test_none_when_not_provided(self):
        s = _score(cluster_resolved=None)
        assert s.cluster_resolved is None

    def test_true_gives_1(self):
        s = _score(cluster_resolved=True)
        assert s.cluster_resolved == 1.0

    def test_false_gives_0(self):
        s = _score(cluster_resolved=False)
        assert s.cluster_resolved == 0.0

    def test_false_adds_note(self):
        s = _score(cluster_resolved=False)
        assert any("NOT resolved" in n for n in s.notes)
