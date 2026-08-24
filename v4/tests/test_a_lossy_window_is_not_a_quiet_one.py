"""A dropped observation is a gap in perception, so the digest has to say so too.

`app.detectors.perception` exists because two surfaces answered the same question about the
same window separately and disagreed: on 2026-08-20 `kq findings` refused to call a window
clear while `kq digest` said *"Quiet watch: no findings in the last 24h."* Its module
docstring states the remedy — *"the classification lives here once, and both surfaces read
it."*

On 2026-08-24 `kq findings` learned a third way to be blind: the watch queue sheds the
**oldest** observation when it overflows, rather than applying backpressure to `kubectl`, so
a fully connected sensorium can be losing perception with every other field reading healthy.
That rule was added to the CLI alone — which recreated precisely the divergence this module
was written to end. Measured immediately after, with one connected stream, Prometheus not in
play and `queue_stats() == {"shed_total": 4271, "high_water": 512}`:

    perception_gaps(...)  ->  []
    kq findings           ->  "Perception is lossy — the sensorium dropped 4271
                               observation(s) … not an all-clear"
    kq digest             ->  "Quiet watch: no findings in the last 24h."   (degraded: False)

`perception_gaps` promises to be *"empty exactly when every instrument was able to look"*.
The instrument looked; the observations were discarded between the watch and the detectors.

The knowledge now lives in the classifier, so both surfaces move together. The last class
below is the one that matters: it asserts the *property*, not the sentence.
"""
from __future__ import annotations

import asyncio

import pytest
from app.api.v1.endpoints.findings import list_findings
from app.core.config import settings
from app.detectors import perception, service
from app.detectors.perception import perception_gaps, perception_state
from app.digest import builder
from app.sensorium import k8s_watcher
from app.sensorium.k8s_watcher import StreamHealth, reset_queue_stats, reset_stream_health

QUIET = "Quiet watch: no findings in the last 24h."


class _Engine:
    detectors = tuple(range(20))
    trend_blind_since = None
    last_trend_error = None

    def recent_findings(self, **_k):
        return []


class _StubPool:
    """Answers truthfully with zero rows — a genuinely quiet, fully recorded night."""

    async def fetch(self, *_a, **_k):
        return []


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    reset_stream_health()
    reset_queue_stats()
    monkeypatch.setattr(service, "_engine", _Engine())
    monkeypatch.setattr(builder, "_get_pool", lambda: _StubPool())
    monkeypatch.setattr(settings, "FLIGHT_RECORDER_ENABLED", True)
    monkeypatch.setattr(settings, "USE_SQLITE", False)
    monkeypatch.setattr(settings, "WATCHTOWER_ENABLED", True)
    monkeypatch.setattr(settings, "PREDICTIVE_DETECTION_ENABLED", False)
    yield
    reset_stream_health()
    reset_queue_stats()


def _watching() -> None:
    h = StreamHealth("get pods -A")
    h.connected, h.stopped, h.last_error = True, False, None
    k8s_watcher._streams["get pods -A"] = h


def _shed(total: int, high_water: int = 512) -> None:
    """Set the counters the watch loop increments when it drops an observation."""
    k8s_watcher._shed_total = total
    k8s_watcher._queue_high_water = high_water


def _digest(hours: float = 24.0) -> dict:
    return asyncio.run(builder.build_digest(hours))


def _findings() -> dict:
    return asyncio.run(list_findings(limit=100, since=0.0))


class TestTheMeasuredDisagreement:
    def test_a_lossy_window_is_not_reported_as_quiet(self):
        _watching()
        _shed(4271)
        d = _digest()
        assert QUIET not in d["summary"], (
            "the digest called a window quiet after 4271 observations were thrown away")
        assert d["degraded"] is True

    def test_the_reason_names_the_queue_and_the_size(self):
        _watching()
        _shed(4271)
        reasons = _digest()["degraded_reasons"]
        lossy = [r for r in reasons if "queue" in r]
        assert len(lossy) == 1, f"expected one queue reason, got {reasons}"
        assert "4271" in lossy[0], (
            "the reason named a loss without its size; 1 dropped event and 4271 are not the "
            "same night")
        assert "512" in lossy[0], "high-water is what says whether the loss is ongoing"

    def test_a_healthy_queue_is_still_a_quiet_watch(self):
        """The fix must not turn every quiet night into a degraded one."""
        _watching()
        _shed(0, high_water=7)
        d = _digest()
        assert d["summary"] == QUIET
        assert d["degraded"] is False and d["degraded_reasons"] == []


class TestTheClassifierCarriesIt:
    def test_the_state_reports_what_the_queue_did(self):
        _watching()
        _shed(9, high_water=64)
        st = perception_state(_Engine())
        assert (st.shed_total, st.queue_high_water) == (9, 64)
        assert st.sensorium == "active", (
            "shedding must not be smuggled in by downgrading the sensorium — the stream "
            "genuinely is connected, and calling it 'stopped' would be a different lie")

    def test_a_gap_is_reported_for_a_connected_but_lossy_sensorium(self):
        _watching()
        _shed(3)
        assert perception_gaps(perception_state(_Engine())), (
            "`perception_gaps` promises to be empty only when every instrument could look")

    def test_the_disabled_path_still_reports_the_queue(self, monkeypatch):
        """A sensorium that has since gone away can still have shed while it was up.

        Reporting 0 there would be a claim, not the absence of one — and the disabled path
        builds its state positionally, which is exactly where a new field gets forgotten.
        """
        _shed(11)
        monkeypatch.setattr(service, "_engine", None)
        st = perception_state()
        assert st.sensorium == "disabled"
        assert st.shed_total == 11, "the disabled branch dropped the queue counters"

    def test_the_positional_construction_still_works(self):
        """The two new fields are defaulted last, like `sensorium_reason` before them."""
        st = perception.PerceptionState("active", 20, "off", 0, None, [])
        assert st.shed_total == 0 and st.queue_high_water == 0


class TestNeitherSurfaceCanDisagreeAgain:
    """The property this module exists for: one classifier, so both surfaces move together.

    Asserted by changing only the queue and checking that BOTH answers change — a
    per-surface rule would satisfy either half of this alone, which is how the defect got
    in the first time.
    """

    def test_shedding_moves_both_surfaces_at_once(self):
        _watching()
        _shed(0)
        assert _digest()["degraded"] is False
        assert _findings()["queue"]["shed_total"] == 0

        _shed(31)
        assert _digest()["degraded"] is True, "the digest did not follow the classifier"
        assert _findings()["queue"]["shed_total"] == 31, (
            "the findings surface did not follow the classifier")

    def test_the_two_surfaces_report_the_same_number(self):
        _watching()
        _shed(77)
        endpoint = _findings()["queue"]["shed_total"]
        reason = next(r for r in _digest()["degraded_reasons"] if "queue" in r)
        assert str(endpoint) in reason, (
            f"the endpoint reported {endpoint} dropped events and the digest's reason "
            f"disagrees: {reason!r}")
