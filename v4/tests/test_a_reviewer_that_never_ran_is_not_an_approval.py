"""The adversarial RCA reviewer must never report health it does not have.

Two defects, both measured on 2026-08-24 by driving the real `review_rca`/`render_review_note`
pair, both of the same family the standing audit hunts — *a signal that reports success,
emptiness or health when the underlying state is none of those*:

1. `render_review_note` returned `""` when `errored=True`. `graph.py` appends the note only
   `if note:`, so **a reviewer that died and a reviewer that approved produced a byte-identical
   answer.** The user's only cue that verification happened is the absence of a caveat — which is
   also what a total instrument failure looks like. Fail-open is right; fail-*silent* is not.

2. The confidence line was rendered `if review.confidence:`, so it disappeared for exactly
   `0.0` — the reviewer stating it has **no confidence in the RCA at all**, the loudest verdict
   in the contract. The caveat block was quietest at maximum alarm. And `0.0` was also the
   fallback for an absent or unparseable field, so "said zero" and "said nothing" were one value.

Both are pinned here in *both* directions: every assertion that a state renders something is
paired with proof that a neighbouring state renders something else.
"""

from __future__ import annotations

import pytest

from app.cortex.verify import RcaReview, render_review_note, review_rca


class _LLM:
    """Minimal chat model. `reply=None` raises, standing in for a reviewer outage."""

    def __init__(self, reply: str | None) -> None:
        self.reply = reply
        self.calls = 0

    async def ainvoke(self, _messages):  # noqa: ANN001, ANN202
        self.calls += 1
        if self.reply is None:
            raise RuntimeError("reviewer is down")

        class _R:
            content = self.reply

        return _R()


def _flagged(**over) -> RcaReview:
    kw = {"supported": False, "confidence": 0.3, "unsupported": ["the DNS theory"]}
    kw.update(over)
    return RcaReview(**kw)


# ── 1. errored is a third state, not the clean state ─────────────────────────────────────────


class TestAReviewerThatNeverRanIsVisible:
    def test_errored_renders_a_block(self):
        note = render_review_note(_flagged(errored=True, confidence=None))
        assert note.strip(), "a reviewer that did not run rendered nothing at all"
        assert "NOT PERFORMED" in note

    def test_errored_is_not_byte_identical_to_a_clean_review(self):
        clean = render_review_note(RcaReview(supported=True, confidence=0.9))
        dead = render_review_note(RcaReview(supported=True, confidence=None, errored=True))
        # The exact confusion: `review_rca` fails open to supported=True, so these two differ
        # in nothing but `errored`.
        assert clean == "", "vacuity guard: the clean case must be the empty one"
        assert dead != clean

    def test_the_errored_block_does_not_claim_the_answer_is_wrong(self):
        # Fail-open is the documented principle: a broken reviewer must not contradict a sound
        # answer. Saying "this was not checked" is a statement about the instrument.
        note = render_review_note(RcaReview(supported=True, confidence=None, errored=True))
        assert "flagged claims" not in note
        assert "absence of a finding, not a finding" in note

    def test_the_errored_block_does_not_list_stale_unsupported_items(self):
        # `errored` and a populated `unsupported` should not co-occur, but if they ever do the
        # items came from a reviewer that failed — they must not be presented as findings.
        note = render_review_note(_flagged(errored=True, unsupported=["a ghost claim"]))
        assert "a ghost claim" not in note
        assert "a ghost claim" in render_review_note(_flagged(unsupported=["a ghost claim"])), (
            "vacuity guard: the item must appear when the reviewer DID run"
        )

    def test_a_clean_review_is_still_silent(self):
        # The fix must not caveat every answer — that is how a caveat stops being read.
        assert render_review_note(RcaReview(supported=True, confidence=0.9)) == ""
        assert render_review_note(RcaReview(supported=True, confidence=0.0)) == ""
        assert render_review_note(RcaReview(supported=True, confidence=None)) == ""

    @pytest.mark.parametrize(
        ("supported", "unsupported"),
        [(True, []), (False, []), (True, ["x"]), (False, ["x"])],
    )
    def test_errored_wins_over_every_other_field(self, supported, unsupported):
        note = render_review_note(
            RcaReview(supported=supported, confidence=0.5, unsupported=unsupported, errored=True)
        )
        assert "NOT PERFORMED" in note


# ── 2. a stated zero is the loudest verdict, not a missing one ───────────────────────────────


class TestZeroConfidenceIsStated:
    def test_zero_confidence_is_rendered(self):
        note = render_review_note(_flagged(confidence=0.0))
        assert "_Reviewer confidence in the RCA: 0%._" in note

    def test_zero_and_thirty_both_render_a_line(self):
        # The one-sidedness: 30% was visible, 0% was not, and 0% is the worse news.
        assert "0%" in render_review_note(_flagged(confidence=0.0))
        assert "30%" in render_review_note(_flagged(confidence=0.3))

    def test_no_number_is_not_reported_as_a_number(self):
        note = render_review_note(_flagged(confidence=None))
        assert "_The reviewer stated no confidence value._" in note
        assert "%" not in note

    def test_a_stated_zero_and_no_number_render_differently(self):
        assert render_review_note(_flagged(confidence=0.0)) != render_review_note(
            _flagged(confidence=None)
        )

    def test_every_flagged_review_says_something_about_confidence(self):
        for conf in (None, 0.0, 0.001, 0.5, 1.0):
            note = render_review_note(_flagged(confidence=conf))
            assert "confidence" in note.lower(), f"confidence silently omitted for {conf!r}"

    @pytest.mark.parametrize(
        ("conf", "expected"), [(0.0, "0%"), (0.005, "0%"), (0.5, "50%"), (1.0, "100%")]
    )
    def test_rounding_never_turns_a_number_into_nothing(self, conf, expected):
        # 0.005 rounds to "0%" — still a printed number, which is the whole point.
        assert f"RCA: {expected}._" in render_review_note(_flagged(confidence=conf))


# ── 3. what the parser actually puts in that field ───────────────────────────────────────────


class TestParsedConfidence:
    async def test_a_stated_zero_survives_parsing(self):
        llm = _LLM('{"supported": false, "confidence": 0.0, "unsupported": ["x"]}')
        r = await review_rca("c", "e", llm=llm)
        assert llm.calls == 1, "vacuity guard: the reviewer was never invoked"
        assert r.confidence == 0.0 and r.errored is False

    @pytest.mark.parametrize(
        "body",
        [
            '{"supported": false, "unsupported": ["x"]}',  # key absent
            '{"supported": false, "confidence": "high", "unsupported": ["x"]}',  # not a number
            '{"supported": false, "confidence": null, "unsupported": ["x"]}',  # explicit null
            '{"supported": false, "confidence": [0.4], "unsupported": ["x"]}',  # wrong type
        ],
    )
    async def test_an_unusable_confidence_is_none_not_zero(self, body):
        r = await review_rca("c", "e", llm=_LLM(body))
        assert r.confidence is None
        assert r.errored is False, "an unusable number is not a reviewer failure"

    async def test_clamping_still_works_and_still_yields_a_number(self):
        assert (await review_rca("c", "e", llm=_LLM('{"confidence": 5}'))).confidence == 1.0
        assert (await review_rca("c", "e", llm=_LLM('{"confidence": -3}'))).confidence == 0.0

    async def test_a_dead_reviewer_reports_no_number(self):
        r = await review_rca("c", "e", llm=_LLM(None))
        assert r.errored is True and r.confidence is None
        assert r.supported is True, "fail-open must be preserved"

    async def test_an_unparseable_reply_reports_no_number(self):
        r = await review_rca("c", "e", llm=_LLM("looks fine to me"))
        assert r.errored is True and r.confidence is None


# ── 4. end to end: the six live outcomes are six distinct strings ────────────────────────────


class TestTheSixOutcomesAreDistinguishable:
    async def test_no_two_outcomes_render_the_same_text(self):
        replies = {
            "dead": None,
            "garbage": "sure, seems right",
            "flagged_zero": '{"supported": false, "confidence": 0.0, "unsupported": ["the DNS theory"]}',
            "flagged_low": '{"supported": false, "confidence": 0.3, "unsupported": ["the DNS theory"]}',
            "flagged_nonum": '{"supported": false, "unsupported": ["the DNS theory"]}',
            "clean": '{"supported": true, "confidence": 0.9, "unsupported": []}',
        }
        notes = {}
        for name, body in replies.items():
            notes[name] = render_review_note(await review_rca("c", "e", llm=_LLM(body)))

        # `dead` and `garbage` are the same *state* (the reviewer produced no verdict) and are
        # meant to be identical; every other pair must differ.
        assert notes["dead"] == notes["garbage"]
        distinct = {k: v for k, v in notes.items() if k != "garbage"}
        assert len(set(distinct.values())) == len(distinct), (
            "two different reviewer outcomes render the same answer: "
            f"{ {k: v[:60] for k, v in distinct.items()} }"
        )
        assert notes["clean"] == "", "vacuity guard: only the clean case may be silent"
        assert all(v for k, v in distinct.items() if k != "clean")
