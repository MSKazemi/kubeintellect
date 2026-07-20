"""LOFA L2 offline replay — detector precision/recall over scenario signals.

Pre-registered kill criterion (ADR-006 / vision card §10):
    FP rate > 5%  OR  recall < 80%  → learned-detector synthesis is cut.

This is the *offline* form of the §4 replay procedure in
design/v4/detector-predicates.md: each fault-injecting eval scenario is
represented by the observation its fault produces (derived from the
scenario's setup.yaml fault type), fed through the real compiled detectors.
Negatives are healthy-cluster observations that must never fire. The live
on-cluster replay supersedes this when cluster time is available; this gate
catches predicate regressions on every test run.
"""
from __future__ import annotations

import time

import pytest

from app.detectors.engine import DetectorEngine, load_detectors
from app.sensorium.observations import Observation


def _obs(kind, ns, name, **fields):
    return Observation(
        kind=kind, cluster_id="l2", namespace=ns, name=name, fields=fields, ts=time.time()
    )


# Ground truth: (scenario, observation the injected fault produces, expected playbook).
# Derived from evaluation/scenarios/*/setup.yaml fault types per the map in
# design/v4/detector-predicates.md §4.
POSITIVES = [
    ("crashloop pod", _obs("pod_status", "s", "app-1", status="CrashLoopBackOff"), "CrashLoopBackOff"),
    ("bad image", _obs("pod_status", "s", "app-2", status="ImagePullBackOff"), "ImagePullBackOff"),
    ("bad image err", _obs("pod_status", "s", "app-3", status="ErrImagePull"), "ImagePullBackOff"),
    ("missing configmap", _obs("pod_status", "s", "app-4", status="CreateContainerConfigError"), "CreateContainerConfigError"),
    ("oom", _obs("pod_status", "s", "app-5", status="OOMKilled"), "OOMKilled"),
    (
        "oom event",
        _obs("event", "s", "app-5", reason="OOMKilling", event_type="Warning",
             message="Memory cgroup out of memory: Killed process", involved_kind="Pod"),
        "OOMKilled",
    ),
    ("stuck creating", _obs("pod_status", "s", "app-6", status="ContainerCreating"), "ContainerCreatingStuck"),
    ("init crash", _obs("pod_status", "s", "app-7", status="Init:CrashLoopBackOff"), "InitContainerFailing"),
    ("init progress", _obs("pod_status", "s", "app-8", status="Init:1/2"), "InitContainerFailing"),
    (
        "insufficient resources",
        _obs("event", "s", "app-9", reason="FailedScheduling", event_type="Warning",
             message="0/2 nodes are available: 2 Insufficient cpu.", involved_kind="Pod"),
        "PendingInsufficientResources",
    ),
    (
        "node selector",
        _obs("event", "s", "app-10", reason="FailedScheduling", event_type="Warning",
             message="0/2 nodes are available: 2 node(s) didn't match Pod's node affinity/selector.",
             involved_kind="Pod"),
        "PendingSchedulingConstraints",
    ),
    ("evicted", _obs("pod_status", "s", "app-11", status="Evicted"), "Evicted"),
    (
        "node notready",
        _obs("event", "s", "node-1", reason="NodeNotReady", event_type="Warning",
             message="Node ki-worker status is now: NodeNotReady", involved_kind="Node"),
        "NodeNotReady",
    ),
    (
        "readiness probe",
        _obs("event", "s", "app-12", reason="Unhealthy", event_type="Warning",
             message="Readiness probe failed: HTTP probe failed with statuscode: 500",
             involved_kind="Pod"),
        "ReadinessProbeFailing",
    ),
    (
        "job backoff",
        _obs("event", "s", "job-1", reason="BackoffLimitExceeded", event_type="Warning",
             message="Job has reached the specified backoff limit", involved_kind="Job"),
        "JobBackoffLimitExceeded",
    ),
    (
        "quota exceeded",
        _obs("event", "s", "rs-1", reason="FailedCreate", event_type="Warning",
             message='Error creating: pods "x" is forbidden: exceeded quota: compute-quota',
             involved_kind="ReplicaSet"),
        "QuotaExceeded",
    ),
    (
        "webhook denied",
        _obs("event", "s", "rs-2", reason="FailedCreate", event_type="Warning",
             message='Error creating: admission webhook "deny.example.com" denied the request',
             involved_kind="ReplicaSet"),
        "WebhookAdmissionRejected",
    ),
    ("terminating stuck", _obs("pod_status", "s", "app-13", status="Terminating"), "TerminatingStuck"),
]

# Healthy / benign signals that must never fire any detector.
NEGATIVES = [
    _obs("pod_status", "s", "ok-1", status="Running"),
    _obs("pod_status", "s", "ok-2", status="Completed"),
    _obs("pod_status", "s", "ok-3", status="Succeeded"),
    _obs("pod_status", "s", "ok-4", status="Pending"),
    _obs("event", "s", "ok-5", reason="Scheduled", event_type="Normal",
         message="Successfully assigned s/ok-5 to node", involved_kind="Pod"),
    _obs("event", "s", "ok-6", reason="Pulled", event_type="Normal",
         message="Container image already present on machine", involved_kind="Pod"),
    _obs("event", "s", "ok-7", reason="Started", event_type="Normal",
         message="Started container web", involved_kind="Pod"),
    # Warning events that don't correspond to any playbook:
    _obs("event", "s", "ok-8", reason="FailedToRetrieveImagePullSecret", event_type="Warning",
         message="Unable to retrieve some image pull secrets", involved_kind="Pod"),
    _obs("event", "s", "ok-9", reason="DNSConfigForming", event_type="Warning",
         message="Search Line limits were exceeded", involved_kind="Pod"),
]


def _replay(observations) -> dict[tuple[str, str], set[str]]:
    """Feed each observation into a fresh engine with debounce forced to 0;
    return {(ns, object) -> set of fired playbooks}."""
    from dataclasses import replace

    detectors = tuple(replace(d, debounce_seconds=0) for d in load_detectors())
    fired: dict[tuple[str, str], set[str]] = {}
    for obs in observations:
        engine = DetectorEngine(detectors=detectors, cluster_id="l2")
        for finding in engine.process(obs):
            fired.setdefault((obs.namespace, obs.name), set()).add(finding.playbook)
    return fired


@pytest.fixture(autouse=True)
def _mute_recorder(mocker):
    mocker.patch("app.detectors.engine.flight_recorder.record")


class TestLofaL2:
    def test_recall_at_least_80_percent(self):
        fired = _replay(obs for (_, obs, _) in POSITIVES)
        hits = sum(
            1
            for (_, obs, expected) in POSITIVES
            if expected in fired.get((obs.namespace, obs.name), set())
        )
        recall = hits / len(POSITIVES)
        missed = [
            (label, expected)
            for (label, obs, expected) in POSITIVES
            if expected not in fired.get((obs.namespace, obs.name), set())
        ]
        assert recall >= 0.80, f"recall {recall:.0%} < 80% — missed: {missed}"

    def test_false_positive_rate_under_5_percent(self):
        fired = _replay(NEGATIVES)
        false_firings = sum(len(v) for v in fired.values())
        fp_rate = false_firings / len(NEGATIVES)
        assert fp_rate <= 0.05, (
            f"FP rate {fp_rate:.0%} > 5% — false firings: "
            f"{ {k: sorted(v) for k, v in fired.items()} }"
        )

    def test_report_metrics(self, capsys):
        """Not an assertion — prints the L2 metrics for the eval record."""
        pos_fired = _replay(obs for (_, obs, _) in POSITIVES)
        neg_fired = _replay(NEGATIVES)
        hits = sum(
            1
            for (_, obs, expected) in POSITIVES
            if expected in pos_fired.get((obs.namespace, obs.name), set())
        )
        extra = sum(
            1
            for (_, obs, expected) in POSITIVES
            for pb in pos_fired.get((obs.namespace, obs.name), set())
            if pb != expected
        )
        print(
            f"\nLOFA-L2 offline: recall={hits}/{len(POSITIVES)}"
            f" cross-fires={extra} negatives-fired={sum(len(v) for v in neg_fired.values())}"
            f"/{len(NEGATIVES)}"
        )
