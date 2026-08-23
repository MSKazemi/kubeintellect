"""«approve all» bypasses HITL for one turn. Four doc surfaces said "for the session".

`is_auto_approve_request` matches a phrase set and `stream_events` responds by putting
`hitl_bypass: True` into the run config. Nothing persists it: the config is rebuilt on every
call from `auto_approve`, which comes either from the request body (`kq --auto-approve`) or
from the *current* message. So the bypass ends with the turn — and the `kq` REPL does not
latch it client-side either. Measured over three turns:

    turn 1  "approve all"                    hitl_bypass=True
    turn 2  "get all pods"                   hitl_bypass=False
    turn 3  "scale the api deployment to 0"  hitl_bypass=False   <- gated again

while `docs/security.md` ("session-wide bypass"), `docs/examples.md` and
`docs/cli-reference.md` ("for the rest of a session"), `docs/api-reference.md` ("for the rest
of the session") and the log line ("HITL bypass enabled for session=…") all said otherwise.

**The gap is in the safe direction** — the gate stays on — so this suite pins the behaviour
that exists rather than widening it. Making the bypass genuinely session-scoped would weaken
the product's central safety gate, which is an owner decision (`T43`), not a side effect of
correcting a sentence. If someone later decides to implement it, these tests are the ones to
change deliberately.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent import workflow
from app.agent.hitl import is_approval, is_auto_approve_request, is_denial


class _NoInterrupt:
    tasks = ()


async def _bypass_flags_for(messages: list[str], **kwargs) -> list[bool]:
    """Run `stream_events` once per message and report the `hitl_bypass` each turn ran with."""
    seen: list[bool] = []

    async def _astream_events(input_data, config=None, version=None):
        seen.append(config["configurable"].get("hitl_bypass"))
        return
        yield   # pragma: no cover - makes this an async generator

    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=_NoInterrupt())
    graph.astream_events = _astream_events
    with patch.object(workflow, "get_graph", AsyncMock(return_value=graph)), \
            patch.object(workflow, "get_langfuse_callbacks", lambda: None):
        for message in messages:
            async for _ in workflow.stream_events(message, "sess-1", "u", "admin", **kwargs):
                pass
    return seen


@pytest.mark.asyncio
class TestTheBypassEndsWithTheTurn:

    async def test_approve_all_bypasses_the_turn_it_is_typed_in(self):
        assert await _bypass_flags_for(["approve all"]) == [True]

    async def test_the_next_turn_is_gated_again(self):
        flags = await _bypass_flags_for(
            ["approve all", "get all pods", "scale the api deployment to 0"])
        assert flags == [True, False, False], (
            "the bypass survived the turn — if that is now intended, the docs and T43 have to "
            "move with it, because four surfaces describe the gate's reach to users"
        )

    async def test_an_ordinary_question_never_enables_it(self):
        assert await _bypass_flags_for(["why is the api pod crashing?"]) == [False]

    async def test_the_request_flag_is_what_spans_a_session(self):
        """`kq --auto-approve` sets it on every request; that is the supported way."""
        flags = await _bypass_flags_for(["a", "b"], auto_approve=True)
        assert flags == [True, True]

    async def test_the_flag_does_not_leak_into_a_session_that_did_not_ask(self):
        assert await _bypass_flags_for(["approve all"]) == [True]
        assert await _bypass_flags_for(["scale the api deployment to 0"]) == [False]


class TestThePhraseSetsStayDisjoint:
    """`approve all` has to read as an approval too, or it would cancel what it approved."""

    def test_approve_all_is_also_an_approval(self):
        for phrase in ("approve all", "auto-approve", "yes to all", "/auto-approve"):
            assert is_auto_approve_request(phrase)

    def test_no_phrase_is_both_an_approval_and_a_denial(self):
        from app.agent.hitl import _APPROVAL_PHRASES, _AUTO_APPROVE_PHRASES, _DENIAL_PHRASES
        both = (_APPROVAL_PHRASES | _AUTO_APPROVE_PHRASES) & _DENIAL_PHRASES
        assert not both, f"phrases that both approve and deny: {sorted(both)}"

    def test_an_unrecognised_reply_is_neither(self):
        for phrase in ("not yet", "wait", "why?", "", "maybe"):
            assert not is_approval(phrase) and not is_auto_approve_request(phrase)
            assert not is_denial(phrase) or phrase in ("",)
