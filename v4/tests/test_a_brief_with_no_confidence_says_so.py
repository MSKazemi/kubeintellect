"""The responder brief only ever displayed confidence when the number was reassuring.

`render_brief` gated the line on `if brief.confidence:`, so a brief that rated itself **0.05**
printed *"RCA confidence: 5%"* and a brief that rated itself **0.0** — the least confident it can
be — printed nothing at all. The only brief carrying no caveat was the worst one. The same
truthiness test collapsed three different states into that silence: the model said zero, the model
omitted the field, and the model answered `"high"`.

This is the surface an on-call responder reads (it is appended to the investigation answer in
`cortex/graph.py` behind `KI_V5_ESCALATION_BRIEFS`), so a signal that reaches the dataclass and
not the markdown has not been delivered.

Two smaller corrections ride along. The line was labelled *RCA confidence* when the value is the
brief writer's confidence in the plan it just wrote — attributing it to the root-cause analysis
inflates its standing. And `fell_back` existed on the dataclass but nothing rendered it.
"""
from __future__ import annotations

import json

import pytest
from app.cortex.briefs import (
    _FALLBACK_ESCALATE_IF,
    EscalationBrief,
    build_brief,
    render_brief,
)


class _Reply:
    def __init__(self, content: str) -> None:
        self.content = content


class _LLM:
    def __init__(self, content: str) -> None:
        self.content = content

    async def ainvoke(self, _messages: object) -> _Reply:
        return _Reply(self.content)


class _Boom:
    async def ainvoke(self, _messages: object) -> _Reply:
        raise RuntimeError("model unavailable")


def _reply(**overrides: object) -> str:
    body: dict[str, object] = {
        "summary": "Node pressure evicted the pod.",
        "actions": ["Drain node-3", "Recreate the pod"],
        "escalate_if": ["the eviction repeats within 10 minutes"],
    }
    body.update(overrides)
    return json.dumps(body)


def _confidence_lines(rendered: str) -> list[str]:
    return [ln for ln in rendered.splitlines() if "confidence" in ln.lower()]


# ── The defect ─────────────────────────────────────────────────────────────────

async def test_a_zero_confidence_brief_says_zero() -> None:
    brief = await build_brief("rc", "ev", llm=_LLM(_reply(confidence=0.0)))
    assert brief.confidence == 0.0
    assert "0%" in render_brief(brief), render_brief(brief)


@pytest.mark.parametrize("value,shown", [(0.0, "0%"), (0.05, "5%"), (0.5, "50%"), (0.9, "90%")])
async def test_every_stated_confidence_is_displayed(value: float, shown: str) -> None:
    brief = await build_brief("rc", "ev", llm=_LLM(_reply(confidence=value)))
    lines = _confidence_lines(render_brief(brief))
    assert lines == [f"_Brief confidence: {shown}._"], lines


async def test_a_brief_always_states_a_confidence_exactly_once() -> None:
    """Vacuity guard — one line, always, whatever the model did."""
    for reply in (_reply(confidence=0.0), _reply(confidence=0.9), _reply(),
                  _reply(confidence="high"), _reply(confidence=None)):
        brief = await build_brief("rc", "ev", llm=_LLM(reply))
        assert len(_confidence_lines(render_brief(brief))) == 1, reply


# ── "the model gave none" is not "the model said zero" ─────────────────────────

@pytest.mark.parametrize("reply", [_reply(), _reply(confidence="high"), _reply(confidence=None)])
async def test_an_unusable_confidence_is_absent_not_zero(reply: str) -> None:
    brief = await build_brief("rc", "ev", llm=_LLM(reply))
    assert brief.confidence is None
    assert "reported no confidence" in render_brief(brief)


async def test_absent_and_zero_do_not_render_the_same() -> None:
    zero = render_brief(await build_brief("rc", "ev", llm=_LLM(_reply(confidence=0.0))))
    absent = render_brief(await build_brief("rc", "ev", llm=_LLM(_reply())))
    assert _confidence_lines(zero) != _confidence_lines(absent)


def test_zero_and_none_are_distinct_values() -> None:
    """Non-vacuity for the type change: `0.0 == None` would make the test above meaningless."""
    assert EscalationBrief(summary="s", confidence=0.0).confidence is not None
    assert EscalationBrief(summary="s").confidence is None


# ── The label ──────────────────────────────────────────────────────────────────

def test_the_confidence_is_attributed_to_the_brief_not_the_rca() -> None:
    rendered = render_brief(EscalationBrief(summary="s", actions=["a"], escalate_if=["c"],
                                            confidence=0.8))
    assert "Brief confidence" in rendered
    assert "RCA confidence" not in rendered


# ── The fallback is visible in what the responder reads ────────────────────────

async def test_a_fallback_brief_is_marked_in_its_heading() -> None:
    brief = await build_brief("rc", "ev", llm=_Boom())
    assert brief.fell_back is True
    assert "FALLBACK" in render_brief(brief)


async def test_a_fallback_reports_no_confidence_rather_than_zero_confidence() -> None:
    """A fallback was never written, so it has no opinion of itself to rate — not even a low one.

    `Brief confidence: 0%` would be a statement the brief made. There is no brief.
    """
    brief = await build_brief("rc", "ev", llm=_Boom())
    assert brief.confidence is None
    lines = _confidence_lines(render_brief(brief))
    assert lines == ["_This brief reported no confidence in itself._"], lines


async def test_a_real_brief_is_not_marked_as_a_fallback() -> None:
    """Vacuity guard — the marker is earned, not printed on every brief."""
    brief = await build_brief("rc", "ev", llm=_LLM(_reply(confidence=0.7)))
    assert brief.fell_back is False
    assert "FALLBACK" not in render_brief(brief)


async def test_a_fallback_still_carries_every_safety_condition() -> None:
    """The property the fallback exists for, re-asserted so the rendering change cannot lose it."""
    rendered = render_brief(await build_brief("rc", "ev", llm=_Boom()))
    for condition in _FALLBACK_ESCALATE_IF:
        assert condition in rendered, condition


# ── Clamping survived the rewrite ──────────────────────────────────────────────

@pytest.mark.parametrize("value,shown", [(1.5, "100%"), (-1.0, "0%"), (1.0, "100%")])
async def test_out_of_range_confidence_is_still_clamped(value: float, shown: str) -> None:
    brief = await build_brief("rc", "ev", llm=_LLM(_reply(confidence=value)))
    assert _confidence_lines(render_brief(brief)) == [f"_Brief confidence: {shown}._"]


async def test_the_safety_net_conditions_are_still_appended_to_a_real_brief() -> None:
    brief = await build_brief("rc", "ev", llm=_LLM(_reply(confidence=0.9)))
    assert brief.fell_back is False
    for condition in _FALLBACK_ESCALATE_IF:
        assert condition in brief.escalate_if, condition
