"""The decision_log has two readers, and only one of them knew what a `finding` looks like.

`app.digest.postmortem` and `kq replay` both turn a flight-recorder row into a line of
text. The postmortem handled eleven kinds; `kq replay` matched seven *top-level field
names* — so every row whose content lives elsewhere rendered as an **empty summary**:

    kq replay findings:default
    #   type      summary
    0   finding
    1   finding          <- three detectors fired; the log says so; the reader says nothing

`finding`, `plan` and `ki_otel_span` were all blank. This suite reads *both* artefacts —
the recorded kinds and the readers — so a kind that nothing summarises fails here instead
of silently rendering as an empty cell.

Two separate bugs are pinned:

1. **The summariser was written twice.** `ki_protocol.record.summarise_record` is now the
   only copy; both readers call it.
2. **The replay stream dropped `kind`.** The endpoint yielded the payload alone, so the
   client's `type` column depended on each payload happening to echo its own kind. Every
   hand-written recorder call remembered to; `otel_spans._base` never did.
"""
from __future__ import annotations

import json

import pytest
from ki_protocol import wire
from ki_protocol.record import CARRIES_NO_CONTENT, GAP_KIND, SPAN_KIND, summarise_record

from app.db import flight_recorder, otel_spans
from app.detectors.models import Finding
from app.digest import postmortem
from kube_q.cli import replay_cmd

# ── The kinds that actually reach the recorder ────────────────────────────────────────
# Wire events (via `emitter.emit`) plus the four kinds recorded by hand elsewhere.

WIRE_SAMPLES: dict[str, dict] = {
    "status": dict(phase="analyzing", message="Looking at pods", session_id="s"),
    "tool_call": dict(tool="run_kubectl", command="kubectl get pods", session_id="s"),
    "tool_result": dict(tool="run_kubectl", output="NAME READY STATUS", session_id="s"),
    "token": dict(content="hi", session_id="s"),
    "final": dict(session_id="s"),
    "hitl_request": dict(risk_level="high", command="kubectl delete pod x", session_id="s"),
    "plan": dict(steps=[{"description": "check events", "status": "done"},
                        {"description": "check logs", "status": "pending"}], session_id="s"),
    "error": dict(error="boom", session_id="s"),
}


def _wire_models() -> list[type]:
    return [
        obj for obj in vars(wire).values()
        if isinstance(obj, type) and issubclass(obj, wire.BaseModel)
        and obj is not wire.BaseModel and "type" in obj.model_fields
    ]


def _wire_payload(model: type) -> tuple[str, dict]:
    kind = model.model_fields["type"].default
    return kind, model(**WIRE_SAMPLES[kind]).model_dump()


def _finding_payload() -> dict:
    return Finding(playbook="crashloop", cluster_id="default", namespace="shop",
                   object_name="pod/api-0", evidence="pod status=CrashLoopBackOff").to_dict()


def _rollback_payload() -> dict:
    # Mirrors the literal recorded in kubectl_tool.py.
    return {"type": "rollback_point", "rollback_id": "rb-1", "command": "kubectl delete pod x",
            "pre_state": [], "restorable": False, "capture_notes": [], "session_id": "s"}


NON_WIRE_SAMPLES: dict[str, dict] = {
    "finding": _finding_payload(),
    "rollback_point": _rollback_payload(),
    GAP_KIND: flight_recorder._gap_payload(4, "the decision_log table was missing"),
    SPAN_KIND: otel_spans.chat_span_payload("ep1", 0, system="openai", model="gpt-4o",
                                            input_tokens=10, output_tokens=3),
}

RECORDED_KINDS = sorted(set(WIRE_SAMPLES) | set(NON_WIRE_SAMPLES))


def _payload_for(kind: str) -> dict:
    if kind in NON_WIRE_SAMPLES:
        return NON_WIRE_SAMPLES[kind]
    model = next(m for m in _wire_models() if m.model_fields["type"].default == kind)
    return model(**WIRE_SAMPLES[kind]).model_dump()


# ── 1. Every recorded kind produces a summary, in both readers ────────────────────────

class TestEveryRecordedKindSummarises:

    @pytest.mark.parametrize("kind", [k for k in RECORDED_KINDS if k not in CARRIES_NO_CONTENT])
    def test_shared_summariser_is_never_empty(self, kind):
        assert summarise_record(kind, _payload_for(kind)).strip(), (
            f"kind {kind!r} summarises to nothing — a reader would print a blank cell"
        )

    @pytest.mark.parametrize("kind", [k for k in RECORDED_KINDS if k not in CARRIES_NO_CONTENT])
    def test_the_cli_reader_is_never_empty(self, kind):
        event = {**_payload_for(kind), "type": kind}
        assert replay_cmd._summarise(event).strip(), f"kq replay renders {kind!r} blank"

    @pytest.mark.parametrize("kind", [k for k in RECORDED_KINDS if k not in CARRIES_NO_CONTENT])
    def test_the_server_reader_is_never_empty(self, kind):
        assert postmortem._summarize(kind, _payload_for(kind)).strip()

    @pytest.mark.parametrize("kind", RECORDED_KINDS)
    def test_the_two_readers_agree(self, kind):
        """One summariser, so the postmortem and the CLI cannot drift apart again."""
        payload = _payload_for(kind)
        assert replay_cmd._summarise({**payload, "type": kind}) == \
            postmortem._summarize(kind, payload).replace("\n", " ")

    def test_a_summary_is_never_only_the_kind_name(self):
        """Falling through to `return kind` is the shape the blank cells had."""
        bare = [k for k in RECORDED_KINDS
                if k not in CARRIES_NO_CONTENT and summarise_record(k, _payload_for(k)) == k]
        assert not bare, f"unhandled kinds fell through to their own name: {bare}"

    def test_an_unknown_kind_still_says_something(self):
        assert summarise_record("kind_invented_next_year", {}) == "kind_invented_next_year"
        assert summarise_record("", {}) == "record"

    def test_a_non_dict_payload_does_not_raise(self):
        assert summarise_record("finding", None)      # type: ignore[arg-type]
        assert summarise_record("status", ["nope"])   # type: ignore[arg-type]


# ── 2. The regressions themselves, named ──────────────────────────────────────────────

class TestTheKindsThatUsedToBeBlank:

    def test_a_finding_names_its_playbook_and_object(self):
        out = summarise_record("finding", _finding_payload())
        assert "crashloop" in out and "shop" in out and "pod/api-0" in out

    def test_a_predicted_finding_says_when(self):
        p = _finding_payload() | {"severity": "predicted", "eta_minutes": 12.0}
        assert "predicted ~12m" in summarise_record("finding", p)

    def test_a_plan_reports_its_progress(self):
        _, payload = _wire_payload(wire.PlanEvent)
        assert summarise_record("plan", payload) == "plan — 2 step(s), 1 done"

    def test_an_empty_plan_still_summarises(self):
        assert summarise_record("plan", {"steps": []}).strip()

    def test_a_span_names_its_operation(self):
        out = summarise_record(SPAN_KIND, NON_WIRE_SAMPLES[SPAN_KIND])
        assert "chat" in out and "attribute(s)" in out

    def test_a_rollback_point_still_warns_when_not_restorable(self):
        assert "NOT restorable" in summarise_record("rollback_point", _rollback_payload())

    def test_a_gap_still_shouts(self):
        out = summarise_record(GAP_KIND, NON_WIRE_SAMPLES[GAP_KIND])
        assert "LOST" in out and "4" in out


# ── 3. There is exactly one copy of the summariser ────────────────────────────────────

class TestOneCopyOnly:

    def test_the_cli_no_longer_carries_its_own_field_list(self):
        assert not hasattr(replay_cmd, "_SUMMARY_FIELDS"), (
            "the CLI grew a second summariser again — that is the bug this suite exists for"
        )

    def test_the_postmortem_delegates(self):
        src = postmortem._summarize.__code__.co_consts
        assert postmortem._summarize("finding", _finding_payload()) == \
            summarise_record("finding", _finding_payload())
        assert not any(isinstance(c, str) and "detector" in c for c in src), \
            "the postmortem still spells out its own summaries"

    def test_the_shared_constants_match_the_recorder(self):
        assert GAP_KIND == flight_recorder.GAP_KIND
        assert SPAN_KIND == otel_spans.SPAN_KIND
        assert replay_cmd._GAP_TYPE == flight_recorder.GAP_KIND

    def test_the_skipped_kinds_match(self):
        assert CARRIES_NO_CONTENT == flight_recorder._SKIP_KINDS


# ── 4. Every wire model is covered — a new event type fails here ──────────────────────

class TestTheSampleSetIsComplete:

    def test_every_wire_model_has_a_sample(self):
        missing = [m.__name__ for m in _wire_models()
                   if m.model_fields["type"].default not in WIRE_SAMPLES]
        assert not missing, f"new wire event(s) with no summary coverage: {missing}"

    @pytest.mark.parametrize("model", _wire_models(), ids=lambda m: m.__name__)
    def test_the_sample_is_a_valid_instance(self, model):
        kind, payload = _wire_payload(model)
        assert payload["type"] == kind


# ── 5. The replay stream carries the row's kind ───────────────────────────────────────

class TestTheStreamCarriesTheKind:
    """`ki_otel_span` payloads have no `type` of their own; the row does.

    These drive the real endpoint generator rather than rebuilding the frame here — a test
    that constructs the thing it is checking cannot see the endpoint change underneath it.
    """

    @staticmethod
    async def _frames(rows: list[dict]) -> list[dict]:
        from app.api.v1.endpoints import episodes
        response = await episodes.replay_episode("ep1")
        out = []
        async for chunk in response.body_iterator:
            body = chunk.removeprefix("data: ").strip()
            if body != "[DONE]":
                out.append(json.loads(body))
        return out[1:]   # drop the replay_meta frame

    @pytest.fixture(autouse=True)
    def _recorded(self, mocker):
        self.rows = [
            {"episode_id": "ep1", "seq": i, "kind": kind,
             "payload": json.dumps(_payload_for(kind), default=str),
             "prev_hash": "", "hash": "", "created_at": 0.0}
            for i, kind in enumerate(RECORDED_KINDS)
        ]
        from app.api.v1.endpoints import episodes
        mocker.patch.object(episodes, "fetch_episode", return_value=self.rows)
        # verify_episode is async and three-state: it consults the truncation anchor, not just
        # the links, and says separately whether that consultation happened at all.
        from app.db.flight_recorder import ChainVerdict
        mocker.patch.object(
            episodes, "verify_episode", new_callable=mocker.AsyncMock,
            return_value=ChainVerdict(valid=True, verified=True),
        )

    @pytest.mark.asyncio
    async def test_the_type_column_is_the_recorded_kind(self):
        frames = await self._frames(self.rows)
        assert [f.get("type", "?") for f in frames] == RECORDED_KINDS

    @pytest.mark.asyncio
    async def test_no_replayed_row_renders_blank_in_the_cli(self):
        """End to end: what the endpoint yields is what `kq replay` puts in the table."""
        blank = [
            f.get("type", "?") for f in await self._frames(self.rows)
            if f.get("type") not in CARRIES_NO_CONTENT and not replay_cmd._summarise(f).strip()
        ]
        assert not blank, f"kq replay would print an empty summary for: {blank}"

    @pytest.mark.asyncio
    async def test_a_span_is_not_rendered_as_unknown(self):
        span = next(f for f in await self._frames(self.rows)
                    if f.get("gen_ai.operation.name"))
        assert span.get("type", "?") == SPAN_KIND
        assert "chat" in replay_cmd._summarise(span)
