"""A rollback point for an ordinary LIVE object must be restorable.

`tests/test_rollback_point_is_restorable.py` guards the dangerous direction: a capture that redaction or
truncation turned into something that is not the object must never claim to be restorable. Its fixtures,
however, are the output of `kubectl create ... --dry-run`, which has no server-managed fields. A real
`kubectl get -o yaml` of any live object carries `metadata.uid`, `resourceVersion`, `creationTimestamp`,
`managedFields` and (for a pod) `ownerReferences[].uid`, and the secret redactor treats a UUID as a token.
Measured 2026-11-12 on a stock Deployment in a real cluster: `metadata.uid` became `<redacted-token>`, the
capture was flagged `restorable: false`, and so, since every object has a uid, essentially every real
rollback point was.

These fixtures are the shape a real cluster returns. The redaction itself is NOT relaxed: the last tests
assert that a value that really is token-shaped still costs the capture its flag.
"""
from __future__ import annotations

import pytest
import yaml

from app.tools import kubectl_tool

LIVE_DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  annotations:
    deployment.kubernetes.io/revision: "1"
  creationTimestamp: "2026-11-12T12:49:20Z"
  generation: 1
  labels:
    app: victim
  managedFields:
  - apiVersion: apps/v1
    fieldsType: FieldsV1
    fieldsV1:
      f:spec:
        f:replicas: {}
    manager: kubectl-client-side-apply
    operation: Update
    time: "2026-11-12T12:49:20Z"
  name: victim
  namespace: pilot
  resourceVersion: "1842"
  uid: 9166a7a1-a5b0-4b61-8901-d51fb8e7c92a
spec:
  replicas: 3
  selector:
    matchLabels:
      app: victim
  template:
    metadata:
      creationTimestamp: null
      labels:
        app: victim
    spec:
      containers:
      - image: registry.k8s.io/pause:3.10
        imagePullPolicy: IfNotPresent
        name: pause
status:
  availableReplicas: 3
  observedGeneration: 1
  replicas: 3
"""

LIVE_POD = """apiVersion: v1
kind: Pod
metadata:
  creationTimestamp: "2026-11-12T12:49:21Z"
  labels:
    app: victim
  name: victim-664cb4ccc8-jlkcb
  namespace: pilot
  ownerReferences:
  - apiVersion: apps/v1
    blockOwnerDeletion: true
    controller: true
    kind: ReplicaSet
    name: victim-664cb4ccc8
    uid: 3b1f2c9e-6a41-4d05-9a57-0c8e2f4d7a11
  resourceVersion: "1850"
  uid: 5d2e8a47-1c3b-4f6a-8e90-2b7d4c1a9f03
spec:
  containers:
  - image: registry.k8s.io/pause:3.10
    name: pause
status:
  containerStatuses:
  - containerID: containerd://0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9
    image: registry.k8s.io/pause:3.10
    imageID: registry.k8s.io/pause@sha256:ee6521f290b2168b6e0935a181d4cff9be1ac3f505298cb5ec6a2a8ee5e0e5e0
    name: pause
    ready: true
  phase: Running
"""

LIVE_LIST = """apiVersion: v1
items:
- apiVersion: apps/v1
  kind: Deployment
  metadata:
    name: a
    namespace: pilot
    uid: 11111111-2222-3333-4444-555555555555
    resourceVersion: "10"
  spec:
    replicas: 1
- apiVersion: apps/v1
  kind: Deployment
  metadata:
    name: b
    namespace: pilot
    uid: 66666666-7777-8888-9999-000000000000
    resourceVersion: "11"
  spec:
    replicas: 2
kind: List
metadata:
  resourceVersion: ""
"""

# A ConfigMap whose VALUES are token-shaped, on a live object with a uid. Stripping the uid must not hide this.
LIVE_TOKEN_CONFIGMAP = (
    "apiVersion: v1\ndata:\n"
    + "".join(f"  key{i}: u8jzPde0IgxLd6GncfBAepfJBd0Kh8oOOL8dKLzd\n" for i in range(5))
    + "kind: ConfigMap\nmetadata:\n  name: app-config\n  namespace: pilot\n"
    "  resourceVersion: \"77\"\n  uid: 0e9d8c7b-6a5f-4e3d-2c1b-0a9f8e7d6c5b\n"
)


class _Recorded:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, episode_id, kind, payload):
        self.calls.append((episode_id, kind, payload))

    @property
    def payload(self) -> dict:
        assert self.calls, "nothing was recorded"
        return self.calls[-1][2]


@pytest.fixture
def capture(mocker):
    rec = _Recorded()
    from app.db import flight_recorder
    mocker.patch.object(flight_recorder, "record", rec)

    def _run(stdout: str, *, args=None):
        class _Proc:
            returncode = 0
        proc = _Proc()
        proc.stdout = stdout
        mocker.patch.object(kubectl_tool.subprocess, "run", return_value=proc)
        kubectl_tool._capture_rollback_point(
            "delete", args or ["kubectl", "delete", "deployment", "victim", "-n", "pilot"], None, None, {})
        return rec
    return _run


class TestALiveObjectIsARestorePoint:
    def test_a_live_deployment_is_restorable(self, capture):
        rec = capture(LIVE_DEPLOYMENT)
        assert rec.payload["restorable"] is True, rec.payload["capture_notes"]
        assert rec.payload["capture_notes"] == []

    def test_the_server_managed_fields_are_gone_and_the_desired_state_is_kept(self, capture):
        stored = capture(LIVE_DEPLOYMENT).payload["pre_state"][0]
        for owned_by_the_server in ("uid:", "resourceVersion", "managedFields", "creationTimestamp",
                                    "generation:", "observedGeneration", "<redacted-token>"):
            assert owned_by_the_server not in stored, owned_by_the_server
        doc = yaml.safe_load(stored)
        assert doc["kind"] == "Deployment"
        assert doc["spec"]["replicas"] == 3
        assert doc["metadata"]["name"] == "victim" and doc["metadata"]["namespace"] == "pilot"
        assert doc["metadata"]["labels"] == {"app": "victim"}

    def test_a_live_pod_with_an_owner_and_container_ids_is_restorable(self, capture):
        rec = capture(LIVE_POD, args=["kubectl", "delete", "pod", "victim-664cb4ccc8-jlkcb", "-n", "pilot"])
        assert rec.payload["restorable"] is True, rec.payload["capture_notes"]
        stored = rec.payload["pre_state"][0]
        assert "ownerReferences" not in stored and "containerID" not in stored
        assert yaml.safe_load(stored)["metadata"]["name"] == "victim-664cb4ccc8-jlkcb"

    def test_every_item_of_a_list_is_stripped(self):
        out = yaml.safe_load(kubectl_tool._strip_server_managed(LIVE_LIST))
        assert [i["metadata"]["name"] for i in out["items"]] == ["a", "b"]
        assert [i["spec"]["replicas"] for i in out["items"]] == [1, 2]
        for item in out["items"]:
            assert "uid" not in item["metadata"] and "resourceVersion" not in item["metadata"]


class TestRedactionIsNotRelaxed:
    def test_a_token_shaped_value_still_costs_the_flag_on_a_live_object(self, capture):
        rec = capture(LIVE_TOKEN_CONFIGMAP, args=["kubectl", "delete", "configmap", "app-config", "-n", "pilot"])
        assert rec.payload["restorable"] is False
        assert rec.payload["capture_notes"], "no reason was recorded"
        assert "u8jzPde0IgxLd6GncfBAepfJBd0Kh8oOOL8dKLzd" not in rec.payload["pre_state"][0]

    @pytest.mark.parametrize("text", ["a: [unterminated", "- just\n- a list\n", "plain scalar", ""])
    def test_text_that_is_not_plain_mappings_is_returned_unchanged(self, text):
        assert kubectl_tool._strip_server_managed(text) == text
