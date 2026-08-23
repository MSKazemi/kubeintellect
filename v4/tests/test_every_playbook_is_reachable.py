r"""Every playbook must be reachable through the router that selects it.

`detect:` and `triggers:` are two independent features (docs/agent-behaviors.md says so, and
#114 is why). The previous two passes closed the `detect:` side: a predicate that compiles but
can never match is now rejected on the NL-authoring path and fails CI for a shipped playbook.
This file is the same property on the `triggers:` side, which feeds `match_playbooks` — the
thing that decides which playbook the agent is even shown.

The hole it guards is narrow and silent. `Trigger` has exactly three fields and
`_compile_trigger` reads them with `raw.get`, so any other key is dropped. A near-miss --
`reason_regex` instead of `event_reason_regex`, or one character short -- yields a Trigger with
all three regexes None. The playbook still loads, still counts toward the playbook total, still
passes the schema check, and `match_playbooks` iterates it forever without ever matching:

    >>> _compile_trigger({"reason_regex": "Evicted"})
    Trigger(pod_status_regex=None, event_reason_regex=None, event_message_regex=None)

`pb.triggers` is a non-empty tuple, so the `if not pb.triggers: continue` guard does not fire
either. The loader now warns; this file makes it fail.

`test_playbooks.py` checks specific playbooks against hand-written kubectl fixtures, which is
the right test for *what* each one matches. It cannot cover a playbook nobody wrote a fixture
for. This one derives its input from each trigger's own regex, so a new playbook is covered the
moment it is added.
"""
from __future__ import annotations

import pytest

from app.agent.playbooks import match_playbooks
from app.agent.playbooks.loader import _TRIGGER_KEYS, _compile_trigger, list_playbooks
from app.detectors.predicate_shape import enumerate_samples

_POD_HEADER = "NAME                     READY   STATUS    RESTARTS   AGE\n"
_EVENT_HEADER = "LAST SEEN   TYPE      REASON    OBJECT       MESSAGE\n"

_PLAYBOOKS = sorted(list_playbooks(), key=lambda pb: pb.name)


def _streams(field: str, sample: str) -> tuple[str, str]:
    """The (pods_out, events_out) pair `match_playbooks` would see for this sample."""
    if field == "pod_status_regex":
        return _POD_HEADER + f"pod-1   0/1   {sample}   3   5m\n", _EVENT_HEADER
    return (
        _POD_HEADER + "pod-1   1/1   Running   0   5m\n",
        _EVENT_HEADER + f"5m   Warning   {sample}   pod/pod-1   {sample}\n",
    )


def _cases():
    out = []
    for pb in _PLAYBOOKS:
        for i, trig in enumerate(pb.triggers):
            for field in _TRIGGER_KEYS:
                regex = getattr(trig, field)
                if regex is not None:
                    out.append((pb.name, i, field, regex))
    return out


_CASES = _cases()
_IDS = [f"{n}[{i}].{f.split('_')[0]}" for n, i, f, _ in _CASES]


@pytest.mark.parametrize(("name", "index", "field", "regex"), _CASES, ids=_IDS)
def test_every_trigger_can_select_its_playbook(name, index, field, regex):
    """Feed the router text built from the trigger's own regex; it must return the playbook.

    What this can and cannot catch, stated plainly. The sample is generated *from* the pattern,
    so the pattern matches it by construction — this does **not** prove the trigger matches
    anything a real cluster prints (unlike `detect:`, `triggers:` are searched against whole
    `kubectl get` output, where spaces and newlines are legitimate, so there is no domain shape
    to check against). What it does prove is that the wiring holds: that each field is searched
    against the stream it belongs to and that a matching line actually produces the playbook's
    name. Swap the two streams in `match_playbooks`, or drop a field from the loop, and every
    case here fails. The load-bearing liveness assertion is the next test.
    """
    for sample in enumerate_samples(regex):
        pods, events = _streams(field, sample)
        assert name in match_playbooks(pods, events), (
            f"{name} trigger #{index} {field}={regex.pattern!r} does not select its own "
            f"playbook for {sample!r} — the router can never route to it."
        )


@pytest.mark.parametrize("pb", _PLAYBOOKS, ids=[p.name for p in _PLAYBOOKS])
def test_every_playbook_has_a_trigger_that_could_fire(pb):
    """No playbook may be selectable only in theory."""
    assert pb.triggers, f"{pb.name} has no triggers — match_playbooks skips it entirely"
    live = [
        t for t in pb.triggers
        if any(getattr(t, k) is not None for k in _TRIGGER_KEYS)
    ]
    assert len(live) == len(pb.triggers), (
        f"{pb.name} has {len(pb.triggers) - len(live)} trigger entr(ies) with no recognised "
        f"key — silently dropped by _compile_trigger, and they can never match. "
        f"Expected one of {list(_TRIGGER_KEYS)}."
    )


def test_the_inventory_is_actually_covered():
    """Guard the guard: an empty registry would make both tests above pass vacuously."""
    assert len(_PLAYBOOKS) == 23, f"playbook count changed to {len(_PLAYBOOKS)}"
    assert len(_CASES) == 41, f"trigger-regex count changed to {len(_CASES)}"


def test_a_mistyped_trigger_key_is_exactly_the_shape_this_file_catches():
    """The failure mode, stated as a test so the docstring above cannot drift from the code."""
    dead = _compile_trigger({"reason_regex": "Evicted"}, "Ghost")
    assert all(getattr(dead, k) is None for k in _TRIGGER_KEYS)
    # and it is not filtered out anywhere — a tuple holding it is still truthy
    assert bool((dead,)) is True

    live = _compile_trigger({"event_reason_regex": "Evicted"}, "Ghost")
    assert live.event_reason_regex is not None
