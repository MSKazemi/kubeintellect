"""`match_playbooks` returns candidates, and several matching at once is normal.

`docs/agent-behaviors.md` tells playbook authors that co-firing with existing playbooks is
not a reason to withhold a new one, and names the exact example below. That is a behavioural
claim about the router, so it is pinned here rather than left to rot in prose -- a contributor
assessing whether their playbook is "too generic to add" reads that paragraph and acts on it.

The bar a new playbook has to clear is narrower than "matches alone": it must not fire when
nothing is wrong, and it must not be the only thing pointing at a different failure.
"""
from __future__ import annotations

from app.agent.playbooks import match_playbooks

# An ordinary events stream: one failed image pull, one restart back-off.
_EVENTS = (
    "NAMESPACE LAST SEEN TYPE REASON OBJECT MESSAGE\n"
    'shop 2m Warning Failed pod/api-1 Failed to pull image "x": not found\n'
    "shop 1m Warning BackOff pod/api-1 Back-off restarting failed container\n"
)


def test_one_ordinary_events_stream_routes_to_several_playbooks():
    """If this ever returns one name, the docs paragraph above is wrong."""
    matched = set(match_playbooks("", _EVENTS))
    assert {
        "CrashLoopBackOff",
        "CreateContainerConfigError",
        "ImagePullBackOff",
        "InitContainerFailing",
    } <= matched, (
        "docs/agent-behaviors.md names these four as co-firing on this stream; "
        f"got {sorted(matched)}"
    )


def test_the_broad_failed_reason_triggers_are_still_broad():
    """The two deliberately-broad triggers the docs cite by name."""
    only_failed = (
        "NAMESPACE LAST SEEN TYPE REASON OBJECT MESSAGE\n"
        "shop 2m Warning Failed pod/api-1 something went wrong\n"
    )
    matched = set(match_playbooks("", only_failed))
    assert {"ImagePullBackOff", "CreateContainerConfigError"} <= matched, (
        "both carry event_reason_regex: 'Failed'; if that is narrowed, the docs "
        f"paragraph explaining why co-firing is normal needs rewriting. got {sorted(matched)}"
    )


def test_an_empty_snapshot_routes_nowhere():
    """Guard the guard: matching everything would make the assertions above vacuous."""
    assert match_playbooks("", "") == []
