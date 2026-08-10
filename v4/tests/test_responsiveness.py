"""Responsiveness — never-silent heartbeat + latency budget (v5 P2, REQ-developer-19)."""
from __future__ import annotations

import asyncio

from app.agent.state import PlanStep
from app.cortex import graph as cx
from app.cortex.responsiveness import PhaseBudget, heartbeat
from langchain_core.messages import AIMessage, ToolMessage


class _Clock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class TestHeartbeat:
    async def test_emits_while_active(self):
        got = []
        async def fake_emit(sid, ev):
            got.append(ev)
        async with heartbeat("s1", "investigating", "still working…", interval=0.01, emit_fn=fake_emit):
            await asyncio.sleep(0.05)  # ~5 intervals
        assert len(got) >= 1
        assert got[0].message == "still working…" and got[0].phase == "investigating"

    async def test_fast_phase_emits_nothing(self):
        got = []
        async def fake_emit(sid, ev):
            got.append(ev)
        async with heartbeat("s1", "investigating", "…", interval=0.05, emit_fn=fake_emit):
            pass
        await asyncio.sleep(0)  # let cancellation settle
        assert got == []

    async def test_zero_interval_disables(self):
        got = []
        async def fake_emit(sid, ev):
            got.append(ev)
        async with heartbeat("s1", "p", "m", interval=0, emit_fn=fake_emit):
            await asyncio.sleep(0.02)
        assert got == []

    async def test_emit_error_is_swallowed(self):
        async def boom_emit(sid, ev):
            raise RuntimeError("emit failed")
        # must not raise out of the context manager
        async with heartbeat("s1", "p", "m", interval=0.01, emit_fn=boom_emit):
            await asyncio.sleep(0.03)


class TestPhaseBudget:
    def test_within_budget_no_warning(self):
        clk = _Clock(100.0)
        b = PhaseBudget(first_signal_s=30, full_s=120, clock=clk)
        b.start()
        clk.t = 110.0
        b.mark_first_signal()
        assert b.warning() == ""

    def test_first_signal_breach(self):
        clk = _Clock(0.0)
        b = PhaseBudget(first_signal_s=30, full_s=120, clock=clk)
        b.start()
        clk.t = 45.0
        b.mark_first_signal()
        assert b.first_signal_breached() is True
        assert "first signal" in b.warning()

    def test_full_breach(self):
        clk = _Clock(0.0)
        b = PhaseBudget(first_signal_s=30, full_s=120, clock=clk)
        b.start()
        clk.t = 200.0
        assert b.full_breached() is True
        assert "full investigation" in b.warning()

    def test_mark_first_signal_is_sticky(self):
        clk = _Clock(0.0)
        b = PhaseBudget(clock=clk)
        b.start()
        clk.t = 5.0
        first = b.mark_first_signal()
        clk.t = 50.0
        second = b.mark_first_signal()   # must not move
        assert first == second == 5.0

    def test_since_uses_known_start(self):
        clk = _Clock(300.0)
        b = PhaseBudget.since(100.0, first_signal_s=30, full_s=120, clock=clk)
        assert b.elapsed() == 200.0 and b.full_breached() is True


def _state(**over):
    base = {
        "messages": [],
        "session_id": "s1",
        "investigation_plan": [],
        "plan_cursor": 0,
    }
    base.update(over)
    return base


class TestGatherToolsWiring:
    async def test_heartbeat_wrapping_is_transparent(self, mocker):
        """Flag on: gather_tools still returns exact tool results (wiring adds no behavior change)."""
        mocker.patch.object(cx, "emit", new=mocker.AsyncMock())
        mocker.patch.object(cx.settings, "CORTEX_V5_ENABLED", True)
        mocker.patch.object(cx.settings, "KI_V5_RESPONSIVENESS", True)
        fake_tool = mocker.AsyncMock()
        fake_tool.ainvoke.return_value = "pods output"
        mocker.patch.dict(cx._TOOLS_BY_NAME, {"run_kubectl": fake_tool})
        msg = AIMessage(content="", tool_calls=[{"name": "run_kubectl", "args": {}, "id": "c1"}])
        plan = [PlanStep(description="list", status="in_progress")]
        out = await cx.gather_tools(_state(messages=[msg], investigation_plan=plan), {})
        assert isinstance(out["messages"][0], ToolMessage)
        assert out["messages"][0].content == "pods output"
        assert out["plan_cursor"] == 1
