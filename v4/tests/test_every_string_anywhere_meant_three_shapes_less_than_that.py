"""`_scrub_value` promised "every string *anywhere*"; three shapes went straight past it.

The flight recorder is the tamper-evident decision log, so what survives redaction here is
written to Postgres and kept. An earlier pass (2026-08-20) fixed the walk that only went one
level deep, and left the docstring claiming a universal: *"Redact every string anywhere in a
payload, not only the ones at the top."* A universal claim is a checkable inventory. Measured
2026-08-24 with `REFLEXION_REDACT_SECRETS=true`, all three kept the token verbatim:

    {"attributes": {"kubectl … --token=AKIA…": "ok"}}   a dict KEY — only values were walked
    {"x": {... 9 levels ... "kubectl … --token=AKIA…"}} past the depth bound, the whole
                                                       remaining subtree was returned untouched
    {"tags": {"kubectl … --token=AKIA…"}}              a set matched no branch, and
                                                       `json.dumps(..., default=str)` then wrote
                                                       `str(the_set)` into the column

The depth bound is the one worth naming. It exists so a self-referential structure cannot hang
the drain task — a good reason — but `return value` at the bound means the guard against a cycle
was also a hole in the redaction, and it failed **open** in a module whose output is permanent.
It now returns a marker instead.

Keys needed a narrower tool, not the same one. `redact_secrets("token")` and
`redact_secrets("password")` both return `"# <redacted-line>"`, so redacting keys with it would
rename two ordinary field names to the same string and silently merge two fields of an audit
record into one. `redact_identifier` applies only the substitutions that identify secret
*material*. Collisions between genuinely-secret keys are disambiguated rather than merged:
losing a field from a tamper-evident record in order to hide something in it is the wrong trade.
"""

from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.db import flight_recorder as fr
from app.utils.redact import redact_identifier, redact_secrets

TOKEN = "AKIAIOSFODNN7EXAMPLEAKIAIOSFODNN7EXAMPLE"
CMD = f"kubectl get po --token={TOKEN}"


@pytest.fixture(autouse=True)
def _redaction_on(monkeypatch):
    monkeypatch.setattr(settings, "REFLEXION_REDACT_SECRETS", True)


def _persisted(payload: dict) -> str:
    """What actually reaches the column — `record()` writes `json.dumps(payload, default=str)`,
    so testing the dict alone would miss anything `default=str` stringifies on the way out."""
    return json.dumps(fr._scrub(payload), default=str)


# ── 1. the three shapes ───────────────────────────────────────────────────────────────────────


class TestTheThreeShapesThatEscaped:
    def test_a_secret_used_as_a_dict_key(self):
        assert TOKEN not in _persisted({"attributes": {CMD: "ok"}})

    def test_a_secret_key_at_the_top_level(self):
        assert TOKEN not in _persisted({CMD: "ok"})

    def test_a_secret_below_the_depth_bound(self):
        value: object = CMD
        for _ in range(fr._MAX_SCRUB_DEPTH + 4):
            value = {"x": value}
        assert TOKEN not in _persisted(value)  # type: ignore[arg-type]

    def test_a_secret_in_a_set(self):
        assert TOKEN not in _persisted({"tags": {CMD}})

    def test_a_secret_in_a_frozenset(self):
        assert TOKEN not in _persisted({"tags": frozenset({CMD})})

    def test_a_secret_in_a_set_nested_in_a_list(self):
        assert TOKEN not in _persisted({"a": [{"b": {CMD}}]})


# ── 2. the depth bound fails closed now ───────────────────────────────────────────────────────


class TestTheDepthBoundFailsClosed:
    def test_the_bound_returns_a_marker_not_the_subtree(self):
        value: object = {"secret": CMD, "other": "harmless"}
        for _ in range(fr._MAX_SCRUB_DEPTH + 1):
            value = {"x": value}
        persisted = _persisted(value)  # type: ignore[arg-type]
        assert fr._TOO_DEEP in persisted
        assert "harmless" not in persisted, "an unscanned subtree must not be emitted at all"

    def test_the_bound_still_stops_a_cycle(self):
        """The reason the bound exists. A self-referential payload must terminate, not hang."""
        node: dict = {"name": "loop"}
        node["self"] = node
        assert fr._TOO_DEEP in _persisted({"root": node})

    def test_a_scalar_at_the_bound_is_not_replaced(self):
        """Only containers are refused. An `int` at depth has nothing left to scan, and
        blanking it would lose data for no gain."""
        value: object = 42
        for _ in range(fr._MAX_SCRUB_DEPTH + 1):
            value = {"x": value}
        assert "42" in _persisted(value)  # type: ignore[arg-type]

    def test_a_string_at_the_bound_is_still_redacted(self):
        value: object = CMD
        for _ in range(fr._MAX_SCRUB_DEPTH):
            value = {"x": value}
        assert TOKEN not in _persisted(value)  # type: ignore[arg-type]


# ── 3. keys keep their meaning ────────────────────────────────────────────────────────────────


class TestKeysAreNotDestroyedInTheProcess:
    @pytest.mark.parametrize("key", ["token", "password", "ki.action", "pre_state",
                                     "episode_id", "authorization"])
    def test_an_ordinary_field_name_survives_intact(self, key):
        """These are field *names*, not credentials. `redact_secrets` turns `token` and
        `password` into the same drop marker — which is why keys use `redact_identifier`."""
        assert fr._scrub({key: "v"}) == {key: "v"}

    def test_the_narrower_tool_is_actually_narrower(self):
        assert redact_secrets("token") != "token"
        assert redact_identifier("token") == "token"

    def test_two_secret_keys_do_not_merge_into_one(self):
        scrubbed = fr._scrub({"a": {TOKEN: 1, TOKEN[::-1]: 2}})
        assert len(scrubbed["a"]) == 2, "an audit record must not lose a field to redaction"
        assert TOKEN not in json.dumps(scrubbed)

    def test_a_non_string_key_is_left_alone(self):
        assert fr._scrub({"a": {1: "x", None: "y"}})["a"].keys() == {1, None}


# ── 4. nothing else changed ───────────────────────────────────────────────────────────────────


class TestOrdinaryPayloadsAreUntouched:
    def test_a_normal_payload_round_trips(self):
        payload = {"attributes": {"ki.action": "kubectl get po"},
                   "steps": [{"description": "check the rollout"}],
                   "count": 3, "ok": True}
        assert fr._scrub(payload) == payload

    def test_the_flag_still_switches_the_whole_thing_off(self, monkeypatch):
        monkeypatch.setattr(settings, "REFLEXION_REDACT_SECRETS", False)
        payload = {"attributes": {CMD: "ok"}}
        assert fr._scrub(payload) is payload

    def test_nested_strings_are_still_not_re_capped(self):
        """Pinned by the existing comment: `_MAX_FIELD_CHARS` bounds a top-level field, and
        shrinking an already-capped nested capture would cost it its restorability."""
        # Not `"a" * n` — a long unbroken alphanumeric run is *token*-shaped, so
        # `redact_secrets` replaces it on its own merits and the test would pass for the wrong
        # reason. A real captured pre-state is prose.
        long = ("the rollout captured this state " * 70)[:fr._MAX_FIELD_CHARS + 500]
        assert len(fr._scrub({"rollback_point": {"pre_state": [long]}})
                   ["rollback_point"]["pre_state"][0]) == len(long)

    def test_a_top_level_string_is_still_capped(self):
        long = ("the rollout captured this state " * 70)[:fr._MAX_FIELD_CHARS + 500]
        assert len(fr._scrub({"note": long})["note"]) <= fr._MAX_FIELD_CHARS + 10


# ── 5. the set branch does not make the chain hash unstable ───────────────────────────────────


class TestTheSetBranchIsDeterministic:
    def test_the_same_set_scrubs_to_the_same_list(self):
        """Sets have no order. Emitting iteration order would give one payload two different
        chain hashes across processes, which is a tamper-evidence problem of its own."""
        payload = {"tags": {"b", "a", "c"}}
        assert fr._scrub(payload)["tags"] == fr._scrub(payload)["tags"] == ["a", "b", "c"]
