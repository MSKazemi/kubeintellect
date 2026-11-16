"""`set image` / `set resources` are always-confirm; the same change written as `patch` or `apply` was not.

`_ALWAYS_CONFIRM_SET_SUBCOMMANDS = {"image", "resources"}` exists because a live workload mutation "can take traffic
down without any rollback path - the user must confirm even on auto-approve sessions". The gate keyed on the
*spelling* `set image`, not on the *effect*. Measured 2026-11-12/13 through the real tool layer with a real model
(JGC round-1, R2.3; `papers/v4-operator-r1/data/a3_breadth_haiku45_run2.md`): one session fixed an ImagePullBackOff with
``kubectl patch deployment faulty-app --type=json -p '[{"op":"replace","path":"/spec/template/spec/containers/0/image",...}]'``
and it ran under auto-approve with no prompt, while ``kubectl set image`` for the SAME change on the SAME field stopped
and asked. Over 20 equal-N runs the repair rate of each model tracked which spelling it chose, not how well it diagnosed.

Rule (a decision, recorded so it is not mistaken for an oversight): a write that changes a container ``image`` or
``resources`` field of an existing object is always-confirm, whichever verb carries it:
* ``patch`` (JSON, merge or strategic): any key ``image`` / ``resources``, any JSON-patch op whose path ends in or runs
  through ``image`` / ``resources``. A patch whose payload cannot be read (``--patch-file``, unparseable) is confirmed:
  the gate fails closed.
* ``apply`` / ``replace`` of a stdin manifest that declares a pod-bearing kind (Deployment, StatefulSet, DaemonSet,
  ReplicaSet, ReplicationController, Pod, Job, CronJob) with container ``image`` / ``resources``. The tool cannot tell a
  new object from an update without asking the cluster, so it does not try: both are confirmed.
Everything else is unchanged: ``patch`` of replicas / annotations / a ConfigMap value with no ``image`` key, and
``apply`` of a ConfigMap, keep honouring auto-approve. Unattended A3 therefore can no longer repair an image-pull fault
(the request needs a human); that is the intended consequence of making the gate consistent, not a regression.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.tools.kubectl_tool import _extract_verb, _requires_always_confirm, run_kubectl

IMG_JSON_PATCH = json.dumps([{"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": "busybox:latest"}])
DEPLOY_YAML = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: faulty-app, namespace: b3-i1}\nspec:\n  replicas: 1\n"
    "  selector: {matchLabels: {app: faulty-app}}\n  template:\n    metadata: {labels: {app: faulty-app}}\n"
    "    spec:\n      containers:\n      - {name: app, image: 'busybox:latest'}\n"
)
CM_YAML = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: app-config, namespace: b3-c1}\ndata: {APP_ENV: production}\n"


def _gate(command: str, stdin: str | None = None) -> bool:
    import shlex

    args = shlex.split(command)
    return _requires_always_confirm(_extract_verb(args), args, stdin)


def _invoke(command: str, stdin: str | None = None, hitl_bypass: bool = True):
    captured = {}

    def fake_interrupt(value):
        captured.update(value)
        return True

    proc = MagicMock()
    proc.stdout, proc.stderr, proc.returncode = "ok", "", 0
    cfg = {"configurable": {"user_role": "admin", "hitl_bypass": hitl_bypass}}
    with patch("app.tools.kubectl_tool.interrupt", side_effect=fake_interrupt) as mock_intr:
        with patch("subprocess.run", return_value=proc):
            run_kubectl.invoke({"command": command, "stdin": stdin} if stdin else {"command": command}, config=cfg)
    return captured, mock_intr


# ── the incident itself ────────────────────────────────────────────────────────────────────────────
def test_the_json_patch_that_ran_unprompted_on_2026_11_12_now_stops_and_asks():
    cmd = f"kubectl patch deployment faulty-app -n b3-i1 --type=json -p '{IMG_JSON_PATCH}'"
    captured, intr = _invoke(cmd)
    intr.assert_called_once()
    assert captured["always_confirm"] is True and captured["risk_level"] == "high"


def test_set_image_and_the_patch_for_the_same_change_are_gated_alike():
    assert _gate("kubectl set image deployment/faulty-app app=busybox:latest -n b3-i1") is True
    assert _gate(f"kubectl patch deployment faulty-app -n b3-i1 --type=json -p '{IMG_JSON_PATCH}'") is True


# ── every spelling of a patch that touches image / resources ──────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "kubectl patch deployment faulty-app -n x -p '{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"app\",\"image\":\"busybox:latest\"}]}}}}'",
    "kubectl patch deployment faulty-app -n x --type=merge -p '{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"app\",\"image\":\"b:1\"}]}}}}'",
    "kubectl -n x patch deployment faulty-app --patch '{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"app\",\"image\":\"b:1\"}]}}}}'",
    "kubectl patch deployment faulty-app -n x --patch={\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"app\",\"image\":\"b:1\"}]}}}}",
    "kubectl patch deployment faulty-app -n x -p '{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"app\",\"resources\":{\"limits\":{\"memory\":\"1Gi\"}}}]}}}}'",
    "kubectl patch deployment faulty-app -n x --type=json -p '[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/resources/limits/memory\",\"value\":\"1Gi\"}]'",
    "kubectl patch deployment faulty-app -n x --type=json -p '[{\"op\":\"add\",\"path\":\"/spec/template/spec/initContainers/0/image\",\"value\":\"b:1\"}]'",
])
def test_a_patch_that_changes_image_or_resources_is_always_confirm(cmd):
    assert _gate(cmd) is True


def test_a_patch_read_from_a_file_cannot_be_inspected_so_it_is_confirmed():
    assert _gate("kubectl patch deployment faulty-app -n x --patch-file=/tmp/p.yaml") is True


def test_a_patch_whose_payload_cannot_be_parsed_is_confirmed():
    assert _gate("kubectl patch deployment faulty-app -n x -p '{not json'") is True


def test_a_patch_with_no_payload_flag_is_confirmed():
    assert _gate("kubectl patch deployment faulty-app -n x") is True


# ── what must NOT change: narrower writes keep honouring auto-approve ─────────────────────────────
@pytest.mark.parametrize("cmd", [
    "kubectl patch deployment faulty-app -n x -p '{\"spec\":{\"replicas\":3}}'",
    "kubectl patch deployment faulty-app -n x --type=merge -p '{\"metadata\":{\"annotations\":{\"a\":\"b\"}}}'",
    "kubectl patch deployment faulty-app -n x --type=json -p '[{\"op\":\"replace\",\"path\":\"/spec/replicas\",\"value\":2}]'",
    "kubectl patch job report -n h3-2 --type=merge -p '{\"spec\":{\"backoffLimit\":3}}'",
    "kubectl scale deployment faulty-app -n x --replicas=0",
    "kubectl rollout restart deployment/faulty-app -n x",
    "kubectl create configmap app-config -n x --from-literal=APP_ENV=production",
])
def test_writes_that_do_not_touch_image_or_resources_still_honour_auto_approve(cmd):
    assert _gate(cmd) is False


def test_a_key_that_merely_contains_the_word_image_is_not_the_image_field():
    # `imagePullPolicy` / `imagePullSecrets` are different fields; matching by substring would over-gate them.
    assert _gate("kubectl patch deployment faulty-app -n x -p '{\"spec\":{\"template\":{\"spec\":{\"imagePullSecrets\":[{\"name\":\"s\"}]}}}}'") is False
    assert _gate("kubectl patch deployment faulty-app -n x --type=json -p '[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/imagePullPolicy\",\"value\":\"Always\"}]'") is False


def test_a_read_that_mentions_image_is_not_a_write():
    assert _gate("kubectl get deployment faulty-app -n x -o jsonpath={.spec.template.spec.containers[0].image}") is False


# ── apply / replace of a manifest ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("verb_cmd", ["kubectl apply -f - -n b3-i1", "kubectl replace -f - -n b3-i1", "kubectl apply -f -"])
def test_applying_a_workload_manifest_with_a_container_image_is_always_confirm(verb_cmd):
    assert _gate(verb_cmd, DEPLOY_YAML) is True


def test_applying_a_configmap_manifest_still_honours_auto_approve():
    assert _gate("kubectl apply -f - -n b3-c1", CM_YAML) is False


def test_a_list_manifest_is_inspected_item_by_item():
    lst = f"apiVersion: v1\nkind: List\nitems:\n- {json.dumps(yaml_to_obj(CM_YAML))}\n- {json.dumps(yaml_to_obj(DEPLOY_YAML))}\n"
    assert _gate("kubectl apply -f -", lst) is True


def test_a_multi_document_manifest_is_confirmed_if_any_document_is_a_workload():
    assert _gate("kubectl apply -f -", CM_YAML + "---\n" + DEPLOY_YAML) is True


def test_apply_end_to_end_stops_and_asks_under_auto_approve():
    captured, intr = _invoke("kubectl apply -f - -n b3-i1", DEPLOY_YAML)
    intr.assert_called_once()
    assert captured["always_confirm"] is True


def test_configmap_apply_end_to_end_does_not_ask_under_auto_approve():
    _, intr = _invoke("kubectl apply -f - -n b3-c1", CM_YAML)
    intr.assert_not_called()


def test_without_auto_approve_nothing_changes_the_prompt_was_already_there():
    cmd = f"kubectl patch deployment faulty-app -n b3-i1 --type=json -p '{IMG_JSON_PATCH}'"
    _, intr = _invoke(cmd, hitl_bypass=False)
    intr.assert_called_once()


def test_callers_that_pass_no_stdin_keep_the_two_argument_signature():
    assert _requires_always_confirm("set", ["kubectl", "set", "image", "deploy/x", "a=b"]) is True
    assert _requires_always_confirm("get", ["kubectl", "get", "pods"]) is False


def test_a_json_escaped_field_name_is_still_the_image_field():
    # "ima\u0067e" decodes to `image`; a text match alone would miss it, so the gate also reads the parsed payload.
    cmd = "kubectl patch deployment faulty-app -n x -p '{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"app\",\"ima\\u0067e\":\"b:1\"}]}}}}'"
    assert _gate(cmd) is True


def yaml_to_obj(text: str):
    import yaml

    return yaml.safe_load(text)
