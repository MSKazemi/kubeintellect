"""The pre-fetched snapshot is a second kubectl, and it enforced none of the first one's policy.

`run_kubectl` is the guarded path: roles, HITL, blocked namespaces, blocked resources, output
filtering. `app/agent/nodes/context_fetcher._kubectl_snapshot` is a *second* executor, written
so the fixed pre-fetch does not pay for tool dispatch — and measured on 2026-08-20 it ran
whatever it was handed:

  * `kubectl get pods --all-namespaces` and `kubectl get events --all-namespaces
    --field-selector=type=Warning` were pasted verbatim into the coordinator's system prompt on
    **every** turn. The identical command through `run_kubectl` has its blocked-namespace rows
    removed; here the whole table went in, warning `MESSAGE` column included — the place a
    missing-secret event names the secret and a failing probe names the apiserver's address.
  * `targeted_investigator` builds `-n <namespace>` from a `TARGETED:` line the **model** wrote.
    `run_kubectl` refuses `describe pod etcd-control-plane -n kube-system`; the same read here
    returned the full description into the prompt.

The tests below are grouped by the layer they cover, so a reverted layer fails its own group.
"""
from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app.agent.nodes import context_fetcher as cf
from app.agent.nodes.context_fetcher import (
    _data_row_count,
    _filter_snapshot_output,
    _kubectl_snapshot,
    _scan_snapshot,
    _snapshot_refusal,
)
from app.agent.workflow import targeted_investigator
from app.core.config import settings

PODS_TABLE = (
    "NAMESPACE       NAME                  READY   STATUS             RESTARTS   AGE\n"
    "default         shop-api-7d9f         1/1     Running            0          5d\n"
    "kube-system     etcd-control-plane    1/1     Running            0          9d\n"
    "kubeintellect   kubeintellect-server  1/1     Running            0          2d\n"
    "monitoring      loki-0                0/1     CrashLoopBackOff   7          3h\n"
)

EVENTS_TABLE = (
    "NAMESPACE       LAST SEEN   TYPE      REASON        OBJECT        MESSAGE\n"
    "kube-system     4m          Warning   Unhealthy     pod/kube-api  Liveness probe failed:"
    " HTTP probe failed with statuscode: 500 on https://10.0.0.4:6443/livez\n"
    "kubeintellect   1m          Warning   FailedMount   pod/ki-server MountVolume.SetUp failed"
    ' for volume "api-secrets" : secret "openai-api-key" not found\n'
)


def _fake_kubectl(stdout: str, returncode: int = 0):
    def _run(cmd, **kwargs):
        proc = MagicMock()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = ""
        return proc
    return _run


# ── L1 · the cluster-wide table is filtered on the way back ───────────────────
class TestTheClusterWideSnapshotIsFiltered:
    def test_blocked_rows_do_not_reach_the_snapshot(self):
        out = _filter_snapshot_output(["get", "pods", "--all-namespaces"], PODS_TABLE)
        for ns in ("kube-system", "kubeintellect", "monitoring"):
            assert ns not in out

    def test_the_unblocked_row_survives(self):
        out = _filter_snapshot_output(["get", "pods", "--all-namespaces"], PODS_TABLE)
        assert "shop-api-7d9f" in out

    def test_the_header_survives(self):
        out = _filter_snapshot_output(["get", "pods", "--all-namespaces"], PODS_TABLE)
        assert out.splitlines()[0].startswith("NAMESPACE")

    def test_the_reader_is_told_the_listing_is_short(self):
        out = _filter_snapshot_output(["get", "pods", "--all-namespaces"], PODS_TABLE)
        assert "3 row(s) withheld" in out
        assert "NOT the complete set" in out

    def test_a_clean_table_is_returned_untouched(self):
        clean = "NAMESPACE   NAME\ndefault     a\nshop        b\n"
        assert _filter_snapshot_output(["get", "pods", "-A"], clean) == clean

    def test_a_namespaced_read_is_not_row_filtered(self):
        # `-n <allowed>` was already decided; its rows carry no NAMESPACE column at all.
        body = "NAME   READY\nmonitoring-lookalike   1/1\n"
        assert _filter_snapshot_output(["get", "pods", "-n", "shop"], body) == body

    @pytest.mark.parametrize("flag", ["-A", "--all-namespaces", "--all-namespaces=true"])
    def test_every_spelling_of_all_namespaces_filters(self, flag):
        out = _filter_snapshot_output(["get", "pods", flag], PODS_TABLE)
        assert "kube-system" not in out

    def test_the_event_message_column_is_what_this_protects(self):
        out = _filter_snapshot_output(["get", "events", "-A"], EVENTS_TABLE)
        assert "openai-api-key" not in out
        assert "6443/livez" not in out

    def test_a_fully_withheld_table_keeps_only_header_and_notice(self):
        out = _filter_snapshot_output(["get", "events", "-A"], EVENTS_TABLE)
        assert _data_row_count(out) == 0
        assert "2 row(s) withheld" in out


# ── L2 · a blocked namespace is refused before the subprocess runs ────────────
class TestABlockedNamespaceIsRefusedAtTheFunnel:
    @pytest.mark.parametrize("ns", sorted(settings.kubectl_blocked_namespaces))
    def test_every_blocked_namespace_is_refused(self, ns):
        assert _snapshot_refusal(["get", "pods", "-n", ns]) is not None

    @pytest.mark.parametrize("spelling", [
        ["-n", "kube-system"], ["--namespace", "kube-system"],
        ["--namespace=kube-system"], ["-n=kube-system"], ["-nkube-system"],
    ])
    def test_every_pflag_spelling_is_refused(self, spelling):
        assert _snapshot_refusal(["get", "pods", *spelling]) is not None

    def test_the_refusal_names_the_namespace(self):
        assert "kube-system" in (_snapshot_refusal(["get", "pods", "-n", "kube-system"]) or "")

    def test_case_does_not_open_the_gate(self):
        assert _snapshot_refusal(["get", "pods", "-n", "Kube-System"]) is not None

    def test_an_ordinary_namespace_passes(self):
        assert _snapshot_refusal(["get", "pods", "-n", "shop"]) is None

    def test_the_cluster_wide_pre_fetch_still_runs(self):
        assert _snapshot_refusal(["get", "pods", "--all-namespaces"]) is None

    def test_no_subprocess_is_launched_for_a_refused_read(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("must not run")):
            ok, text = _kubectl_snapshot(["describe", "pod", "etcd", "-n", "kube-system"])
        assert ok is False
        assert text.startswith("[Protected]")

    def test_an_allowed_read_still_reaches_kubectl(self):
        with patch.object(subprocess, "run", _fake_kubectl("NAME   READY\napi    1/1\n")):
            ok, text = _kubectl_snapshot(["get", "pods", "-n", "shop"])
        assert ok is True
        assert "api" in text


# ── L3 · which cluster, and as whom, is not the model's to choose ─────────────
class TestConnectionAndIdentityAreRefusedHereToo:
    @pytest.mark.parametrize("flag", [
        "--kubeconfig=/tmp/evil.yaml", "--server=http://attacker.example.com:8080",
        "--context=admin", "--token=abc", "--as=system:masters",
        "--insecure-skip-tls-verify", "--cluster=other",
    ])
    def test_a_connection_flag_is_refused(self, flag):
        assert _snapshot_refusal(["get", "pods", flag]) is not None

    def test_the_namespace_slot_cannot_smuggle_a_kubeconfig(self):
        # `TARGETED: namespace=--kubeconfig=/tmp/evil.yaml` — the regex captures \\S+, so the
        # namespace slot accepts a flag. It is not a blocked namespace; it is a blocked flag.
        refusal = _snapshot_refusal(["describe", "pod", "x", "-n", "--kubeconfig=/tmp/evil.yaml"])
        assert refusal is not None and "kubeconfig" in refusal

    def test_an_ordinary_read_carries_no_connection_flag(self):
        assert _snapshot_refusal(["get", "events", "-A", "--field-selector=type=Warning"]) is None


# ── L4 · the model-written namespace reaches a refusal, not a description ─────
class TestTargetedInvestigationHonoursTheBlocklist:
    def _run(self, namespace: str, stdout: str = "Name: etcd\nNamespace: kube-system\n"):
        with patch.object(subprocess, "run", _fake_kubectl(stdout)):
            return asyncio.run(targeted_investigator({
                "session_id": "t",
                "targeted_investigation": {
                    "namespace": namespace, "pod": "etcd-control-plane", "issue": "probe fail"},
                "cluster_snapshot": "",
            }))

    def test_a_protected_namespace_is_refused(self):
        snap = self._run("kube-system")["cluster_snapshot"]
        assert "[Protected]" in snap

    def test_nothing_from_the_protected_namespace_is_rendered(self):
        snap = self._run("kube-system")["cluster_snapshot"]
        assert "Pod Description" not in snap
        assert "Name: etcd" not in snap

    def test_the_model_is_told_not_to_infer(self):
        assert "do not infer" in self._run("monitoring")["cluster_snapshot"]

    def test_no_kubectl_runs_for_a_protected_target(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("must not run")):
            asyncio.run(targeted_investigator({
                "session_id": "t",
                "targeted_investigation": {
                    "namespace": "kubeintellect", "pod": "p", "issue": "i"},
                "cluster_snapshot": "",
            }))

    def test_the_flag_is_cleared_so_the_graph_does_not_loop(self):
        assert self._run("kube-system")["targeted_investigation"] is None

    def test_an_existing_snapshot_is_preserved(self):
        with patch.object(subprocess, "run", _fake_kubectl("x")):
            out = asyncio.run(targeted_investigator({
                "session_id": "t",
                "targeted_investigation": {"namespace": "kube-system", "pod": "p", "issue": "i"},
                "cluster_snapshot": "## Cluster Snapshot\nearlier",
            }))
        assert out["cluster_snapshot"].startswith("## Cluster Snapshot\nearlier")

    def test_an_ordinary_namespace_is_still_investigated(self):
        snap = self._run("shop", stdout="Name: shop-api\n")["cluster_snapshot"]
        assert "Pod Description" in snap
        assert "shop-api" in snap


# ── L5/L6 · the notice is not a pod, and not a warning ────────────────────────
class TestThePolicyLineIsNotClusterData:
    def test_a_withheld_notice_is_not_counted_as_a_pod(self):
        filtered = _filter_snapshot_output(["get", "pods", "-A"], PODS_TABLE)
        _, _, pod_count = _scan_snapshot(filtered, "", pods_ok=True, events_ok=True)
        assert pod_count == 1

    def test_a_withheld_notice_does_not_flip_has_issues(self):
        filtered = _filter_snapshot_output(["get", "pods", "-A"], PODS_TABLE)
        has_issues, _, _ = _scan_snapshot(filtered, "", pods_ok=True, events_ok=True)
        assert has_issues is False   # the only CrashLoopBackOff was in `monitoring`

    def test_a_visible_crashloop_still_flips_has_issues(self):
        table = PODS_TABLE.replace("monitoring      loki-0", "shop            loki-0")
        filtered = _filter_snapshot_output(["get", "pods", "-A"], table)
        has_issues, _, _ = _scan_snapshot(filtered, "", pods_ok=True, events_ok=True)
        assert has_issues is True

    def test_a_fully_withheld_event_table_is_not_a_warning(self):
        filtered = _filter_snapshot_output(["get", "events", "-A"], EVENTS_TABLE)
        _, has_warnings, _ = _scan_snapshot(PODS_TABLE, filtered, pods_ok=True, events_ok=True)
        assert has_warnings is False

    def test_a_surviving_event_is_still_a_warning(self):
        table = EVENTS_TABLE + "shop   1m   Warning   BackOff   pod/x   restarting\n"
        filtered = _filter_snapshot_output(["get", "events", "-A"], table)
        _, has_warnings, _ = _scan_snapshot(PODS_TABLE, filtered, pods_ok=True, events_ok=True)
        assert has_warnings is True

    @pytest.mark.parametrize("table,expected", [
        ("", 0),
        ("NAMESPACE  NAME\n", 0),
        ("NAMESPACE  NAME\ndefault  a\n", 1),
        ("NAMESPACE  NAME\ndefault  a\n[Protected] 2 row(s) withheld — x\n", 1),
    ])
    def test_data_row_count(self, table, expected):
        assert _data_row_count(table) == expected


# ── the end-to-end shape the prompt actually receives ─────────────────────────
class TestTheRenderedSnapshot:
    def _snapshot(self):
        def _run(cmd, **kwargs):
            proc = MagicMock()
            proc.returncode = 0
            proc.stderr = ""
            proc.stdout = EVENTS_TABLE if "events" in cmd else PODS_TABLE
            return proc
        with patch.object(subprocess, "run", _run):
            return asyncio.run(cf.context_fetcher({"session_id": "t"}))

    @pytest.mark.parametrize("ns", sorted(settings.kubectl_blocked_namespaces))
    def test_no_blocked_namespace_appears_in_the_prompt(self, ns):
        assert ns not in self._snapshot()["cluster_snapshot"]

    @pytest.mark.parametrize("secret", ["openai-api-key", "6443/livez", "etcd-control-plane"])
    def test_the_infrastructure_detail_is_gone(self, secret):
        assert secret not in self._snapshot()["cluster_snapshot"]

    def test_the_visible_workload_is_still_there(self):
        assert "shop-api-7d9f" in self._snapshot()["cluster_snapshot"]

    def test_the_pod_count_matches_what_the_tool_would_return(self):
        assert self._snapshot()["snapshot_pod_count"] == 1

    def test_the_withholding_is_stated_not_implied(self):
        assert "withheld" in self._snapshot()["cluster_snapshot"]
