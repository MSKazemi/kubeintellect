"""The event-staleness filter must never be off in silence — and must date UTC as UTC.

`_event_observation` drops replayed history so bootstrap-era warnings do not fire detectors
minutes after the fact. Two ways that filter used to fail, both measured 2026-08-24:

1. `_event_timestamp` returns `None` for an Event with no timestamp *and* for one whose
   timestamp cannot be parsed, and the call site skips the comparison entirely when it is
   `None`. Failing open is the right call for a watchtower — dropping an event we merely
   cannot date turns a formatting quirk into a missed incident — but it was silent, so
   "the filter did not run" was indistinguishable from "the filter ran and passed it".

2. A Kubernetes timestamp with no timezone suffix parses to a *naive* datetime, and
   `.timestamp()` interprets a naive datetime as LOCAL time. On this host (CEST, +2) a
   10-minute-old OOMKilling event was dated 110 minutes old and dropped by a watch that had
   been connected for 20 minutes — a missed detection, not a cosmetic error. East of UTC
   that direction drops current events; west of it, replayed history looks current.
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.sensorium import k8s_watcher as W


def _event(ts_field: str | None = "lastTimestamp", raw: str | None = None) -> dict:
    obj: dict = {
        "kind": "Event",
        "type": "Warning",
        "reason": "OOMKilling",
        "message": "Memory cgroup out of memory",
        "involvedObject": {"kind": "Pod", "namespace": "prod", "name": "web-1"},
    }
    if ts_field is not None and raw is not None:
        obj[ts_field] = raw
    return {"object": obj}


def _iso(minutes_ago: float, *, tz: bool = True) -> str:
    when = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return when.isoformat().replace("+00:00", "Z") if tz else when.replace(tzinfo=None).isoformat()


@pytest.fixture(autouse=True)
def _fresh_watch(monkeypatch):
    """Each test gets its own watch epoch and its own once-per-reason warning state."""
    monkeypatch.setattr(W, "_watch_epoch", time.time())
    monkeypatch.setattr(W, "_staleness_unchecked", set())
    yield


class TestAnEventWeCannotDateIsProcessedButNeverInSilence:
    """Fail open, out loud. Both halves are load-bearing."""

    def test_an_event_with_no_timestamp_at_all_is_still_processed(self):
        obs = W._event_observation(_event(ts_field=None), "c1")
        assert obs is not None, (
            "an Event we cannot date was dropped — a timestamp quirk must not become a missed "
            "incident")
        assert obs.fields["reason"] == "OOMKilling"

    def test_an_event_with_no_timestamp_says_so(self, caplog):
        with caplog.at_level(logging.WARNING):
            W._event_observation(_event(ts_field=None), "c1")
        assert any("no lastTimestamp" in r.message for r in caplog.records), (
            f"the staleness filter was skipped in silence; warnings were {
                [r.message for r in caplog.records]}")

    def test_the_warning_says_what_the_consequence_is(self, caplog):
        with caplog.at_level(logging.WARNING):
            W._event_observation(_event(ts_field=None), "c1")
        text = " ".join(r.message for r in caplog.records)
        assert "staleness" in text and "replayed history" in text, (
            f"the warning names a symptom but not what it costs the operator: {text!r}")

    def test_an_unparseable_timestamp_is_processed_and_warned(self, caplog):
        with caplog.at_level(logging.WARNING):
            obs = W._event_observation(_event(raw="Mon, 24 Aug 2026 05:15:00 GMT"), "c1")
        assert obs is not None, "an Event with an RFC1123 timestamp was dropped rather than kept"
        assert any("could not be parsed" in r.message for r in caplog.records)

    def test_the_unparseable_warning_quotes_the_offending_value(self, caplog):
        with caplog.at_level(logging.WARNING):
            W._event_observation(_event(raw="Mon, 24 Aug 2026 05:15:00 GMT"), "c1")
        text = " ".join(r.message for r in caplog.records)
        assert "Mon, 24 Aug 2026" in text, (
            f"the warning cannot be acted on without an example of what failed: {text!r}")

    def test_an_empty_string_timestamp_counts_as_absent_not_unparseable(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert W._event_observation(_event(raw=""), "c1") is not None
        assert any("no lastTimestamp" in r.message for r in caplog.records)


class TestTheWarningIsOncePerReasonPerWatchEpoch:
    """A watch that replays thousands of events must not emit thousands of identical lines."""

    def test_the_same_reason_is_not_repeated(self, caplog):
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                W._event_observation(_event(ts_field=None), "c1")
        hits = [r for r in caplog.records if "no lastTimestamp" in r.message]
        assert len(hits) == 1, f"one reason produced {len(hits)} warning lines"

    def test_a_different_reason_still_gets_its_own_line(self, caplog):
        with caplog.at_level(logging.WARNING):
            W._event_observation(_event(ts_field=None), "c1")
            W._event_observation(_event(raw="not a date"), "c1")
        assert any("no lastTimestamp" in r.message for r in caplog.records)
        assert any("could not be parsed" in r.message for r in caplog.records), (
            "deduplication swallowed a genuinely different reason")

    def test_a_reconnect_re_arms_the_warnings(self, caplog):
        with caplog.at_level(logging.WARNING):
            W._event_observation(_event(ts_field=None), "c1")
            W._staleness_unchecked.clear()  # what a (re)connect does
            W._event_observation(_event(ts_field=None), "c1")
        hits = [r for r in caplog.records if "no lastTimestamp" in r.message]
        assert len(hits) == 2, (
            f"after a reconnect the operator saw {len(hits)} warning(s); a new replay window is "
            "a new chance for the condition to matter")

    def test_the_watch_loop_clears_the_state_on_reconnect(self):
        """The re-arm above is only real if the production reconnect path actually does it."""
        import inspect
        src = inspect.getsource(W)
        epoch = src.index("_watch_epoch = time.time()")
        assert "_staleness_unchecked.clear()" in src[epoch:epoch + 400], (
            "nothing near the watch-epoch reset re-arms the once-per-reason warnings, so after "
            "the first reconnect the condition is permanently silent")


class TestADateableEventIsStillJudgedOnItsDate:
    """The vacuity guard in the other direction: the filter must still filter."""

    def test_replayed_history_is_still_dropped(self):
        assert W._event_observation(_event(raw=_iso(45)), "c1") is None, (
            "a 45-minute-old event survived the staleness filter — the filter is not filtering")

    def test_a_current_event_is_still_processed(self):
        assert W._event_observation(_event(raw=_iso(0)), "c1") is not None

    def test_a_current_event_produces_no_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            W._event_observation(_event(raw=_iso(0)), "c1")
        assert not caplog.records, (
            f"the healthy path is noisy, which trains operators to ignore the real warning: "
            f"{[r.message for r in caplog.records]}")

    def test_eventtime_and_creationtimestamp_are_accepted_too(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert W._event_observation(_event("eventTime", _iso(45)), "c1") is None
            assert W._event_observation(
                {"object": {**_event(ts_field=None)["object"],
                            "metadata": {"creationTimestamp": _iso(45)}}}, "c1") is None
        assert not caplog.records, "a supported timestamp field was treated as undateable"

    def test_a_non_event_document_is_untouched(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert W._event_observation({"object": {"kind": "Pod"}}, "c1") is None
        assert not caplog.records, "a Pod document was run through the Event staleness filter"


class TestANaiveTimestampIsDatedAsUtcNotAsLocalTime:
    """Kubernetes emits RFC3339 UTC. Reading it as local time shifts every age by the offset."""

    def test_a_naive_timestamp_is_dated_within_a_second_of_the_aware_one(self):
        aware = W._event_timestamp(_event(raw=_iso(10))["object"])
        naive = W._event_timestamp(_event(raw=_iso(10, tz=False))["object"])
        assert aware is not None and naive is not None
        assert abs(aware - naive) < 1.0, (
            f"the same instant written with and without a 'Z' was dated {abs(aware - naive):.0f} "
            "seconds apart — the naive form is being read in the host's local timezone")

    def test_a_naive_timestamp_is_not_read_in_local_time(self):
        """Explicit, offset-independent: compare against what local-time parsing would give."""
        raw = _iso(10, tz=False)
        got = W._event_timestamp(_event(raw=raw)["object"])
        local = datetime.fromisoformat(raw).timestamp()
        offset = datetime.now().astimezone().utcoffset()
        assert offset is not None
        if offset.total_seconds() != 0:  # on a UTC host the two readings coincide
            assert got != pytest.approx(local), (
                "the naive timestamp was interpreted as local time")
        assert got == pytest.approx(datetime.fromisoformat(raw).replace(tzinfo=UTC).timestamp())

    def test_a_recent_naive_event_survives_a_watch_that_has_been_up_a_while(self, monkeypatch):
        """The regression that made this a missed detection rather than a cosmetic one."""
        monkeypatch.setattr(W, "_watch_epoch", time.time() - 20 * 60)
        assert W._event_observation(_event(raw=_iso(10, tz=False)), "c1") is not None, (
            "a real OOMKilling from 10 minutes ago was dropped by a watch connected 20 minutes "
            "ago — it was dated with the host's UTC offset added")

    def test_a_naive_timestamp_that_is_genuinely_old_is_still_dropped(self, monkeypatch):
        monkeypatch.setattr(W, "_watch_epoch", time.time() - 20 * 60)
        assert W._event_observation(_event(raw=_iso(45, tz=False)), "c1") is None, (
            "dating naive timestamps as UTC must not turn the filter off for them")

    def test_fractional_seconds_beyond_microseconds_still_parse(self):
        """Kubernetes can emit more precision than `datetime` keeps; that is not an error."""
        clean = datetime.now(UTC).replace(microsecond=0)
        raw2 = clean.isoformat().replace("+00:00", "") + ".1234567Z"
        got = W._event_timestamp(_event(raw=raw2)["object"])
        assert got == pytest.approx(clean.timestamp(), abs=1.0), (
            "a 7-digit fractional-second timestamp was mis-dated")


class TestTheGracePeriodStillApplies:
    def test_an_event_just_inside_the_grace_window_is_kept(self):
        assert W._event_observation(
            _event(raw=_iso((W._EVENT_STALENESS_GRACE - 5) / 60)), "c1") is not None

    def test_an_event_well_outside_the_grace_window_is_dropped(self):
        assert W._event_observation(
            _event(raw=_iso((W._EVENT_STALENESS_GRACE + 60) / 60)), "c1") is None
