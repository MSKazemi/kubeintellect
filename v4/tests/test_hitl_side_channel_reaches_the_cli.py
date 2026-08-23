"""The approval gate the server sends must be the one `kq` sees.

Every other test of this side channel builds its own chunk by hand and asserts the client reads
it. That proves the client can read *a* frame shaped the way the test author imagined -- it
cannot prove the server ever sends that shape. So these tests take the frames from the real
producer (`_serialise_event` and the terminal frames `_stream` emits) and feed them to the real
consumer (`kube_q.transport._stream_once`), with nothing hand-written in between.

The defect that motivated them: the streaming reader only consulted `hitl_required` inside
`if finish == "stop":`, while the server sets `hitl_required` on the gate chunk (whose
`finish_reason` is null) and `finish_reason: "stop"` on a later, separate terminal chunk. The
two conditions were never true at once, so `kq` never saw a gate on its default path -- and the
only other route to `hitl_pending`, an emoji fallback, tests for a "\N{OCTAGONAL SIGN}" that no
code anywhere emits.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from app.api.v1.endpoints.chat_completions import (
    _done_chunk,
    _make_chunk,
    _serialise_event,
)
from kube_q.transport import _stream_once, build_headers, build_payload, make_client

CID = "chatcmpl-crosscheck"
URL = "http://server"


def _gate_frames(risk: str = "high", action_id: str = "act-1") -> str:
    """Exactly what the server streams when the agent stops for approval.

    The gate chunk comes from the production serialiser; the two frames after it are the ones
    `_stream` yields once the graph interrupts.
    """
    gate = _serialise_event(CID, {
        "type": "hitl_request",
        "action_id": action_id,
        "risk_level": risk,
        "command": "kubectl scale deploy/payments --replicas=5",
    })
    assert gate is not None
    return gate + _make_chunk(CID, "", finish_reason="stop") + _done_chunk()


def _read(body: str):
    with respx.mock:
        respx.post(f"{URL}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, text=body, headers={"Content-Type": "text/event-stream"}
            )
        )
        with make_client(30.0) as client:
            return _stream_once(
                URL,
                build_payload([{"role": "user", "content": "scale it"}], "u", stream=True),
                build_headers("sess-1", "u", "req-1"),
                client,
            )


class TestTheGateSurvivesTheWire:
    def test_kq_sees_the_gate_the_server_actually_sends(self):
        text, hitl_pending, action_id, _usage = _read(_gate_frames())
        assert hitl_pending is True, (
            "the server set hitl_required on the gate chunk and kq did not see it"
        )
        assert action_id == "act-1", "without the action_id the REPL cannot resume the action"
        assert "Approval Required" in text

    @pytest.mark.parametrize("risk", ["high", "medium"])
    def test_it_does_not_depend_on_the_risk_emoji(self, risk):
        """Detection must come from the field, not from prose that could be reworded."""
        _text, hitl_pending, action_id, _u = _read(_gate_frames(risk=risk, action_id=f"a-{risk}"))
        assert hitl_pending is True
        assert action_id == f"a-{risk}"

    def test_an_ordinary_answer_is_not_mistaken_for_a_gate(self):
        body = _make_chunk(CID, "3 pods are running.") \
            + _make_chunk(CID, "", finish_reason="stop") + _done_chunk()
        text, hitl_pending, action_id, _u = _read(body)
        assert hitl_pending is False
        assert action_id is None
        assert "3 pods are running." in text


class TestTheEmojiFallbackIsHonest:
    def test_no_code_emits_the_emoji_the_fallback_waits_for(self):
        """The fallback tells the operator to upgrade the server. That must be true when shown.

        It fires on a "\N{OCTAGONAL SIGN}" in the answer text. Nothing in the server emits that
        character -- the gate message uses the risk emoji -- so the message could only ever have
        appeared for text the *model* happened to write, while blaming the server.
        """
        gate = _serialise_event(CID, {
            "type": "hitl_request", "action_id": "act-2",
            "risk_level": "high", "command": "kubectl delete ns test",
        })
        assert gate is not None
        assert "\N{OCTAGONAL SIGN}" not in gate


class TestTheGateReachesTheSDK:
    """`kube_q.core.client` is the published SDK, and it exports `HitlRequestEvent`.

    Exporting a typed event for the approval gate is a promise that a caller can detect the gate
    without reading prose. The server sends the gate as an ordinary chunk with the fields merged
    onto the choice -- not as a `ki_event` -- and the SDK's parser looked only at `ki_event` and
    `delta.content`, so the promised event could never be produced. A caller streaming through
    the SDK got a `TokenEvent` full of markdown and no machine-readable signal at all.
    """

    def _gate_chunk(self) -> dict:
        import json

        frame = _serialise_event(CID, {
            "type": "hitl_request", "action_id": "act-7",
            "risk_level": "high", "command": "kubectl delete ns prod",
        })
        assert frame is not None
        return json.loads(frame[len("data: "):].strip())

    def test_the_sdk_emits_a_typed_gate_event(self):
        from kube_q.core.client import _parse_sse_events
        from kube_q.core.events import HitlRequestEvent

        events = _parse_sse_events(self._gate_chunk())
        gates = [e for e in events if isinstance(e, HitlRequestEvent)]
        assert gates, f"no HitlRequestEvent produced, got {[type(e).__name__ for e in events]}"
        gate = gates[0]
        assert gate.data.approval_id == "act-7", "without this the caller cannot resume"
        assert gate.data.risk == "high"
        assert "kubectl delete ns prod" in gate.data.action

    def test_the_prose_still_reaches_the_caller(self):
        """The markdown is what a human-facing consumer prints; adding the signal must not eat it."""
        from kube_q.core.client import _parse_sse_events
        from kube_q.core.events import TokenEvent

        events = _parse_sse_events(self._gate_chunk())
        tokens = [e for e in events if isinstance(e, TokenEvent)]
        assert tokens and "Approval Required" in tokens[0].data.content
        assert events.index(tokens[0]) == 0, "prose first, then the structured signal"

    def test_an_ordinary_token_chunk_yields_exactly_one_event(self):
        from kube_q.core.client import _parse_sse_events
        from kube_q.core.events import TokenEvent

        events = _parse_sse_events({"choices": [{"index": 0, "delta": {"content": "hello"}}]})
        assert len(events) == 1 and isinstance(events[0], TokenEvent)


class TestTheDocumentedFrameIsTheRealFrame:
    """`docs/api-reference.md` is the contract a third-party client is written against.

    Two defects in as many days came from consumers that had the wrong idea of where the gate
    fields live. The documented example is what an outside integrator copies, so it must be the
    frame the server actually sends -- and if a field is ever renamed or added, this fails rather
    than leaving the public reference quietly wrong.
    """

    STRUCTURAL = {"index", "delta", "finish_reason"}

    def _documented_choice(self) -> dict:
        import json
        import re
        from pathlib import Path

        doc = (Path(__file__).resolve().parents[1] / "docs" / "api-reference.md").read_text()
        marker = "**4. Approval request (HITL)**"
        assert marker in doc, "the HITL section was renamed; this gate must be re-pointed"
        block = re.search(r"```json\n(.*?)\n```", doc[doc.index(marker):], re.S)
        assert block, "no JSON example under the HITL section"
        return json.loads(block.group(1))["choices"][0]

    def _emitted_choice(self) -> dict:
        import json

        frame = _serialise_event(CID, {
            "type": "hitl_request", "action_id": "a1",
            "risk_level": "medium", "command": "kubectl scale deployment/web --replicas=5",
        })
        assert frame is not None
        return json.loads(frame[len("data: "):].strip())["choices"][0]

    def test_the_documented_side_channel_fields_are_exactly_the_emitted_ones(self):
        documented = set(self._documented_choice()) - self.STRUCTURAL
        emitted = set(self._emitted_choice()) - self.STRUCTURAL
        assert documented == emitted, (
            f"api-reference.md and the server disagree — "
            f"only in docs: {documented - emitted}; only in code: {emitted - documented}"
        )

    def test_the_documented_gate_does_not_carry_a_terminal_finish_reason(self):
        """The gate chunk is mid-turn. Documenting it as terminal is what misled the CLI.

        `kq` gated its reading of `hitl_required` on `finish_reason == "stop"`, and the gate
        chunk does not carry one -- so the side channel was never read. Pinning this keeps the
        example from teaching the next integrator the same wrong thing.
        """
        assert self._documented_choice().get("finish_reason") in (None, "null")
        assert self._emitted_choice().get("finish_reason") is None
