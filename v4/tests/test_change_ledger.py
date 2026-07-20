"""Change ledger (v5 P1) — parse mutations, store, and feed change-first RCA."""
from __future__ import annotations

import pytest

from app.cortex.change_rca import recent_changes, render_change_prior
from app.memory.change_ledger import (
    _clear,
    parse_kubectl_change,
    record_from_commands,
    recent,
)


@pytest.fixture(autouse=True)
def clean_ledger():
    _clear()
    yield
    _clear()


class TestParse:
    def test_scale(self):
        r = parse_kubectl_change("kubectl scale deploy/web --replicas=3", 100.0, "demo")
        assert r.kind == "scale" and r.target == "deploy/web" and r.namespace == "demo"

    def test_set_image(self):
        r = parse_kubectl_change("kubectl set image deploy/web app=nginx:1.27", 100.0)
        assert r.kind == "image" and r.target == "deploy/web"

    def test_apply(self):
        r = parse_kubectl_change("kubectl apply -f manifest.yaml", 100.0)
        assert r.kind == "apply" and r.target == "manifest.yaml"  # first non-flag token

    def test_rollout(self):
        r = parse_kubectl_change("kubectl rollout restart deploy/api", 100.0)
        assert r.kind == "rollout" and r.target == "restart"

    def test_without_kubectl_prefix(self):
        r = parse_kubectl_change("delete pod/web", 100.0)
        assert r.kind == "delete" and r.target == "pod/web"

    def test_read_verb_is_not_a_change(self):
        assert parse_kubectl_change("kubectl get pods", 100.0) is None

    def test_empty_is_none(self):
        assert parse_kubectl_change("", 100.0) is None


class TestLedger:
    def test_record_and_recent(self):
        n = record_from_commands("cl-1", ["kubectl scale deploy/web --replicas=2"], 100.0, "demo")
        assert n == 1
        got = recent("cl-1")
        assert len(got) == 1 and got[0].kind == "scale"

    def test_read_commands_are_skipped(self):
        n = record_from_commands("cl-1", ["kubectl get pods", "kubectl scale deploy/x --replicas=1"], 100.0)
        assert n == 1

    def test_namespace_filter(self):
        record_from_commands("cl-1", ["kubectl scale deploy/a --replicas=1"], 100.0, "demo")
        record_from_commands("cl-1", ["kubectl scale deploy/b --replicas=1"], 101.0, "prod")
        assert len(recent("cl-1", namespace="demo")) == 1
        assert len(recent("cl-1")) == 2

    def test_ring_buffer_bounded(self):
        cmds = [f"kubectl scale deploy/d{i} --replicas=1" for i in range(250)]
        for c in cmds:
            record_from_commands("cl-1", [c], 100.0)
        assert len(recent("cl-1")) == 200          # _MAX_PER_CLUSTER

    def test_isolated_per_cluster(self):
        record_from_commands("cl-1", ["kubectl delete pod/x"], 100.0)
        assert recent("cl-2") == []


class TestFeedsChangeRca:
    def test_recording_wires_the_rca_source(self):
        # Before recording, change_rca's default source is empty.
        assert recent_changes("cl-1") == []
        record_from_commands("cl-1", ["kubectl set image deploy/web c=nginx:2"], 100.0, "demo")
        # After recording, change_rca reads the ledger (install_as_change_source ran).
        changes = recent_changes("cl-1")
        assert len(changes) == 1 and changes[0].kind == "image"
        prior = render_change_prior(changes)
        assert "deploy/web" in prior and "consider these FIRST" in prior
