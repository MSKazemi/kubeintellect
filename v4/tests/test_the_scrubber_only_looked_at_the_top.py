"""The recorder's secret scrubber walked one level, and said it had walked the payload.

`REFLEXION_REDACT_SECRETS` is documented as *apply secret/URL/token scrubbing before
persisting*, and `flight_recorder._scrub`'s docstring said *redact secrets from string
fields*. What it did was iterate `payload.items()` and redact the values that happened to be
strings — every string inside a list or a nested dict went to the hash-chained
`decision_log` verbatim. Which payloads were safe was an accident of who wrote the call site:

    {"command": "kubectl … --token=AKIA…"}                  -> redacted (top level)
    {"attributes": {"ki.action": "kubectl … --token=AKIA…"}} -> STORED VERBATIM
    {"steps": [{"description": "… --token=AKIA…"}]}          -> STORED VERBATIM
    {"pre_state": ["password: hunter2"]}                     -> STORED VERBATIM
                                                                (safe only because
                                                                 kubectl_tool redacts it
                                                                 itself, first)

A gate whose reach depends on the shape of the caller's dict is not a gate. This suite plants
a credential at every nesting shape the recorder actually stores and asserts none survives.

The second half is pass 94's lens again. `kubectl_tool._capture_note` describes what
redaction did to a rollback capture, and it counted **three** of the six markers
`redact_secrets` can emit — a hand-copied subset. A capture whose only redaction was a PEM
private key or a Secret's `data:` block was therefore reported to the operator as, in full,
"redacted": the least informative note for the most secret-dense object there is. The
vocabulary now lives once, in `redact.count_redactions`, and a test reads it against the
literals in `redact.py`'s own source.
"""
from __future__ import annotations

import inspect
import json
import re

import pytest

from app.db import flight_recorder
from app.db.flight_recorder import _MAX_FIELD_CHARS, _MAX_SCRUB_DEPTH, _scrub
from app.tools.kubectl_tool import _ROLLBACK_MAX_CHARS, _capture_note
from app.utils import redact
from app.utils.redact import REDACTION_MARKERS, count_redactions, redact_secrets

SECRET = "AKIAIOSFODNN7EXAMPLEKEYID1234567890abcdefGHIJ"
SECRET_LINE = f"password: {SECRET}"


@pytest.fixture(autouse=True)
def _redaction_on(mocker):
    mocker.patch.object(flight_recorder.settings, "REFLEXION_REDACT_SECRETS", True)


# ── 1. Every nesting shape the recorder actually stores ───────────────────────────────

# Each entry is a payload shaped like one the recorder really receives.
NESTED_PAYLOADS = {
    "tool_call.command (top level)": {"command": f"kubectl create secret generic s "
                                                 f"--from-literal={SECRET_LINE}"},
    "rollback_point.pre_state": {"pre_state": [SECRET_LINE]},
    "ki_otel_span.attributes": {"attributes": {"ki.action": f"kubectl --token={SECRET}"}},
    "plan.steps": {"steps": [{"description": f"run with {SECRET_LINE}"}]},
    "dict in list in dict": {"a": {"b": [{"c": SECRET_LINE}]}},
    "tuple member": {"a": (SECRET_LINE,)},
    "deep but within bound": {"a": {"b": {"c": {"d": SECRET_LINE}}}},
}


class TestNoStringEscapesTheScrubber:

    @pytest.mark.parametrize("name", list(NESTED_PAYLOADS), ids=list(NESTED_PAYLOADS))
    def test_the_secret_does_not_reach_the_log(self, name):
        assert SECRET not in json.dumps(_scrub(NESTED_PAYLOADS[name]), default=str), (
            f"{name}: the credential was written to the decision_log verbatim"
        )

    def test_a_secret_key_name_survives_so_the_record_stays_auditable(self):
        out = _scrub({"pre_state": [SECRET_LINE]})
        assert "password" in out["pre_state"][0]

    def test_non_strings_are_left_alone(self):
        payload = {"restorable": False, "seq": 7, "eta": 1.5, "nothing": None, "empty": ""}
        assert _scrub(payload) == payload

    def test_the_flag_still_turns_it_off(self, mocker):
        mocker.patch.object(flight_recorder.settings, "REFLEXION_REDACT_SECRETS", False)
        payload = {"attributes": {"ki.action": SECRET_LINE}}
        assert _scrub(payload) is payload

    def test_structure_and_key_order_are_preserved(self):
        """The payload is hashed into the chain, so scrubbing must not reshape it."""
        payload = {"z": "a", "a": {"n": ["x", "y"]}, "m": 1}
        out = _scrub(payload)
        assert list(out) == list(payload)
        assert list(out["a"]) == list(payload["a"])
        assert isinstance(out["a"]["n"], list) and len(out["a"]["n"]) == 2

    def test_a_tuple_becomes_a_list_which_is_what_json_would_have_done(self):
        assert _scrub({"a": ("x", "y")})["a"] == ["x", "y"]

    def test_the_result_is_still_serialisable(self):
        json.dumps(_scrub(NESTED_PAYLOADS["plan.steps"]), default=str)

    def test_scrubbing_is_idempotent(self):
        once = _scrub({"a": {"b": SECRET_LINE}})
        assert _scrub(once) == once


class TestTheWalkIsBounded:

    def test_a_cycle_does_not_hang(self):
        loop: dict = {"name": "x"}
        loop["self"] = loop
        _scrub(loop)   # must return, not recurse forever

    def test_below_the_bound_a_string_is_still_reached(self):
        payload: dict = {}
        cursor = payload
        for _ in range(_MAX_SCRUB_DEPTH - 1):
            nxt: dict = {}
            cursor["n"] = nxt
            cursor = nxt
        cursor["v"] = SECRET_LINE
        assert SECRET not in json.dumps(_scrub(payload))


class TestTheCapKeepsItsCurrentReach:
    """The cap is a storage limit on a payload *field*; widening its reach is a separate call."""

    def test_a_top_level_field_is_still_capped(self):
        out = _scrub({"output": "a" * (_MAX_FIELD_CHARS + 500)})
        assert len(out["output"]) <= _MAX_FIELD_CHARS

    def test_a_nested_capture_is_not_shrunk(self):
        """A `rollback_point` pre-state arrives capped at 4000 by its producer."""
        capture = "spec:\n" + "\n".join(f"  field{i}: value{i}" for i in range(400))
        assert len(capture) > _MAX_FIELD_CHARS
        out = _scrub({"pre_state": [capture]})
        assert len(out["pre_state"][0]) > _MAX_FIELD_CHARS
        assert not out["pre_state"][0].endswith("[...]")

    def test_redact_secrets_can_be_asked_not_to_truncate(self):
        text = "field: value\n" * 500
        assert not redact_secrets(text, max_chars=None).endswith("[...]")
        assert redact_secrets(text, max_chars=100).endswith("[...]")


# ── 2. One marker vocabulary, read from the module that emits them ────────────────────

class TestTheMarkerVocabularyIsNotCopiedByHand:

    def test_every_marker_in_the_source_is_declared(self):
        """Reads both artefacts: the literals `redact.py` emits, and the tuple that lists them."""
        source = inspect.getsource(redact)
        body = source[source.index("def _is_secret_name"):]     # skip the declarations above
        found = set(re.findall(r"<redacted[a-z\-]*>", body))
        declared = {m.replace("# ", "") for m in REDACTION_MARKERS}
        assert found <= declared, f"marker(s) emitted but never counted: {sorted(found - declared)}"

    def test_the_markers_do_not_contain_one_another(self):
        """Counting each independently is only correct while they stay disjoint."""
        overlapping = [(a, b) for a in REDACTION_MARKERS for b in REDACTION_MARKERS
                       if a != b and a in b]
        assert not overlapping, f"markers overlap, so counts double: {overlapping}"

    @pytest.mark.parametrize("doc,label", [
        ("apiVersion: v1\nkind: Secret\ndata:\n  password: aHVudGVyMg==\n", "Secret data block"),
        ("data:\n  tls.key: |\n    -----BEGIN RSA PRIVATE KEY-----\n    MIIEowIBAAKCAQEA\n"
         "    -----END RSA PRIVATE KEY-----\n", "PEM private key"),
        ("spec:\n  containers:\n    - env:\n        - name: DB_PASSWORD\n"
         "          value: hunter2\n", "env value"),
    ])
    def test_a_redacted_capture_is_always_counted(self, doc, label):
        """Asserted through `_capture_note`, the real consumer — not through the counter."""
        kept = redact_secrets(doc, max_chars=_ROLLBACK_MAX_CHARS)
        assert kept.rstrip("\n") != doc.rstrip("\n"), f"{label}: nothing was redacted"
        dropped, replaced = count_redactions(kept)
        assert dropped + replaced > 0, f"{label}: redacted, but nothing counted it"
        note = _capture_note(doc, kept, _ROLLBACK_MAX_CHARS)
        assert note != "redacted", (
            f"{label}: the operator is told only 'redacted' — no count of what was taken"
        )

    def test_the_note_says_what_happened_to_a_private_key(self):
        doc = ("data:\n  tls.key: |\n    -----BEGIN RSA PRIVATE KEY-----\n"
               "    MIIEowIBAAKCAQEA\n    -----END RSA PRIVATE KEY-----\n")
        kept = redact_secrets(doc, max_chars=_ROLLBACK_MAX_CHARS)
        note = _capture_note(doc, kept, _ROLLBACK_MAX_CHARS)
        assert note != "redacted", "a captured private key was described as, in full, 'redacted'"
        assert "value(s) replaced" in note

    def test_a_dropped_line_and_a_replaced_value_are_reported_apart(self):
        assert count_redactions("# <redacted-line>") == (1, 0)
        assert count_redactions("a: <redacted>") == (0, 1)

    def test_truncation_still_outranks_redaction_in_the_note(self):
        long = "field: value\n" * 2000
        kept = redact_secrets(long, max_chars=_ROLLBACK_MAX_CHARS)
        assert "truncated at" in _capture_note(long, kept, _ROLLBACK_MAX_CHARS)

    def test_an_untouched_capture_reports_nothing(self):
        assert count_redactions("spec:\n  replicas: 3\n") == (0, 0)
