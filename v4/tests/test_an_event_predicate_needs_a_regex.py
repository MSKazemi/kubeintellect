"""An Event watch predicate with no reason_regex and no message_regex matches EVERY Warning event.

`WatchPredicate.matches` reads an absent regex as "anything" (`reason_regex is None or ...`), so
`{kind: Event}` — or `{kind: Event, involved_kind: Pod}` — fires on every Warning event (about a
Pod), routine BackOff/Unhealthy/FailedScheduling noise included. The ADR-012 branch noted it and
left it open; no shipped playbook relies on it (every shipped Event predicate names a reason),
so it is not an intended catch-all.

It is the Event-channel form of a predicate that fires on healthy objects, so it is refused where
those are: the NL-authoring gate (`validate_detect_block`) and promotion
(`review._liveness_error`, read by `promote_candidate`), and named — not dropped — by the DB loader
(`DetectBlock.fires_on_healthy`), the same evidence-preserving rule as `^Running$`. Before this
change the gate accepted both shapes below and the loader recorded nothing.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.playbooks.loader import list_playbooks
from app.detectors import authoring
from app.detectors.engine import load_db_detectors
from app.detectors.models import parse_detect_block
from app.detectors.predicate_shape import predicate_health_errors
from app.memory import service as mem_service
from app.sensorium.observations import Observation


def _event(reason="BackOff", message="Back-off restarting failed container"):
    return Observation(kind="event", cluster_id="t", namespace="shop", name="api-1", ts=1000.0,
                       fields={"event_type": "Warning", "reason": reason, "message": message,
                               "involved_kind": "Pod"})


@pytest.mark.parametrize("entry", [
    {"kind": "Event"},
    {"kind": "Event", "involved_kind": "Pod"},
])
class TestACatchAllEventPredicate:
    def test_it_really_matches_any_warning(self, entry):
        """The premise, so this file fails if `matches` ever changes."""
        block = parse_detect_block("x", {"watch_predicates": [entry]})
        assert block.watch_predicates[0].matches(_event("Unhealthy", "Readiness probe failed"))

    def test_the_shape_check_names_it(self, entry):
        block = parse_detect_block("x", {"watch_predicates": [entry]})
        (msg,) = predicate_health_errors(block.watch_predicates[0])
        assert "no reason_regex and no message_regex" in msg and "every Warning event" in msg

    def test_nl_authoring_refuses_it(self, entry):
        block, errors = authoring.validate_detect_block({"watch_predicates": [entry]}, "nl")
        assert block is None
        assert any("every Warning event" in e for e in errors), errors

    def test_the_loader_names_it_rather_than_dropping_it(self, entry, monkeypatch):
        class _Rows:
            async def fetch(self, *_a):
                return [{"name": "nl:any", "status": "shadow",
                         "predicate": json.dumps({"watch_predicates": [entry]})}]

        monkeypatch.setattr(mem_service, "_pool", _Rows())
        _active, (block,) = asyncio.run(load_db_detectors("t"))
        assert block.fires_on_healthy and "every Warning event" in block.fires_on_healthy[0]


@pytest.mark.parametrize("entry", [
    {"kind": "Event", "reason_regex": "^BackOff$"},
    {"kind": "Event", "message_regex": "Back-off restarting failed container"},
])
def test_one_regex_is_enough(entry):
    block, errors = authoring.validate_detect_block({"watch_predicates": [entry]}, "nl")
    assert errors == [] and block is not None


def test_no_shipped_playbook_is_a_catch_all():
    offenders = [
        p.name for p in list_playbooks() if p.detect is not None
        for w in p.detect.watch_predicates
        if w.kind == "Event" and w.reason_regex is None and w.message_regex is None
    ]
    assert offenders == []
