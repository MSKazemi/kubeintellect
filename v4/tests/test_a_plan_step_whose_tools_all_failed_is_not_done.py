"""A tool batch in which everything failed still ticked the plan step green.

`gather_tools._run_one` deliberately turns a tool exception into an ordinary `ToolMessage`
("Tool error: …") so the model can read and react to it, and an unrecognised tool name into
"Unknown tool: …". Both return normally. The plan transition underneath then ran
`model_copy(update={"status": "done"})` unconditionally — so the batch returning was the
whole test, and the CLI renders `done` as a green ✓.

Measured 2026-08-24, before the fix: a two-call batch where one tool raised
`connection refused` and the other did not exist produced `status='done'` and the icon
`[plan.done]✓[/plan.done]` on the step "Check pod events".
"""
import os

import pytest

os.environ.setdefault("OPENAI_API_KEY", "sk-test")

import app.cortex.graph as G  # noqa: E402
from app.agent.state import PlanStep  # noqa: E402
from kube_q.cli.theme import PLAN_ICONS  # noqa: E402

KUBECTL = {"id": "1", "name": "run_kubectl", "args": {"command": "get pods"}}
BOGUS = {"id": "2", "name": "no_such_tool", "args": {}}


class Boom:
    name = "run_kubectl"

    async def ainvoke(self, args, config):
        raise RuntimeError("connection refused")


class Fine:
    name = "run_kubectl"

    def __init__(self): self.calls = 0

    async def ainvoke(self, args, config):
        self.calls += 1
        return type("R", (), {"content": "NAME  READY\nweb  1/1"})()


@pytest.fixture
def run(mocker):
    async def _noemit(*a, **k): return None

    mocker.patch.object(G, "emit", _noemit)

    async def _run(tools, calls, plan=None, cursor=0):
        plan = plan or [PlanStep(description="Check pod events", status="in_progress"),
                        PlanStep(description="Check logs")]
        mocker.patch.object(G, "_TOOLS_BY_NAME", tools)

        class M:
            tool_calls = calls

        return await G.gather_tools(
            {"investigation_plan": plan, "plan_cursor": cursor,
             "session_id": "s", "messages": [M()]}, {})

    return _run


class TestAFailedStepSaysSo:
    async def test_a_batch_where_every_tool_failed_is_not_done(self, run):
        out = await run({"run_kubectl": Boom()}, [KUBECTL, BOGUS])
        assert out["investigation_plan"][0].status == "failed"

    async def test_a_batch_where_every_tool_succeeded_is_done(self, run):
        # Vacuity guard: a transition that marked everything "failed" would pass the
        # test above and be exactly as useless as the one it replaced.
        out = await run({"run_kubectl": Fine()}, [KUBECTL])
        assert out["investigation_plan"][0].status == "done"

    async def test_an_unknown_tool_alone_is_a_failure(self, run):
        out = await run({}, [BOGUS])
        assert out["investigation_plan"][0].status == "failed"

    async def test_a_partial_batch_is_still_done(self, run):
        # Deliberate: the step did gather evidence. The log names what failed.
        out = await run({"run_kubectl": Fine()}, [KUBECTL, BOGUS])
        assert out["investigation_plan"][0].status == "done"

    async def test_the_tools_still_run_and_their_errors_still_reach_the_model(self, run):
        # Non-vacuity spy on the other half: the fix must not have stopped the batch, and
        # the error text must still be visible to the model, not swallowed into a status.
        tool = Fine()
        out = await run({"run_kubectl": tool}, [KUBECTL, BOGUS])
        assert tool.calls == 1
        assert any("Unknown tool: no_such_tool" in m.content for m in out["messages"])

    async def test_a_failed_step_still_advances_the_cursor(self, run):
        # A plan that stops advancing on the first failure hangs the live view forever.
        out = await run({"run_kubectl": Boom()}, [KUBECTL])
        assert out["plan_cursor"] == 1
        assert out["investigation_plan"][1].status == "in_progress"

    async def test_an_empty_batch_does_not_invent_a_failure(self, run):
        out = await run({}, [])
        assert out["investigation_plan"][0].status == "done"


class TestTheRendererHasAGlyphForIt:
    def test_failed_does_not_fall_through_to_the_pending_dot(self):
        # renderer.py resolves an unknown status to PLAN_ICONS["pending"], so a new state
        # with no entry renders as an untouched step — quieter than the bug it replaced.
        assert "failed" in PLAN_ICONS
        assert PLAN_ICONS["failed"] != PLAN_ICONS["pending"]
        assert PLAN_ICONS["failed"] != PLAN_ICONS["done"]

    def test_the_glyph_is_not_a_tick(self):
        assert "✓" not in PLAN_ICONS["failed"]
        assert "✗" in PLAN_ICONS["failed"]

    def test_every_declared_status_can_be_rendered(self):
        # The model's Literal and the icon table are two lists that must not drift apart.
        import typing

        declared = set(typing.get_args(PlanStep.model_fields["status"].annotation))
        assert declared <= set(PLAN_ICONS), f"no icon for {declared - set(PLAN_ICONS)}"

    def test_the_style_resolves_in_both_themes(self):
        from rich.console import Console

        from kube_q.cli import theme as T
        for th in (T._COLOURED, T._NEUTRAL):
            out = Console(theme=th, force_terminal=False, width=40, file=open(os.devnull, "w"))
            out.print(PLAN_ICONS["failed"])  # raises on a style the theme does not define


class TestTheFailureIsAlsoInTheLog:
    async def test_the_warning_names_the_tools_and_the_reason(self, run, caplog):
        with caplog.at_level("WARNING"):
            await run({"run_kubectl": Boom()}, [KUBECTL, BOGUS])
        msg = "\n".join(r.message for r in caplog.records)
        assert "2 of 2 tool call(s) failed" in msg
        assert "connection refused" in msg
        assert "no_such_tool: unknown tool" in msg
        assert "'failed'" in msg

    async def test_a_clean_batch_logs_no_warning(self, run, caplog):
        # Vacuity guard: a warning on every batch is a warning on none of them.
        with caplog.at_level("WARNING"):
            await run({"run_kubectl": Fine()}, [KUBECTL])
        assert not [r for r in caplog.records if "tool call(s) failed" in r.message]

    async def test_a_partial_batch_still_warns_even_though_the_step_is_done(self, run, caplog):
        with caplog.at_level("WARNING"):
            await run({"run_kubectl": Fine()}, [KUBECTL, BOGUS])
        msg = "\n".join(r.message for r in caplog.records)
        assert "1 of 2 tool call(s) failed" in msg
        assert "'done'" in msg
