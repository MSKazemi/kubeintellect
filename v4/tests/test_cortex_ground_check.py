"""ADR-009 grounding check — the Cortex `ground_check` node and the autonomy it gates.

Fake-classifier tests (no network). What each class pins:

* an unsupported claim is hedged on the wire, withdrawn from the stored answer, and demotes the
  turn to advisory — and the watchtower then does NOT run the auto-approved fix turn;
* an all-supported draft passes through unchanged and may drive an A3 fix;
* a draft with no actionable claim costs no LLM call;
* a classifier error returns the read-only answer, says the check did not run, and never raises
  autonomy;
* with SELF_GOVERN_ENABLED off the graph and the watchtower are exactly what they were.
"""
from __future__ import annotations

from app.autonomy import watchtower
from app.cortex import graph as cx
from app.cortex import verify
from app.detectors.models import Finding
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

_DIAGNOSIS = (
    "The pod web-1 is crash-looping because the container is OOMKilled. "
    "The root cause is a memory leak introduced by the v2.3 image. "
    "Fix: raise the memory limit to 512Mi."
)
_LEAK_CLAIM = "The root cause is a memory leak introduced by the v2.3 image."


class _LLM:
    """Fake cheap-tier model: returns a preset reply and counts calls."""
    def __init__(self, content):
        self._content = content
        self.calls = 0

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        return AIMessage(content=self._content)


class _BoomLLM:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        raise RuntimeError("classifier down")


def _claims(*pairs):
    import json
    return json.dumps({"claims": [{"claim": c, "support": s} for c, s in pairs]})


def _state(answer, **over):
    base = {
        "messages": [
            HumanMessage(content="why is web-1 crashing?"),
            ToolMessage(tool_call_id="t1", name="run_kubectl",
                        content="web-1  0/1  CrashLoopBackOff  Reason: OOMKilled  Limits: memory 128Mi"),
            AIMessage(content=answer, id="draft-1"),
        ],
        "session_id": "s-ground",
        "user_id": "u1",
        "user_role": "admin",
        "memory_context": "",
        "cluster_snapshot": "",
        "matched_playbooks": [],
        "investigation_plan": [],
        "plan_cursor": 0,
        "gather_rounds": 1,
        "turn_start_index": 1,
        "triage_mode": "investigate",
    }
    base.update(over)
    return base


def _wire(mocker, llm):
    """Patch the cheap tier, the emitter and the flight recorder; return the captures."""
    emitted, recorded = [], []

    async def _capture(sid, event):
        emitted.append(event)

    mocker.patch.object(cx, "emit", side_effect=_capture)
    mocker.patch("app.cortex.models.get_specialist_llm", return_value=llm)
    mocker.patch("app.db.flight_recorder.record",
                 side_effect=lambda sid, kind, payload: recorded.append((kind, payload)))
    return emitted, recorded


def _tokens(emitted):
    return "".join(e.content for e in emitted if getattr(e, "type", "") == "token")


class TestUnsupportedClaimDemotes:
    async def test_claim_withdrawn_hedged_and_autonomy_demoted(self, mocker):
        llm = _LLM(_claims(
            ("The pod web-1 is crash-looping because the container is OOMKilled.", "supported"),
            (_LEAK_CLAIM, "none"),
            ("Fix: raise the memory limit to 512Mi.", "partial"),
        ))
        emitted, recorded = _wire(mocker, llm)

        out = await cx.ground_check(_state(_DIAGNOSIS), {})

        assert llm.calls == 1
        g = out["grounding"]
        assert g["status"] == "unsupported"
        assert g["counts"] == {"supported": 1, "partial": 1, "none": 1}
        assert g["autonomy_ceiling"] == "A1"            # advisory: cannot auto-trigger A2/A3
        assert verify.grounding_permits_autofix(g) is False

        # Stored copy: the unsupported claim is dropped from the body; the draft is REPLACED
        # (same id), not followed by a second answer.
        (stored,) = out["messages"]
        assert stored.id == "draft-1"
        body, _, note = stored.content.partition("\n---\n")
        assert _LEAK_CLAIM not in body
        assert "[withdrawn" in body
        assert "OOMKilled" in body                     # the supported claim survives
        # Streamed copy: the tokens already went out, so the claim is hedged by the note.
        streamed = _tokens(emitted)
        assert "not supported by the evidence" in streamed and _LEAK_CLAIM in streamed
        assert "demoted to advisory" in streamed
        # Observable: one flight-recorder row with the per-class counts.
        assert recorded == [("ground_check", {"type": "ground_check", **g})]

    async def test_watchtower_withholds_the_fix_turn(self, mocker):
        runs = await _watchtower_runs(mocker, grounding={
            "status": "unsupported", "counts": {"supported": 1, "partial": 0, "none": 1},
            "autonomy_ceiling": "A1",
        })
        assert len(runs) == 1                           # diagnose-and-propose only
        assert runs[0]["auto"] is False
        assert "do not execute destructive" in runs[0]["ask"]


class TestAllSupportedUnchanged:
    async def test_answer_untouched_no_ceiling(self, mocker):
        llm = _LLM(_claims(
            ("The pod web-1 is crash-looping because the container is OOMKilled.", "supported"),
            ("Fix: raise the memory limit to 512Mi.", "supported"),
        ))
        emitted, _ = _wire(mocker, llm)
        answer = ("The pod web-1 is crash-looping because the container is OOMKilled. "
                  "Fix: raise the memory limit to 512Mi.")

        out = await cx.ground_check(_state(answer), {})

        assert out["grounding"]["status"] == "grounded"
        assert out["grounding"]["autonomy_ceiling"] is None
        assert "messages" not in out                    # the answer is not rewritten
        assert _tokens(emitted) == ""
        assert verify.grounding_permits_autofix(out["grounding"]) is True

    async def test_watchtower_runs_the_fix_turn(self, mocker):
        runs = await _watchtower_runs(mocker, grounding={
            "status": "grounded", "counts": {"supported": 2, "partial": 0, "none": 0},
            "autonomy_ceiling": None,
        })
        assert [r["auto"] for r in runs] == [False, True]
        assert "Apply the fix you proposed" in runs[1]["ask"]


class TestSkipRule:
    async def test_no_actionable_claim_makes_no_llm_call(self, mocker):
        llm = _BoomLLM()
        emitted, recorded = _wire(mocker, llm)
        healthy = "All 24 pods in namespace shop are Running and Ready. No warning events."

        out = await cx.ground_check(_state(healthy), {})

        assert llm.calls == 0
        assert out["grounding"]["status"] == "skipped"
        assert out["grounding"]["autonomy_ceiling"] is None
        assert "messages" not in out and emitted == []
        assert recorded[0][1]["status"] == "skipped"    # still observable

    def test_skipped_check_never_licenses_an_autofix(self):
        # Nothing actionable was claimed, so there is nothing to apply.
        assert verify.grounding_permits_autofix({"status": "skipped", "autonomy_ceiling": None}) is False


class TestClassifierErrorFailsOpen:
    async def test_answer_returned_and_autonomy_not_raised(self, mocker):
        llm = _BoomLLM()
        emitted, _ = _wire(mocker, llm)

        out = await cx.ground_check(_state(_DIAGNOSIS), {})

        assert llm.calls == 1
        g = out["grounding"]
        assert g["status"] == "errored"
        assert g["autonomy_ceiling"] == "A2"            # may stay at propose, never raised
        assert verify.grounding_permits_autofix(g) is False
        # The read-only answer is not blocked: every word of the draft is still there.
        (stored,) = out["messages"]
        assert stored.content.startswith(_DIAGNOSIS)
        assert "NOT PERFORMED" in _tokens(emitted)

    async def test_garbage_and_empty_verdicts_are_errors_not_passes(self):
        for reply in ("not json", '{"claims": []}', '{"claims": "x"}'):
            v = await verify.classify_claims(_DIAGNOSIS, "ev", llm=_LLM(reply))
            assert v.status == "errored", reply

    async def test_unknown_label_counts_as_unsupported(self):
        v = await verify.classify_claims(
            _DIAGNOSIS, "ev", llm=_LLM(_claims((_LEAK_CLAIM, "probably"))))
        assert v.status == "unsupported" and v.counts()["none"] == 1

    async def test_unreadable_verdict_withholds_the_fix_turn(self, mocker):
        runs = await _watchtower_runs(mocker, grounding=None)
        assert len(runs) == 1 and runs[0]["auto"] is False


class TestFlagOff:
    def test_graph_topology_unchanged(self, mocker):
        mocker.patch.object(cx.settings, "SELF_GOVERN_ENABLED", False)
        builder = cx.build_cortex_graph()
        assert "ground_check" not in builder.nodes
        assert ("synthesize", "remember") in builder.edges

    def test_flag_on_inserts_ground_check(self, mocker):
        mocker.patch.object(cx.settings, "SELF_GOVERN_ENABLED", True)
        builder = cx.build_cortex_graph()
        assert "ground_check" in builder.nodes
        assert ("synthesize", "ground_check") in builder.edges
        assert ("ground_check", "remember") in builder.edges
        assert ("synthesize", "remember") not in builder.edges
        builder.compile()

    async def test_watchtower_single_auto_approved_turn(self, mocker):
        runs = await _watchtower_runs(mocker, grounding=None, self_govern=False)
        assert len(runs) == 1 and runs[0]["auto"] is True
        assert "apply the appropriate fix" in runs[0]["ask"]

    async def test_v2_graph_is_not_gated(self, mocker):
        # The verdict only exists on the Cortex graph; gating V2 would silently disable A3.
        runs = await _watchtower_runs(mocker, grounding=None, cortex=False)
        assert len(runs) == 1 and runs[0]["auto"] is True


async def _watchtower_runs(mocker, *, grounding, self_govern=True, cortex=True):
    """Drive `_investigate` at A3 on an allowlisted finding; return every turn it ran."""
    runs: list[dict] = []

    async def fake_run_session(ask, session_id, user_id, user_role, auto_approve, trigger_source):
        runs.append({"ask": ask, "auto": auto_approve})

    async def empty_stream(sid, heartbeat_interval=5.0):
        return
        yield

    mocker.patch("app.agent.workflow.run_session", side_effect=fake_run_session)
    mocker.patch("app.streaming.emitter.prepare_session")
    mocker.patch("app.streaming.emitter.stream", side_effect=empty_stream)
    mocker.patch("app.autonomy.watchtower.a3_allowed", return_value=True)
    mocker.patch.object(watchtower.settings, "SELF_GOVERN_ENABLED", self_govern)
    mocker.patch.object(watchtower.settings, "CORTEX_V4_ENABLED", cortex)
    mocker.patch.object(watchtower.settings, "MEMORY_PROSPECTIVE", False)
    mocker.patch.object(watchtower, "_turn_grounding", new=mocker.AsyncMock(return_value=grounding))
    finding = Finding(playbook="CrashLoopBackOff", cluster_id="c1", namespace="dev",
                      object_name="web-1", evidence="pod status=CrashLoopBackOff")
    await watchtower._investigate(finding, "A3")
    return runs
