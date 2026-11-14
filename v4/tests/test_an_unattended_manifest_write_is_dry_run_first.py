"""An unattended manifest write is checked with `kubectl diff` first: a no-op is refused, a rejection is explained.

Measured 2026-11-14: unattended sessions sent writes that could not change anything (a `backoffLimit` raise on a Job
that had already failed; 73 identical rejected applies), and the cluster's reply to a malformed manifest named no
field. Probed on Kind v1.37: plain `kubectl diff -f -` exits 0 when the cluster already matches the manifest (also
for an object made by `create`) and 1 when it would change it; `kubectl diff --server-side` cannot tell a no-op
(field-ownership differences make it exit 1) but its error names the field
(`.spec.containers[name="c"].command[1]: expected string`, exit 2). Anything else that fails (no `diff` binary, an
unreachable server) fails open: the write proceeds and the API server still validates it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from app.core.config import settings
from app.tools import kubectl_tool as kt

CM = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app-config\n  namespace: b3-c1\ndata:\n  k: v\n"
SSA_ERROR = (
    "Error from server: failed to create typed patch object (b3-c1/app-config; /v1, Kind=ConfigMap): "
    ".data.k: expected string, got &value.valueUnstructured{}"
)


def _send(
    plain_rc: int,
    *,
    ssa: tuple[int, str] = (1, ""),
    bypass: bool = True,
    plain_err: str = "",
):
    calls: list[list[str]] = []
    prompts: list[dict] = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        if "diff" in argv and "--server-side" in argv:
            return SimpleNamespace(returncode=ssa[0], stdout="", stderr=ssa[1])
        if "diff" in argv:
            return SimpleNamespace(returncode=plain_rc, stdout="", stderr=plain_err)
        return SimpleNamespace(
            returncode=0, stdout="configmap/app-config configured\n", stderr=""
        )

    cfg = {
        "configurable": {
            "user_role": "operator",
            "hitl_bypass": bypass,
            "thread_id": "auto-pf",
            "bound_namespace": "b3-c1",
        }
    }
    with (
        mock.patch.object(settings, "PREFLIGHT_DIFF_ENABLED", True),
        mock.patch.object(kt.subprocess, "run", fake_run),
        mock.patch.object(kt, "_capture_rollback_point", lambda *a, **k: None),
        mock.patch.object(kt, "interrupt", lambda p: prompts.append(p) or True),
    ):
        out = kt.run_kubectl.invoke(
            {"command": "kubectl apply -f - -n b3-c1", "stdin": CM}, config=cfg
        )
    applied = [c for c in calls if "apply" in c]
    return applied, calls, out


def test_a_write_that_changes_nothing_is_refused_and_not_sent():
    applied, _, out = _send(plain_rc=0)
    assert not applied
    assert out.startswith("[No change]") and "cause is somewhere else" in out


def test_a_write_that_changes_something_is_sent():
    applied, _, out = _send(plain_rc=1)
    assert applied and "configured" in out


def test_a_server_rejection_is_explained_with_the_field_and_not_sent():
    applied, _, out = _send(plain_rc=2, ssa=(2, SSA_ERROR))
    assert not applied
    assert out.startswith(
        "[Invalid: rejected by the API server's dry run, not applied]"
    )
    assert ".data.k: expected string" in out and "[permanent error:" in out


@pytest.mark.parametrize(
    "plain_err, ssa",
    [
        (
            'error: failed to run "diff": executable file not found in $PATH',
            (2, "error: executable file not found"),
        ),
        (
            "Unable to connect to the server: dial tcp: i/o timeout",
            (2, "Unable to connect to the server"),
        ),
    ],
)
def test_anything_else_fails_open(plain_err, ssa):
    applied, _, _ = _send(plain_rc=2, plain_err=plain_err, ssa=ssa)
    assert applied, (
        "a diff that cannot run must not block the write; the API server still validates it"
    )


def test_a_resent_no_op_is_refused_by_the_repeat_guard():
    _send(plain_rc=0)
    _, calls, out = _send(plain_rc=0)
    assert out.startswith("[Repeat refused]") and not [c for c in calls if "diff" in c]


def test_a_chat_session_is_not_dry_run():
    applied, calls, _ = _send(plain_rc=0, bypass=False)
    assert applied and not [c for c in calls if "diff" in c]


# ── patch ─────────────────────────────────────────────────────────────────────
# Probed on Kind v1.37 (2026-11-14): `kubectl patch … --dry-run=server` prints "<obj> patched (no change)" when the
# patch changes nothing (merge and JSON patches alike), "<obj> patched" when it would change the object, and the
# server's rejection otherwise. Note what this cannot catch: a patch that changes a field to no useful effect (raising
# `backoffLimit` on a Job that already failed, seen in the re-runs) is a real change; outcome verification
# (`watchtower._record_fix_outcome`) is what reports it as not fixed.

PATCH = (
    'kubectl patch configmap app-config -n b3-c1 --type=merge -p \'{"data":{"k":"v"}}\''
)


def _patch(dry_rc: int, dry_out: str, dry_err: str = "", *, bypass: bool = True):
    calls: list[list[str]] = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        if "--dry-run=server" in argv:
            return SimpleNamespace(returncode=dry_rc, stdout=dry_out, stderr=dry_err)
        return SimpleNamespace(
            returncode=0, stdout="configmap/app-config patched\n", stderr=""
        )

    cfg = {
        "configurable": {
            "user_role": "operator",
            "hitl_bypass": bypass,
            "thread_id": "auto-pp",
            "bound_namespace": "b3-c1",
        }
    }
    with (
        mock.patch.object(settings, "PREFLIGHT_DIFF_ENABLED", True),
        mock.patch.object(kt.subprocess, "run", fake_run),
        mock.patch.object(kt, "_capture_rollback_point", lambda *a, **k: None),
        mock.patch.object(kt, "interrupt", lambda p: True),
    ):
        out = kt.run_kubectl.invoke({"command": PATCH}, config=cfg)
    sent = [c for c in calls if "patch" in c and "--dry-run=server" not in c]
    return sent, out


def test_a_patch_that_changes_nothing_is_refused():
    sent, out = _patch(0, "configmap/app-config patched (no change) (server dry run)\n")
    assert not sent and out.startswith("[No change]")


def test_a_patch_that_changes_something_is_sent():
    sent, out = _patch(0, "configmap/app-config patched (server dry run)\n")
    assert sent and "patched" in out


def test_a_rejected_patch_is_explained_and_not_sent():
    err = 'The request is invalid: patch: Invalid value: "{...}": json: cannot unmarshal object into Go struct field ConfigMap.data of type string'
    sent, out = _patch(1, "", err)
    assert not sent and out.startswith(
        "[Invalid: rejected by the API server's dry run, not applied]"
    )


def test_a_patch_dry_run_that_cannot_run_fails_open():
    sent, _ = _patch(1, "", "Unable to connect to the server: dial tcp: i/o timeout")
    assert sent


def test_a_chat_patch_is_not_dry_run():
    sent, _ = _patch(0, "configmap/app-config patched (no change)\n", bypass=False)
    assert sent
