"""`redact_secrets` was a YAML redactor that reported itself applied to any text.

Everything about the module is line-aware: `_LINE_RE` finds `key: value`, the k8s env idiom
(`- name: DB_PASSWORD` on one line, `value: hunter2` on the next) is tracked across lines, and
keys that are credentials by convention (`tls.key`) are recognised by name. All of it matched
the shape `kubectl -o yaml` produces. `kubectl -o json` — an equally ordinary read, and the
form the API returns — writes the same object as:

    "name": "DB_PASSWORD",
    "value": "hunter2-prod-db"

`_LINE_RE`'s key group is `[A-Za-z0-9_.\\-/]+` with no quotes, so neither line matched. Both
fell through to the free-text branch, and measured, the credential was stored **verbatim**:

    kubectl get pod    -o yaml  -> value: <redacted>
    kubectl get pod    -o json  -> "value": "hunter2-prod-db"     <- stored
    kubectl get secret -o yaml  -> tls.key: <redacted>
    kubectl get secret -o json  -> "tls.key": "LS0tLS1CRUdJTiBSU0E="   <- stored

Whether a credential was caught depended on the `-o` flag the caller happened to pass — which
is pass 95's lesson exactly, one component along: a control whose reach depends on the shape of
its caller's data is a coincidence, not a control.

The parser now captures the key's quote (back-referenced, so an opening quote requires a
closing one) and `_unwrap_value` hands the value's own punctuation back to the emitter, so a
redacted JSON document is still a JSON document.
"""
from __future__ import annotations

import json

import pytest

from app.utils.redact import _unwrap_value, redact_secrets

PASSWORD = "hunter2-prod-db"
KEYMATERIAL = "LS0tLS1CRUdJTiBSU0EgUFJJVkFURSBLRVktLS0t"
TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdefghij"
SECRETS = (PASSWORD, KEYMATERIAL, TOKEN)


def _as_yaml(obj, indent=0):
    """Render the same object the way `kubectl -o yaml` would (enough of it for this)."""
    pad = " " * indent
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}:")
                lines.append(_as_yaml(v, indent + 2))
            else:
                lines.append(f"{pad}{k}: {v}")
    elif isinstance(obj, list):
        for item in obj:
            rendered = _as_yaml(item, indent + 2).lstrip()
            lines.append(f"{pad}- {rendered}" if not isinstance(item, (dict, list))
                         else f"{pad}- " + _as_yaml(item, indent + 2).strip())
    return "\n".join(lines)


OBJECTS = {
    "Secret data": {
        "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "db-creds"},
        "data": {"password": KEYMATERIAL, "tls.key": KEYMATERIAL},
    },
    "pod env idiom": {
        "spec": {"containers": [{"name": "api", "env": [
            {"name": "DB_PASSWORD", "value": PASSWORD},
        ]}]},
    },
    "bearer token field": {"status": {"token": TOKEN}},
    "nothing secret": {"spec": {"replicas": 3, "image": "nginx:1.27"}},
}


class TestTheSameObjectIsRedactedInEitherFormat:

    @pytest.mark.parametrize("name", list(OBJECTS), ids=list(OBJECTS))
    def test_json_leaks_nothing_yaml_would_have_caught(self, name):
        obj = OBJECTS[name]
        as_json = redact_secrets(json.dumps(obj, indent=2), max_chars=None)
        leaked = [s for s in SECRETS if s in as_json]
        assert not leaked, f"{name}: -o json stored a credential -o yaml would have redacted"

    @pytest.mark.parametrize("name", list(OBJECTS), ids=list(OBJECTS))
    def test_yaml_still_leaks_nothing(self, name):
        as_yaml = redact_secrets(_as_yaml(OBJECTS[name]), max_chars=None)
        assert not [s for s in SECRETS if s in as_yaml], f"{name}: YAML regressed"

    def test_compact_json_leaks_nothing_either(self):
        out = redact_secrets(json.dumps(OBJECTS["pod env idiom"]), max_chars=None)
        assert PASSWORD not in out

    def test_an_object_with_no_secret_is_left_alone(self):
        doc = json.dumps(OBJECTS["nothing secret"], indent=2)
        assert redact_secrets(doc, max_chars=None) == doc


class TestTheRedactedJsonIsStillJson:

    def test_it_parses(self):
        doc = json.dumps(OBJECTS["Secret data"], indent=2)
        json.loads(redact_secrets(doc, max_chars=None))

    def test_the_keys_survive_so_the_record_stays_auditable(self):
        doc = json.dumps(OBJECTS["Secret data"], indent=2)
        out = json.loads(redact_secrets(doc, max_chars=None))
        assert set(out["data"]) == {"password", "tls.key"}
        assert out["data"]["password"] == "<redacted>"

    def test_the_env_name_is_kept_and_only_the_value_goes(self):
        doc = json.dumps(OBJECTS["pod env idiom"], indent=2)
        env = json.loads(redact_secrets(doc, max_chars=None))["spec"]["containers"][0]["env"][0]
        assert env == {"name": "DB_PASSWORD", "value": "<redacted>"}

    def test_non_string_values_keep_their_type_and_comma(self):
        doc = json.dumps({"spec": {"replicas": 3, "paused": False}}, indent=2)
        assert json.loads(redact_secrets(doc, max_chars=None)) == {
            "spec": {"replicas": 3, "paused": False}}

    def test_a_secret_key_opening_a_container_keeps_the_container(self):
        doc = json.dumps({"secretKeyRef": {"name": "db-creds", "key": "password"}}, indent=2)
        out = redact_secrets(doc, max_chars=None)
        assert out.count("{") == doc.count("{"), "the opener was dropped and the document broke"
        json.loads(out)


class TestTheValueSplitter:

    @pytest.mark.parametrize("raw,expected", [
        ('"hunter2",', ('"', "hunter2", '",')),
        ('"hunter2"', ('"', "hunter2", '"')),
        ("3,", ("", "3", ",")),
        ("hunter2", ("", "hunter2", "")),
        ("", ("", "", "")),
        ("{", ("", "{", "")),
        ('"a: b",', ('"', "a: b", '",')),
    ])
    def test_it_round_trips(self, raw, expected):
        assert _unwrap_value(raw) == expected
        open_q, inner, close_q = _unwrap_value(raw)
        assert f"{open_q}{inner}{close_q}" == raw


class TestTheQuotingIsNotGuessed:

    def test_an_unbalanced_quote_is_not_treated_as_a_json_key(self):
        """The key quote is back-referenced: an opening quote demands a closing one."""
        line = '  "password: still-open'
        assert PASSWORD not in redact_secrets(line, max_chars=None)

    def test_a_yaml_quoted_value_keeps_its_quotes(self):
        assert redact_secrets(f'password: "{PASSWORD}"', max_chars=None) == \
            'password: "<redacted>"'

    def test_a_plain_yaml_value_stays_unquoted(self):
        assert redact_secrets(f"password: {PASSWORD}", max_chars=None) == "password: <redacted>"
