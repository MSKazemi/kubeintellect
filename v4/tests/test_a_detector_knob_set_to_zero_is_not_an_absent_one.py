"""`entry.get(k) or default` cannot tell "not set" from "set to zero".

Every trend-predicate knob was read that way, so an author who wrote a value of `0` got the
default instead — silently, with the loaded detector reporting a configuration nobody had
written. Measured 2026-08-24 before the fix:

    author wrote        : window=0 horizon=0 eta_within=0 min_r2=0
    detector actually is: window=30 horizon=120 eta_within=30 min_r2=0.5
    anything said?      : no

The consequences run in opposite directions, which is why one rule does not cover them:

* `min_r2: 0` is a **real setting**. `engine.project_eta` gates on `if r2 < min_r2`, so zero
  deliberately disables the fit-quality check. Restoring 0.5 makes the detector *quieter*
  than authored — a false negative, the failure nobody notices.
* the three interval knobs at zero produce a predicate the engine can never fire, which is
  the dead-detector trap this module already refuses to ship for promql-only blocks.

So zero is honoured where it means something and refused **out loud** where it means a
no-op. It is never swapped in silence.
"""
import pathlib

import pytest

from app.detectors.models import parse_detect_block


def block(tp: dict | None = None, **raw):
    body = {"metric": "container_memory_working_set_bytes", "threshold": 1e9}
    body.update(tp or {})
    return parse_detect_block("authored-by-hand", {"trend_predicates": [body], **raw})


def tp(**kw):
    return block(kw).trend_predicates[0]


class TestZeroIsHonouredWhereItMeansSomething:
    def test_min_r2_zero_survives(self):
        assert tp(min_r2=0).min_r2 == 0.0

    def test_min_r2_zero_is_not_the_default(self):
        # Vacuity guard: the assertion above is only meaningful if 0.0 != the default.
        assert tp().min_r2 == 0.5

    def test_min_r2_one_survives(self):
        assert tp(min_r2=1).min_r2 == 1.0

    def test_min_r2_zero_logs_nothing(self, caplog):
        # It is a valid setting, not a rescued mistake.
        with caplog.at_level("WARNING"):
            tp(min_r2=0)
        assert not [r for r in caplog.records if "min_r2" in r.message]


class TestZeroIsRefusedOutLoudWhereItMeansANoOp:
    @pytest.mark.parametrize(
        "key,bad,default",
        [("window_minutes", 0, 30), ("window_minutes", -5, 30),
         ("projection_horizon_minutes", 0, 120),
         ("fire_if_eta_within_minutes", 0, 30)],
    )
    def test_the_default_is_used(self, key, bad, default):
        assert getattr(tp(**{key: bad}), key) == default

    @pytest.mark.parametrize(
        "key,bad", [("window_minutes", 0), ("projection_horizon_minutes", 0),
                    ("fire_if_eta_within_minutes", 0)])
    def test_and_it_says_so(self, key, bad, caplog):
        with caplog.at_level("WARNING"):
            tp(**{key: bad})
        msg = "\n".join(r.message for r in caplog.records)
        assert key in msg
        assert "is NOT the one that was written" in msg

    def test_a_valid_value_logs_nothing(self, caplog):
        # Vacuity guard: warning on every parse is warning on none of them.
        with caplog.at_level("WARNING"):
            tp(window_minutes=15, projection_horizon_minutes=60,
               fire_if_eta_within_minutes=10, min_r2=0.8)
        assert not [r for r in caplog.records if "was written" in r.message]

    def test_an_absent_knob_logs_nothing(self, caplog):
        with caplog.at_level("WARNING"):
            tp()
        assert not [r for r in caplog.records if "was written" in r.message]

    def test_the_reason_given_is_specific_to_the_field(self, caplog):
        # A generic "would make this unable to fire" was wrong for debounce_seconds, where a
        # negative value makes the detector fire MORE. A false log line is still a false line.
        with caplog.at_level("WARNING"):
            block(debounce_seconds=-5)
        msg = "\n".join(r.message for r in caplog.records)
        assert "disables debouncing entirely" in msg
        assert "can never fire" not in msg


class TestTheOtherInvalidShapes:
    def test_out_of_range_min_r2_falls_back(self, caplog):
        with caplog.at_level("WARNING"):
            v = tp(min_r2=1.7).min_r2
        assert v == 0.5
        assert "r²" in "\n".join(r.message for r in caplog.records)

    def test_a_non_numeric_knob_falls_back_and_says_so(self, caplog):
        with caplog.at_level("WARNING"):
            v = tp(min_r2="high").min_r2
        assert v == 0.5
        assert "is not a number" in "\n".join(r.message for r in caplog.records)

    def test_an_explicit_null_is_treated_as_absent(self, caplog):
        # YAML `min_r2:` with no value. Absent, not invalid — no warning.
        with caplog.at_level("WARNING"):
            v = tp(min_r2=None).min_r2
        assert v == 0.5
        assert not [r for r in caplog.records if "min_r2" in r.message]

    def test_a_negative_debounce_is_refused(self, caplog):
        with caplog.at_level("WARNING"):
            b = block(debounce_seconds=-5)
        assert b.debounce_seconds == 0
        assert "debounce_seconds" in "\n".join(r.message for r in caplog.records)

    def test_a_zero_debounce_is_kept_and_silent(self, caplog):
        # 0 is the documented default here — "fires immediately" — not a mistake.
        with caplog.at_level("WARNING"):
            b = block(debounce_seconds=0)
        assert b.debounce_seconds == 0
        assert not [r for r in caplog.records if "debounce" in r.message]

    def test_a_positive_debounce_survives(self):
        assert block(debounce_seconds=90).debounce_seconds == 90


class TestNothingElseMoved:
    def test_the_shipped_playbooks_still_parse_to_their_authored_values(self):
        # The real corpus is the strongest guard against a fallback that changed meaning.
        # `_load_all()` hands back already-parsed DetectBlocks, so the authored values have to
        # come from the YAML itself — comparing a parse against its own output proves nothing.
        import yaml

        from app.detectors.models import parse_detect_block

        root = pathlib.Path(
            "packages/kubeintellect-server/app/agent/playbooks")
        if not root.exists():  # running from inside the server package
            root = pathlib.Path("app/agent/playbooks")
        checked = 0
        for f in sorted(root.glob("*.yaml")):
            raw = yaml.safe_load(f.read_text()) or {}
            det = raw.get("detect")
            if not isinstance(det, dict):
                continue
            parsed = parse_detect_block(raw.get("name", f.stem), det)
            assert parsed is not None, f"{f.name} no longer parses"
            if "debounce_seconds" in det:
                assert parsed.debounce_seconds == int(det["debounce_seconds"])
                checked += 1
            authored = det.get("trend_predicates") or []
            assert len(parsed.trend_predicates) == len(authored)
            for got, want in zip(parsed.trend_predicates, authored):
                for key, cast in (("window_minutes", int), ("projection_horizon_minutes", int),
                                  ("fire_if_eta_within_minutes", int), ("min_r2", float)):
                    if key in want:
                        assert getattr(got, key) == cast(want[key]), (
                            f"{f.name}: {key} authored {want[key]!r}, parsed "
                            f"{getattr(got, key)!r}")
                        checked += 1
        assert checked > 0, "no shipped playbook sets any of these knobs — this proves nothing"
