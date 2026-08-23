"""The sensorium watches cluster-wide — so its *evidence* is where the blocklist has to land.

Pass 90 of the standing audit (T38), reached by re-asking pass 89's question — *is the channel
constrained, or only the content?* — of the second path to the cluster.

`run_kubectl` refuses `kubectl get pods -n kube-system` for every role. The sensorium runs
`kubectl get pods -A --watch` and `kubectl get events -A --watch` as **raw subprocesses**, not
through that tool, and the blocklist has no reference anywhere in `app/sensorium/`. That part is
deliberate and must stay: a watchtower that cannot see the infrastructure namespaces cannot tell
a quiet cluster from an unwatched one, which is the defect pass 80 closed.

What came with it was the free text. Measured 2026-08-20 by feeding the engine the observation
shape `k8s_watcher` actually emits:

    GET /v1/findings returned
      ns=kube-system     evidence=event reason=FailedMount message=MountVolume.SetUp failed for
                                  volume "creds": secret "kubeintellect-secrets" not found; token=eyJ…
      ns=kubeintellect   (same)
      ns=monitoring      (same)

Up to 140 characters of raw Kubernetes event message, from the namespaces this product blocks
*because they hold its own credentials* — and `GET /v1/findings` calls `recent_findings()` with
**no role parameter at all**, while `docs/security.md`'s RBAC table said infrastructure-namespace
access, *reads included*, was `❌ Blocked` for admin, operator and readonly alike.

An event `message` is arbitrary cluster text: mount failures name Secrets, image pulls name
registries and auth errors, probe failures quote URLs and payloads. Every other field a finding
carries is an enum or an object name. So the message is withheld for a blocked namespace and
everything else is kept — the operator still learns that coredns is crash-looping, which is the
legitimate reason the watcher is cluster-wide in the first place.

The RBAC table has been corrected in the same pass: it stated an absolute that the product does
not implement and, as a watchtower, cannot.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.detectors.engine import _WITHHELD_MESSAGE, DetectorEngine, _summarise, load_detectors
from app.sensorium.observations import Observation

SECRET_MSG = (
    'MountVolume.SetUp failed for volume "creds": secret "kubeintellect-secrets" '
    "not found; token=eyJhbGciOiJSUzI1NiJ9.LEAK"
)
BLOCKED = ["kube-system", "kubeintellect", "monitoring", "kube-public", "cert-manager"]


def _event(ns: str, message: str = SECRET_MSG, reason: str = "FailedMount",
           ts: float | None = None) -> Observation:
    return Observation(kind="event", cluster_id="c1", namespace=ns, name="pod-x",
                       fields={"reason": reason, "message": message,
                               "involved_kind": "Pod", "type": "Warning"},
                       ts=time.time() if ts is None else ts)


def _pod(ns: str, status: str = "CrashLoopBackOff", ts: float | None = None) -> Observation:
    return Observation(kind="pod_status", cluster_id="c1", namespace=ns, name="coredns-abc",
                       fields={"status": status, "watch_type": "MODIFIED", "node": "n1"},
                       ts=time.time() if ts is None else ts)


# ── 1. The message is withheld for a blocked namespace ────────────────────────

class TestTheEventMessageIsWithheld:
    @pytest.mark.parametrize("ns", BLOCKED)
    def test_the_raw_message_never_appears(self, ns):
        out = _summarise(_event(ns))
        assert "kubeintellect-secrets" not in out
        assert "eyJhbGciOiJSUzI1NiJ9.LEAK" not in out
        assert _WITHHELD_MESSAGE in out

    @pytest.mark.parametrize("ns", BLOCKED)
    def test_the_reason_survives(self, ns):
        """A Kubernetes `reason` is a defined enum, and it is the whole signal."""
        assert "reason=FailedMount" in _summarise(_event(ns))

    def test_an_unblocked_namespace_keeps_its_message(self):
        out = _summarise(_event("shop"))
        assert "MountVolume.SetUp failed" in out
        assert _WITHHELD_MESSAGE not in out

    def test_the_withheld_text_says_why(self):
        assert "protected namespace" in _WITHHELD_MESSAGE

    def test_folding_hardening(self):
        """Kubernetes namespaces are RFC 1123 lowercase, so this cannot occur from the watcher.

        Asserted anyway because pass 81 found eight guards that folded one side of the
        comparison only, and this comparison has two sides.
        """
        assert _WITHHELD_MESSAGE in _summarise(_event("  KUBE-SYSTEM  "))


# ── 2. Everything else the sensorium reports is unchanged ─────────────────────

class TestTheSignalIsKept:
    @pytest.mark.parametrize("ns", BLOCKED + ["shop"])
    def test_pod_status_is_an_enum_and_is_untouched(self, ns):
        assert _summarise(_pod(ns)) == "pod status=CrashLoopBackOff"

    @pytest.mark.parametrize("ns", BLOCKED + ["shop"])
    def test_node_status_is_untouched(self, ns):
        obs = Observation(kind="node_status", cluster_id="c1", namespace=ns, name="n1",
                          fields={"status": "NotReady"}, ts=time.time())
        assert _summarise(obs) == "node status=NotReady"

    def test_a_blocked_namespace_still_produces_a_finding(self):
        """Dropping it would blind the digest — the defect pass 80 closed."""
        eng = DetectorEngine(detectors=load_detectors(), cluster_id="c1", on_finding=lambda f: None)
        for i in range(4):
            eng.process(_pod("kube-system", ts=time.time() - 600 + i))
        fired = eng.tick(now=time.time() + 600)
        assert any(f.namespace == "kube-system" for f in fired)


# ── 3. End to end, through what /v1/findings actually returns ─────────────────

class TestWhatTheEndpointReturns:
    def _engine_with(self, namespaces):
        eng = DetectorEngine(detectors=load_detectors(), cluster_id="c1", on_finding=lambda f: None)
        for ns in namespaces:
            for i in range(4):
                eng.process(_event(ns, ts=time.time() - 600 + i))
            eng.tick(now=time.time() + 600)
        return eng

    def test_no_blocked_namespace_message_reaches_the_payload(self):
        eng = self._engine_with(BLOCKED + ["shop"])
        payload = eng.recent_findings(limit=50)
        blocked_rows = [d for d in payload if d["namespace"] in BLOCKED]
        assert blocked_rows, "fixture produced no blocked-namespace findings"
        for row in blocked_rows:
            assert "kubeintellect-secrets" not in row["evidence"]
            assert _WITHHELD_MESSAGE in row["evidence"]

    def test_the_unblocked_row_is_the_control(self):
        eng = self._engine_with(["shop"])
        rows = [d for d in eng.recent_findings(limit=50) if d["namespace"] == "shop"]
        assert rows and "MountVolume.SetUp failed" in rows[0]["evidence"]


# ── 4. It follows the configured blocklist, not a literal ─────────────────────

class TestItFollowsTheSetting:
    def test_a_namespace_added_to_the_setting_is_withheld(self):
        with patch.object(settings, "KUBECTL_BLOCKED_NAMESPACES", "vault-system"):
            assert _WITHHELD_MESSAGE in _summarise(_event("vault-system"))

    def test_a_namespace_removed_from_the_setting_is_not(self):
        with patch.object(settings, "KUBECTL_BLOCKED_NAMESPACES", "vault-system"):
            assert "MountVolume.SetUp failed" in _summarise(_event("kube-system"))
