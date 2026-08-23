"""A manifest KubeIntellect cannot read cannot be gated, and cannot be shown to the approver.

Pass 55 taught the protected-access checks to read the manifest on stdin. `kubectl apply` has
other ways to name one, and they put it somewhere the process cannot reach at all:

    kubectl apply -f /tmp/payload.yaml            a path inside the pod
    kubectl apply -f https://example.com/m.yaml   fetched by kubectl, from inside the cluster
    kubectl apply -k https://github.com/x/repo    a kustomize overlay, same

Measured 2026-08-20 for `operator` and `admin`, all of these ran. Three separate properties fail
at once, which is why this is worse than the stdin gap pass 55 closed:

1. the protected-resource and protected-namespace checks see a command that names neither, so
   applying a Secret or writing into `kube-system` is invisible to both;
2. the approval prompt carries `stdin: null` and a `human_summary` that is just the command line,
   so the human is asked to approve a payload nobody has seen — and for a URL the content does
   not even exist yet at approval time, since kubectl fetches it afterwards;
3. the URL form is unreviewed outbound network access from the KubeIntellect pod.

`-f -` with stdin is the supported form: it is validated, read by both checks, and rendered in
the approval prompt. It is also what the product already tells users to use — the `kubectl edit`
rejection message says so.

The over-block that would matter here is `kubectl logs -f`, where `-f` is `--follow`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.tools.kubectl_tool import _external_manifest_source, run_kubectl

_CONFIGMAP = "kind: ConfigMap\nmetadata:\n  name: c\n  namespace: prod\ndata:\n  a: b\n"


def _invoke(command: str, stdin: str | None, role: str = "admin") -> tuple[str, bool, bool]:
    with patch("subprocess.run") as run:
        proc = MagicMock()
        proc.stdout, proc.stderr, proc.returncode = "<DONE>", "", 0
        run.return_value = proc
        try:
            out, hitl = str(run_kubectl.invoke({"command": command, "stdin": stdin},
                            config={"configurable": {"user_role": role, "thread_id": "t",
                                                     "hitl_bypass": True}})), False
        except KeyError as exc:
            if "__pregel_scratchpad" not in str(exc):
                raise
            out, hitl = "", True
        return out, run.called, hitl


_EXTERNAL = [
    "kubectl apply -f /tmp/payload.yaml",
    "kubectl apply -f=/tmp/payload.yaml",
    "kubectl apply -f/tmp/payload.yaml",
    "kubectl apply --filename /tmp/payload.yaml",
    "kubectl apply --filename=https://example.com/m.yaml",
    "kubectl apply -f https://example.com/m.yaml",
    "kubectl apply -f ./manifests/",
    "kubectl create -f https://example.com/m.yaml",
    "kubectl replace -f /etc/kubeintellect/x.yaml",
    "kubectl apply -k https://github.com/someone/repo",
    "kubectl apply -k=./overlays/prod",
    "kubectl apply -k./overlays/prod",
    "kubectl apply --kustomize ./overlays/prod",
    "kubectl -n prod apply -f /tmp/x.yaml",          # flag-order, from pass 51
]


class TestAnUnreadableManifestSourceIsRefused:
    @pytest.mark.parametrize("command", _EXTERNAL)
    @pytest.mark.parametrize("role", ["operator", "admin", "superadmin"])
    def test_it_never_reaches_kubectl(self, command, role):
        out, ran, hitl = _invoke(command, None, role)
        # The role gate runs first and is the more useful answer when it applies: an operator
        # cannot `replace` at all, so it is told that rather than told about manifest sources.
        role_gated = role == "operator" and command.split()[1] in ("replace", "delete", "drain")
        expected = "[Permission Denied]" if role_gated else "[Unsupported]"
        assert expected in out, f"{command!r} as {role}: {out[:120]!r} hitl={hitl}"
        assert not ran and not hitl

    def test_the_message_names_the_supported_form(self):
        out, _ran, _hitl = _invoke("kubectl apply -f https://example.com/m.yaml", None)
        assert "-f -" in out and "stdin" in out

    def test_a_readonly_key_still_sees_the_permission_error_first(self):
        """Role is the more informative answer for a key that could never do this anyway."""
        out, ran, _hitl = _invoke("kubectl apply -f /tmp/x.yaml", None, "readonly")
        assert "[Permission Denied]" in out
        assert not ran


class TestTheSupportedFormStillWorks:
    @pytest.mark.parametrize("command", [
        "kubectl apply -f -",
        "kubectl apply -f=-",
        "kubectl apply -f-",
        "kubectl create -f - --dry-run=server",
        "kubectl -n prod apply -f -",
    ])
    def test_stdin_is_not_an_external_source(self, command):
        out, ran, _hitl = _invoke(command, _CONFIGMAP)
        assert ran, f"{command!r} was refused: {out[:120]!r}"


class TestFollowIsNotAFilename:
    @pytest.mark.parametrize("command", [
        "kubectl logs -f api-1 -n prod",
        "kubectl logs --follow api-1 -n prod",
        "kubectl logs -f api-1 -c sidecar -n prod",
        "kubectl -n prod logs -f api-1",
    ])
    def test_kubectl_logs_f_still_streams(self, command):
        out, ran, _hitl = _invoke(command, None, "readonly")
        assert ran, f"-f was misread as a filename: {out[:120]!r}"


class TestOrdinaryCommandsAreUnaffected:
    @pytest.mark.parametrize("command", [
        "kubectl get pods -n prod",
        "kubectl get pods -o=json -n prod",
        "kubectl get pods -o custom-columns=NAME:.metadata.name",
        "kubectl describe deployment api -n prod",
        "kubectl top pods --sort-by=cpu -n prod",
        "kubectl get pods -l app=api --field-selector=status.phase=Running",
    ])
    def test_it_runs(self, command):
        out, ran, _hitl = _invoke(command, None, "readonly")
        assert ran, f"over-blocked: {out[:120]!r}"


class TestTheParserItself:
    @pytest.mark.parametrize("args,expected", [
        (["kubectl", "apply", "-f", "-"], None),
        (["kubectl", "apply", "-f", "x.yaml"], "x.yaml"),
        (["kubectl", "apply", "-f=x.yaml"], "x.yaml"),
        (["kubectl", "apply", "-fx.yaml"], "x.yaml"),
        (["kubectl", "apply", "--filename", "x.yaml"], "x.yaml"),
        (["kubectl", "apply", "--filename=x.yaml"], "x.yaml"),
        (["kubectl", "apply", "-k", "./o"], "./o"),
        (["kubectl", "get", "pods", "-o", "json"], None),
        (["kubectl", "get", "pods", "--field-selector=a=b"], None),
    ])
    def test_values_are_read_in_every_spelling(self, args, expected):
        assert _external_manifest_source(args[1], args) == expected

    def test_a_trailing_bare_f_fails_closed(self):
        """A malformed command is refused rather than passed through unexamined."""
        assert _external_manifest_source("apply", ["kubectl", "apply", "-f"]) == ""

    def test_follow_is_only_exempt_for_logs(self):
        assert _external_manifest_source("logs", ["kubectl", "logs", "-f", "api-1"]) is None
        assert _external_manifest_source("apply", ["kubectl", "apply", "-f", "api-1"]) == "api-1"
