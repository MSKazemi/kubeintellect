"""A cluster-scoped object has no namespace, and the safety model is built on namespaces.

The autonomy ladder decides what the watchtower may do *by namespace*: protected namespaces
are pinned to A0, per-namespace overrides tune the rest, and A3 auto-fix requires an explicit
`playbook/namespace` allowlist entry. That model is sound for Pods. It has no answer for a
Node, a PersistentVolume or a ClusterRole — and it answered anyway, with the configured
default.

Measured 2026-08-20 by feeding `_event_observation` a real-shaped Warning event about a Node
(`reason: NodeNotReady`, `involvedObject: {kind: Node, name: worker-1}` — no namespace field
anywhere, because there is no namespace):

    observation.namespace                          ''
    level_for_namespace('')                        A1        (the configured default)
    with AUTONOMY_LEVEL=A3, allowlist 'NodeNotReady/*':
    level_for_namespace('')                        A3
    a3_allowed('NodeNotReady', '')                 True      <-- auto-fix on a Node

`fnmatch('', '*')` is True, and `*` is the natural way to write "all my namespaces" in an
allowlist whose docstring advertises glob support. So an operator enabling auto-fix for their
own namespaces also, invisibly, enabled it for cluster-scoped objects — where an unattended
remediation (cordon, drain, delete node) is the least recoverable action the system can take.

Node events are not hypothetical: `NodeNotReady`, `Rebooted`, `KubeletHasDiskPressure` and
`NodeHasSufficientMemory` are among the most common Warning events in any cluster. The
`node_status` observation kind is declared and handled but not currently emitted by the
watcher; the Event path above is live.

Fixed by capping an unattributable namespace at A1 — investigate and report, never mutate —
and refusing A3 outright. Observation is unaffected; only autonomous *action* is.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from app.autonomy.ladder import a3_allowed, at_least, level_for_namespace
from app.core.config import settings
from app.sensorium.k8s_watcher import _event_observation

NODE_EVENT = {"type": "ADDED", "object": {
    "kind": "Event", "type": "Warning", "reason": "NodeNotReady",
    "message": "Node worker-1 status is now: NodeNotReady",
    "involvedObject": {"kind": "Node", "name": "worker-1"},
    "metadata": {"name": "worker-1.17f", "uid": "x"},
    "lastTimestamp": "2099-01-01T00:00:00Z"}}


class TestTheReachablePath:
    def test_a_node_event_really_does_arrive_without_a_namespace(self):
        """If this ever stops being true the rest of this file is guarding nothing."""
        obs = _event_observation(NODE_EVENT, "cluster-1")
        assert obs is not None
        assert obs.namespace == ""
        assert obs.fields["involved_kind"] == "Node"

    def test_such_a_finding_is_investigated_but_never_auto_fixed(self):
        obs = _event_observation(NODE_EVENT, "cluster-1")
        with patch.object(settings, "AUTONOMY_LEVEL", "A3"), \
             patch.object(settings, "AUTONOMY_A3_ALLOWLIST", "NodeNotReady/*"):
            level = level_for_namespace(obs.namespace)
            assert level == "A1", f"a Node was raised to {level}"
            assert at_least(level, "A1"), "investigation should still happen"
            assert not a3_allowed("NodeNotReady", obs.namespace)


class TestAnUnattributableNamespaceIsCappedNotTrusted:
    @pytest.mark.parametrize("namespace", ["", None, "   "])
    def test_it_never_exceeds_investigate_only(self, namespace):
        with patch.object(settings, "AUTONOMY_LEVEL", "A3"):
            assert level_for_namespace(namespace) == "A1"

    @pytest.mark.parametrize("pattern", ["*", "**", "NodeNotReady/*"])
    def test_no_allowlist_glob_can_reach_it(self, pattern):
        with patch.object(settings, "AUTONOMY_LEVEL", "A3"), \
             patch.object(settings, "AUTONOMY_A3_ALLOWLIST", f"CrashLoop/{pattern}"):
            assert not a3_allowed("CrashLoop", "")

    def test_the_a3_refusal_holds_on_its_own(self):
        """Isolate the second gate: with the level cap defeated, a3_allowed must still refuse.

        Removing this guard breaks no other test in this file, because the cap alone already
        blocks the path — so without this test it would be unproven defence-in-depth. It is
        kept deliberately: `a3_allowed` is the last thing between a detector firing and an
        unattended `kubectl` write, and it should not depend on another function for its
        correctness.
        """
        import app.autonomy.ladder as ladder
        with patch.object(ladder, "level_for_namespace", lambda ns: "A3"), \
             patch.object(settings, "AUTONOMY_A3_ALLOWLIST", "CrashLoop/*"):
            assert ladder.a3_allowed("CrashLoop", "prod"), "the stub should allow a real ns"
            assert not ladder.a3_allowed("CrashLoop", ""), "empty namespace reached auto-fix"

    def test_a_per_namespace_override_cannot_raise_it_either(self):
        with patch.object(settings, "AUTONOMY_NAMESPACE_LEVELS", "=A3"), \
             patch.object(settings, "AUTONOMY_A3_ALLOWLIST", "CrashLoop/*"):
            assert level_for_namespace("") == "A1"
            assert not a3_allowed("CrashLoop", "")

    def test_the_cap_is_a_ceiling_not_a_floor(self):
        """A deployment that pinned everything to A0 must stay at A0, not be raised to A1."""
        with patch.object(settings, "AUTONOMY_LEVEL", "A0"):
            assert level_for_namespace("") == "A0"

    def test_observation_is_not_suppressed(self):
        """A1 still means the investigation opens — this is a cap on action, not on looking."""
        with patch.object(settings, "AUTONOMY_LEVEL", "A1"):
            assert at_least(level_for_namespace(""), "A1")


class TestTheLadderAgreesWithTheKubectlToolAboutNames:
    """Two components deciding 'is this namespace protected' must not drift."""

    @pytest.mark.parametrize("spelling", [
        "kube-system", "  kube-system  ", "KUBE-SYSTEM", "Kube-System", "kube-system\n",
    ])
    def test_every_spelling_of_a_blocked_namespace_pins_to_a0(self, spelling):
        assert level_for_namespace(spelling) == "A0", f"{spelling!r} escaped the pin"

    def test_an_override_written_with_odd_spacing_still_applies(self):
        with patch.object(settings, "AUTONOMY_NAMESPACE_LEVELS", " Prod = A2 "):
            assert level_for_namespace("prod") == "A2"

    def test_it_follows_the_configured_blocklist(self):
        with patch.object(settings, "KUBECTL_BLOCKED_NAMESPACES", "vault-system"):
            assert level_for_namespace("vault-system") == "A0"
            assert level_for_namespace("kube-system") != "A0", "hardcoded blocklist survived"


class TestNoOverBlocking:
    def test_ordinary_namespaces_are_unaffected(self):
        with patch.object(settings, "AUTONOMY_LEVEL", "A3"), \
             patch.object(settings, "AUTONOMY_A3_ALLOWLIST", "CrashLoop/*"):
            assert level_for_namespace("prod") == "A3"
            assert a3_allowed("CrashLoop", "prod")

    def test_a_namespaced_event_still_carries_its_namespace(self):
        doc = {"type": "ADDED", "object": {
            "kind": "Event", "type": "Warning", "reason": "BackOff",
            "message": "Back-off restarting failed container",
            "involvedObject": {"kind": "Pod", "name": "web-1", "namespace": "prod"},
            "metadata": {"name": "web-1.17f"}, "lastTimestamp": "2099-01-01T00:00:00Z"}}
        obs = _event_observation(doc, "cluster-1")
        assert obs.namespace == "prod"
        with patch.object(settings, "AUTONOMY_LEVEL", "A3"), \
             patch.object(settings, "AUTONOMY_A3_ALLOWLIST", "BackOff/prod"):
            assert a3_allowed("BackOff", obs.namespace)

    def test_the_protected_pin_still_beats_everything(self):
        with patch.object(settings, "AUTONOMY_NAMESPACE_LEVELS", "kube-system=A3"), \
             patch.object(settings, "AUTONOMY_A3_ALLOWLIST", "CrashLoop/kube-system"):
            assert level_for_namespace("kube-system") == "A0"
            assert not a3_allowed("CrashLoop", "kube-system")
