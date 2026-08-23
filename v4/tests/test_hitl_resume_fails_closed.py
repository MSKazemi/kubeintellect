"""An approval must be recognised, not merely "not recognised as a denial".

`stream_events` resumed a paused HITL interrupt with `Command(resume=not is_denial(msg))`, and
`is_denial` is an exact match against 13 phrases. Every reply outside that list executed the
pending destructive command. Measured 2026-08-20 by driving the real `stream_events` with a
thread paused at an interrupt and capturing the `Command` it builds:

    "no"            → resume=False   cancelled
    "No."           → resume=True    *** EXECUTED ***   ← a full stop
    "NO!"           → resume=True    *** EXECUTED ***
    "no thanks"     → resume=True    *** EXECUTED ***
    "don't do that" → resume=True    *** EXECUTED ***
    "cancel it"     → resume=True    *** EXECUTED ***
    "stop it"       → resume=True    *** EXECUTED ***
    "not yet"       → resume=True    *** EXECUTED ***
    "wait"          → resume=True    *** EXECUTED ***
    "why?"          → resume=True    *** EXECUTED ***
    ""              → resume=True    *** EXECUTED ***

`docs/security.md` has always documented the opposite — *"anything else → treated as denial"* —
so this was the published contract being false in the fail-open direction, on the last gate
standing between an LLM and a destructive cluster operation. `is_approval()` already existed in
`app/agent/hitl.py`; nothing called it for this decision.

The direction matters: a wrongly-refused approval costs the user one retry, a wrongly-accepted
denial deletes something. Anything unrecognised now cancels and logs why.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.agent.workflow as wf
from app.agent.hitl import is_approval, is_auto_approve_request, is_denial


def _resume_value_for(reply: str) -> bool | None:
    """Drive the real stream_events with a paused thread; return the resume it builds."""
    captured: list = []

    class FakeCommand:
        def __init__(self, resume=None, **_kw):
            captured.append(resume)

    async def _empty_stream(*_a, **_k):
        if False:  # pragma: no cover - an empty async generator
            yield None

    task = MagicMock()
    task.interrupts = [MagicMock()]
    state = MagicMock()
    state.tasks = [task]
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=state)
    graph.astream_events = _empty_stream

    async def _drive():
        with patch.object(wf, "get_graph", AsyncMock(return_value=graph)), \
             patch.object(wf, "Command", FakeCommand):
            async for _ in wf.stream_events(session_id="t", user_message=reply,
                                            user_id="u", user_role="admin"):
                pass

    asyncio.run(_drive())
    return captured[0] if captured else None


_MUST_NOT_EXECUTE = [
    "no", "deny", "cancel", "abort", "stop",
    "No.", "NO!", "no.", "nope!", "no thanks", "no, don't",
    "don't do that", "cancel it", "stop it", "not yet", "wait", "hold on",
    "why?", "what will that do?", "explain first", "hmm", "", "   ",
    "actually show me the diff first", "let me check with the on-call first",
]

_MUST_EXECUTE = [
    "yes", "approve", "approved", "ok", "okay", "sure", "proceed", "confirm",
    "do it", "go ahead", "run it",
    "Yes.", "YES!", "Approve.", "  ok  ", '"yes"',
]


class TestOnlyARecognisedApprovalExecutes:
    @pytest.mark.parametrize("reply", _MUST_NOT_EXECUTE)
    def test_anything_unrecognised_cancels(self, reply):
        assert _resume_value_for(reply) is False, f"{reply!r} resumed the destructive command"

    @pytest.mark.parametrize("reply", _MUST_EXECUTE)
    def test_a_real_approval_still_works(self, reply):
        """Failing closed is worthless if it also refuses the approvals people actually type."""
        assert _resume_value_for(reply) is True, f"{reply!r} was not accepted as an approval"

    def test_approve_all_approves_the_pending_action_too(self):
        """It enables bypass for the rest of the turn; cancelling what it just approved would be
        a new bug in the other direction."""
        assert is_auto_approve_request("approve all")
        assert _resume_value_for("approve all") is True


class TestTheNormaliser:
    @pytest.mark.parametrize("reply", ["No.", "NO!", "no", " no ", '"no"', "'no'", "No"])
    def test_punctuation_and_case_do_not_hide_a_denial(self, reply):
        assert is_denial(reply)

    @pytest.mark.parametrize("reply", ["Yes.", "YES!", " yes ", '"yes"', "Yes"])
    def test_punctuation_and_case_do_not_hide_an_approval(self, reply):
        assert is_approval(reply)

    @pytest.mark.parametrize("reply", ["no thanks", "yes but not now", "nope nope", "yesterday"])
    def test_it_does_not_match_on_a_substring(self, reply):
        """Whole-phrase matching, so a word appearing inside a sentence is not a verdict."""
        assert not is_approval(reply)

    def test_approval_and_denial_are_disjoint(self):
        for phrase in _MUST_EXECUTE:
            assert not is_denial(phrase), phrase
