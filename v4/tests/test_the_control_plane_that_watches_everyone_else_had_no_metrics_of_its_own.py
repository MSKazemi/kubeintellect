"""Domain metrics — `app/core/metrics.py` and its two wiring points.

`/metrics` served twenty families before this existed and every one came from the FastAPI
instrumentator: request counts, latency, RSS, GC. An operator could see that a request took forty
seconds and not whether it spent them in one model call or eleven kubectl invocations, whether a
tool failed, or whether an approval gate stopped it.

These tests pin the four families and — more importantly — the three properties that make
instrumentation safe to leave in a request path.
"""
from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from app.core import metrics


def _value(name: str, **labels) -> float:
    v = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if v is None else v


def test_the_four_domain_families_exist_and_are_kubeintellects_own():
    assert metrics.enabled() is True
    names = {m.name for m in REGISTRY.collect()}
    for fam in ("kubeintellect_tool_calls", "kubeintellect_tool_duration_seconds",
                "kubeintellect_hitl_interrupts", "kubeintellect_llm_calls",
                "kubeintellect_llm_tokens"):
        assert fam in names, f"{fam} missing from the registry"


def test_a_tool_call_is_counted_by_tool_and_outcome():
    before = _value("kubeintellect_tool_calls_total", tool="t_probe", outcome="ok")
    metrics.record_tool_call("t_probe", "ok", 0.3)
    assert _value("kubeintellect_tool_calls_total", tool="t_probe", outcome="ok") == before + 1


def test_the_duration_histogram_reaches_past_ten_seconds():
    """A kubectl call lands near 0.1 s and an LLM-backed subagent near 60 s. The default buckets
    stop at 10 s, which would file every subagent under +Inf — where latency goes to die."""
    buckets = metrics._TOOL_DURATION._upper_bounds
    assert max(b for b in buckets if b != float("inf")) >= 300
    metrics.record_tool_call("t_slow", "ok", 75.0)
    assert _value("kubeintellect_tool_duration_seconds_bucket", tool="t_slow", le="60.0") == 0.0
    assert _value("kubeintellect_tool_duration_seconds_bucket", tool="t_slow", le="120.0") == 1.0


def test_an_approval_gate_is_not_counted_as_an_error():
    """A gate stopping a destructive action is the product working. Folding it into the error
    rate would make the safety feature the product is sold on look like a defect on a dashboard."""
    err_before = _value("kubeintellect_tool_calls_total", tool="t_gated", outcome="error")
    metrics.record_hitl_interrupt("t_gated")
    assert _value("kubeintellect_hitl_interrupts_total", tool="t_gated") == 1
    assert _value("kubeintellect_tool_calls_total", tool="t_gated", outcome="error") == err_before


def test_llm_usage_is_counted_by_direction():
    ib = _value("kubeintellect_llm_tokens_total", direction="input")
    ob = _value("kubeintellect_llm_tokens_total", direction="output")
    cb = _value("kubeintellect_llm_calls_total")
    metrics.record_llm_usage(input_tokens=120, output_tokens=30, calls=1)
    assert _value("kubeintellect_llm_tokens_total", direction="input") == ib + 120
    assert _value("kubeintellect_llm_tokens_total", direction="output") == ob + 30
    assert _value("kubeintellect_llm_calls_total") == cb + 1


@pytest.mark.parametrize("fn,args", [
    (metrics.record_tool_call, ("t", "ok", 0.1)),
    (metrics.record_hitl_interrupt, ("t",)),
    (metrics.record_llm_usage, ()),
])
def test_a_broken_metrics_backend_cannot_break_a_request(monkeypatch, fn, args):
    """Instrumentation that can take down the thing it measures is worse than none."""
    class Exploding:
        def labels(self, **_kw):
            raise RuntimeError("registry exploded")

        def inc(self, *_a):
            raise RuntimeError("registry exploded")

    for attr in ("_TOOL_CALLS", "_TOOL_DURATION", "_HITL", "_LLM_CALLS", "_LLM_TOKENS"):
        monkeypatch.setattr(metrics, attr, Exploding())
    fn(*args, **({"input_tokens": 5} if fn is metrics.record_llm_usage else {}))


def test_everything_is_a_no_op_when_the_backend_is_absent(monkeypatch):
    monkeypatch.setattr(metrics, "_ENABLED", False)
    monkeypatch.setattr(metrics, "_TOOL_CALLS", None)
    metrics.record_tool_call("t", "ok", 1.0)
    metrics.record_hitl_interrupt("t")
    metrics.record_llm_usage(input_tokens=1, output_tokens=1, calls=1)
    assert metrics.enabled() is False


def test_no_label_comes_from_an_unbounded_set():
    """An unbounded label on a counter is how a metrics endpoint becomes an outage. `tool` comes
    from a fixed registry; session, namespace, cluster and pod must never appear."""
    forbidden = {"session", "session_id", "namespace", "cluster", "cluster_id", "pod", "user",
                 "query", "query_id", "request_id"}
    for m in REGISTRY.collect():
        if not m.name.startswith("kubeintellect_"):
            continue
        for sample in m.samples:
            assert not (set(sample.labels) & forbidden), \
                f"{m.name} carries an unbounded label: {set(sample.labels) & forbidden}"


# --- the wiring ------------------------------------------------------------------------------

def test_the_usage_meter_emits_from_the_one_place_every_model_call_converges():
    from app.core.usage import UsageMeter
    before = _value("kubeintellect_llm_tokens_total", direction="input")
    UsageMeter().add(input_tokens=7, output_tokens=3)
    assert _value("kubeintellect_llm_tokens_total", direction="input") == before + 7


def test_the_meter_emits_outside_its_own_lock():
    """A metrics backend is not something to hold a request-scoped lock across."""
    import inspect

    from app.core.usage import UsageMeter
    src = inspect.getsource(UsageMeter.add)
    lock_line = next(i for i, ln in enumerate(src.splitlines()) if "with self._lock" in ln)
    emit_line = next(i for i, ln in enumerate(src.splitlines()) if "record_llm_usage" in ln)
    indent = len(src.splitlines()[emit_line]) - len(src.splitlines()[emit_line].lstrip())
    lock_indent = len(src.splitlines()[lock_line]) - len(src.splitlines()[lock_line].lstrip())
    assert emit_line > lock_line and indent <= lock_indent, "emission is inside the lock"


def test_the_graph_records_every_tool_outcome_including_the_unknown_tool_path():
    import inspect

    from app.cortex import graph
    src = inspect.getsource(graph.gather_tools)
    assert 'metrics.record_tool_call(name, "unknown_tool")' in src
    assert 'metrics.record_tool_call(name, "ok"' in src
    assert 'metrics.record_tool_call(name, "error"' in src
    assert "metrics.record_hitl_interrupt(name)" in src


def test_the_interrupt_is_still_re_raised_after_being_counted():
    """HITL depends on GraphInterrupt propagating. Counting it must not swallow it."""
    import inspect

    from app.cortex import graph
    lines = inspect.getsource(graph.gather_tools).splitlines()
    i = next(n for n, ln in enumerate(lines) if "record_hitl_interrupt" in ln)
    assert any(ln.strip() == "raise" for ln in lines[i:i + 4]), \
        "the interrupt must still be re-raised"
