"""The server reported no token usage at all, and a whole campaign's cost data was lost.

Nothing in `app/api/` or `app/cortex/` read `usage_metadata`, and the SSE stream carried no usage
frame, so a client had no way to learn what a request cost. All 72 predictions of the 2026-08-24
evaluation recorded `tokens == 0`; the efficiency axis was written off, and the real figures were
recovered months later only by joining archived Langfuse traces on `service.instance.id`. The data
existed the whole time — it never came back down the wire.

The tests below pin the four properties that make that recoverable at the source:

  * usage is summed across *every* model call a request makes, including ones made in nested
    asyncio tasks (the subagent fan-out), which is where a ContextVar gets this wrong;
  * the meter is attached whether or not Langfuse tracing is on;
  * the provider's shape does not matter — three known layouts are all understood;
  * "called the model and got no counts" stays distinguishable from "cheap request".
"""

from __future__ import annotations

import asyncio
import types

import pytest

from app.core import usage


@pytest.fixture(autouse=True)
def _fresh_meter():
    usage._meter_var.set(None)
    yield
    usage._meter_var.set(None)


def _llm_result_modern(inp: int, out: int):
    """The provider-neutral LangChain shape: generation.message.usage_metadata."""
    message = types.SimpleNamespace(usage_metadata={"input_tokens": inp, "output_tokens": out})
    gen = types.SimpleNamespace(message=message)
    return types.SimpleNamespace(generations=[[gen]], llm_output=None)


def _llm_result_openai(inp: int, out: int):
    """What the OpenAI/Azure integrations have long emitted."""
    return types.SimpleNamespace(
        generations=[[types.SimpleNamespace(message=None)]],
        llm_output={"token_usage": {"prompt_tokens": inp, "completion_tokens": out}},
    )


def _llm_result_raw(inp: int, out: int):
    """The raw provider payload, passed straight through."""
    return types.SimpleNamespace(
        generations=[],
        llm_output={"usage": {"input_tokens": inp, "output_tokens": out}},
    )


class TestEveryProviderShapeIsUnderstood:
    @pytest.mark.parametrize(
        "build", [_llm_result_modern, _llm_result_openai, _llm_result_raw]
    )
    def test_counts_are_extracted(self, build):
        usage.start_request_meter()
        usage.TokenMeterCallback().on_llm_end(build(120, 30))
        assert usage.current_usage() == {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "llm_calls": 1,
        }

    def test_an_unrecognised_shape_still_counts_the_call(self):
        """Zero tokens on a real call is an instrumentation gap, not a free request."""
        usage.start_request_meter()
        usage.TokenMeterCallback().on_llm_end(types.SimpleNamespace(generations=[], llm_output={}))
        got = usage.current_usage()
        assert got["total_tokens"] == 0
        assert got["llm_calls"] == 1, (
            "llm_calls is what separates 'the model reported nothing' from 'no model was called'"
        )

    def test_accounting_never_breaks_the_request(self):
        class Exploding:
            @property
            def generations(self):
                raise RuntimeError("provider object is not what we assumed")

        usage.start_request_meter()
        usage.TokenMeterCallback().on_llm_end(Exploding())  # must not raise
        assert usage.current_usage()["total_tokens"] == 0


class TestUsageIsSummedAcrossTheWholeRequest:
    def test_many_calls_accumulate(self):
        usage.start_request_meter()
        cb = usage.TokenMeterCallback()
        for _ in range(5):
            cb.on_llm_end(_llm_result_modern(100, 20))
        got = usage.current_usage()
        assert got == {
            "prompt_tokens": 500,
            "completion_tokens": 100,
            "total_tokens": 600,
            "llm_calls": 5,
        }

    def test_tokens_spent_inside_nested_tasks_are_not_lost(self):
        """The subagent fan-out runs as tasks, and this is exactly where a ContextVar fails.

        `asyncio.create_task` COPIES the context, so a nested task that rebinds the var mutates
        only its own copy and its tokens vanish. Binding one mutable meter and never reassigning
        it is what makes the fan-out's usage visible to the request that spawned it. If someone
        "simplifies" `usage.py` to store an immutable total in the ContextVar, this fails.
        """

        async def scenario():
            usage.start_request_meter()
            cb = usage.TokenMeterCallback()

            async def branch(n: int) -> None:
                await asyncio.sleep(0)
                cb.on_llm_end(_llm_result_modern(n, 1))

            cb.on_llm_end(_llm_result_modern(10, 1))          # coordinator
            await asyncio.gather(*(branch(100) for _ in range(4)))  # four subagents
            return usage.current_usage()

        got = asyncio.run(scenario())
        assert got["llm_calls"] == 5, "a subagent's call was not counted"
        assert got["prompt_tokens"] == 410, "a subagent's tokens were lost in its task context"

    def test_two_requests_do_not_share_a_meter(self):
        async def one(n: int) -> dict:
            usage.start_request_meter()
            usage.TokenMeterCallback().on_llm_end(_llm_result_modern(n, 0))
            await asyncio.sleep(0)
            return usage.current_usage()

        async def both():
            # Separate tasks => separate context copies => separate meters.
            return await asyncio.gather(asyncio.create_task(one(7)),
                                        asyncio.create_task(one(9)))

        a, b = asyncio.run(both())
        assert {a["prompt_tokens"], b["prompt_tokens"]} == {7, 9}

    def test_no_meter_bound_reports_a_well_formed_zero(self):
        """Callers must never have to special-case the shape."""
        assert usage.current_usage() == {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "llm_calls": 0,
        }
        usage.TokenMeterCallback().on_llm_end(_llm_result_modern(5, 5))  # must not raise


class TestTheMeterIsAttachedEvenWithTracingOff:
    """The campaign that lost its cost data HAD Langfuse enabled and still shipped zeros.

    `workflow.py` used to set `config["callbacks"]` only `if callbacks:` — so with tracing off,
    no callback was attached at all and usage could never be reported. Asserting on the source
    is deliberate: the alternative is standing up a graph, and this is the precise line that
    regressed.
    """

    def test_the_token_meter_is_not_conditional_on_langfuse(self):
        import inspect

        from app.agent import workflow

        src = inspect.getsource(workflow)
        assert src.count("TokenMeterCallback()") == 2, (
            "both the invoke and the stream path must attach the meter"
        )
        assert "if callbacks:\n        config[\"callbacks\"]" not in src, (
            "callbacks are attached conditionally again — with Langfuse off the meter is "
            "dropped and every request reports zero tokens"
        )


class TestTheStreamActuallyCarriesIt:
    def test_the_usage_frame_is_emitted_before_the_terminator(self):
        """A client that stops reading at `finish_reason` must still have seen usage."""
        import inspect

        from app.api.v1.endpoints import chat_completions

        src = inspect.getsource(chat_completions._stream)
        # The FIRST finish_reason in the function is the normal-completion one; the other is in
        # the error path below it. Searching from an offset (as an earlier version of this test
        # did) can skip past the one that matters and make the assertion vacuous.
        # The marker is `wire.UsageEvent` since 2026-08-28: the frame used to be a hand-written
        # `{"type": "usage", ...}` dict, which is why nothing validated it and why the wire
        # module did not describe it. The ordering this asserts is unchanged.
        assert src.index("wire.UsageEvent(") < src.index('finish_reason="stop"'), (
            "usage must be emitted before the finish_reason frame, or a client that stops "
            "reading at the terminator never sees what the request cost"
        )

    def test_the_meter_is_bound_before_the_workflow_task_is_created(self):
        """Bound after `create_task`, the workflow inherits no meter and reports zero."""
        import inspect

        from app.api.v1.endpoints import chat_completions

        src = inspect.getsource(chat_completions._stream)
        assert src.index("start_request_meter()") < src.index("asyncio.create_task("), (
            "create_task copies the context, so the meter must exist before the task does"
        )


class TestTheUsageFrameGoesOutThroughTheDeclaredModel:
    """The usage frame was the one event built by hand instead of by a wire model.

    `ki_protocol/__init__.py` calls `wire` "canonical for what the server sends", and
    `chat_completions.py` emitted `{"type": "usage", **meter.as_dict()}` directly — so the one
    module making that claim did not describe the ninth thing the server sends. The cost was not
    cosmetic: the frame was validated by nothing, and `packages/kube-q/tests/core/
    test_the_wire_has_two_halves.py` is generative over `wire`, so an event outside `wire` was
    invisible to the very sweep that exists to catch this. `llm_calls` was dropped on arrival and
    the client's `model` field was never populated, both unnoticed for that reason.
    """

    def test_the_endpoint_does_not_hand_build_the_usage_frame(self):
        import inspect

        from app.api.v1.endpoints import chat_completions

        assert '{"type": "usage"' not in inspect.getsource(chat_completions), (
            "emit through wire.UsageEvent so the payload is validated and the wire module "
            "actually describes what the server sends"
        )

    def test_the_meter_output_validates_against_the_wire_model(self):
        from ki_protocol import wire

        from app.core.usage import UsageMeter

        meter = UsageMeter()
        meter.input_tokens, meter.output_tokens, meter.llm_calls = 7, 3, 2
        event = wire.UsageEvent(session_id="s", **meter.as_dict())
        assert (event.total_tokens, event.llm_calls) == (10, 2)

    def test_every_meter_field_has_a_home_on_the_wire(self):
        # `as_dict()` gaining a key that `UsageEvent` does not declare would silently drop it
        # again — pydantic ignores extras — which is precisely how `llm_calls` was lost.
        from ki_protocol import wire

        from app.core.usage import UsageMeter

        produced = set(UsageMeter().as_dict())
        declared = set(wire.UsageEvent.model_fields)
        assert produced <= declared, f"the meter produces {sorted(produced - declared)}, unwired"
        # And the reverse: the endpoint names these fields explicitly, so a count the model
        # declares but the meter stopped producing would ship as a silent default instead.
        assert declared - produced == {"type", "session_id", "ts"}, (
            f"UsageEvent declares {sorted(declared - produced - {'type','session_id','ts'})} "
            f"that the meter does not produce"
        )
