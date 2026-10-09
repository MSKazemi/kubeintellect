"""An unresolved placeholder in a kubectl argument is refused before anything runs (#173).

A real `kq` session batched `kubectl get nodes -o wide` with `kubectl describe node <node-name>`
in one parallel response — the second call needed the first one's answer and used planning
notation in its place. `<node-name>` was refused, but as "disallowed shell characters", which
told the model nothing about the mistake; `{namespace}` and `POD_NAME` were refused by nothing
at all and went to kubectl, which answers with a NotFound the model reads as evidence.

The guard narrows what executes and must never refuse ordinary kubectl: jsonpath, go-template
and custom-columns values carry `{name}`-like text legitimately, and so do quoted JSON patches,
annotation values and a container's own command line after `--`. Both halves are pinned here.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402

from app.tools.kubectl_tool import (  # noqa: E402
    _unresolved_placeholder,
    _unresolved_placeholder_in_args,
    run_kubectl,
)


def _proc(stdout: str = "NAME      STATUS\nworker-1  Ready\n") -> MagicMock:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = ""
    proc.returncode = 0
    return proc


# (command, the placeholder the refusal must name)
PLACEHOLDERS = [
    ("kubectl describe node <node-name>", "<node-name>"),
    ("kubectl describe pod/<pod> -n shop", "<pod>"),
    ("kubectl get pods -n {namespace}", "{namespace}"),
    ("kubectl -n shop describe pod {pod}", "{pod}"),
    ("kubectl describe pod {{pod}} -n shop", "{{pod}}"),
    ("kubectl get pods -n shop -l app={app}", "{app}"),
    ("kubectl logs POD_NAME -n shop", "POD_NAME"),
    ("kubectl describe deployment/DEPLOYMENT_NAME -n shop", "DEPLOYMENT_NAME"),
    ("kubectl logs web-1 -n YOUR_NAMESPACE", "YOUR_NAMESPACE"),
    ("kubectl logs web-1 --namespace=MY_NS", "MY_NS"),
    ("kubectl logs web-1 -c CONTAINER_NAME -n shop", "CONTAINER_NAME"),
    ("kubectl get pods -A --field-selector spec.nodeName=$NODE", "$NODE"),
    ("kubectl get pods -A --field-selector=spec.nodeName=${NODE}", "${NODE}"),
]

# Ordinary kubectl that carries a placeholder-like shape somewhere the guard must not look.
NOT_PLACEHOLDERS = [
    # jsonpath — quoted, unquoted, unquoted-with-spaces (followed until its braces balance),
    # attached `-ojsonpath=`, `--output=`, and a quoted `{"\n"}` separator inside it
    "kubectl get pods -n prod -o jsonpath='{.items[*].metadata.name}'",
    r"""kubectl get pods -n prod -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'""",
    r'kubectl get pods -n prod -o jsonpath={range .items[*]}{.metadata.name}{"\n"}{end}',
    "kubectl get pods -n prod -o=jsonpath={range .items[*]}{.metadata.name}{end}",
    "kubectl get pods -n prod -ojsonpath='{.items[*].metadata.name}'",
    "kubectl get pods -n prod --output=jsonpath='{range .items[*]}{.status.phase}{end}'",
    # go-template — inline, via --template quoted, via --template unquoted
    "kubectl get pods -n prod -o go-template='{{range .items}}{{.metadata.name}}{{end}}'",
    "kubectl get pods -n prod -o go-template --template='{{range .items}}{{.metadata.name}}{{end}}'",
    "kubectl get pods -n prod -o go-template --template {{range .items}}{{end}}",
    # custom-columns headers are UPPER_SNAKE by convention
    "kubectl get pods -n prod -o=custom-columns=POD_NAME:.metadata.name,NODE_NAME:.spec.nodeName",
    "kubectl get pods -n prod --sort-by={.metadata.creationTimestamp}",
    # quoted values: a JSON patch, an annotation value
    """kubectl patch deploy web -n shop -p '{"spec":{"replicas":3}}'""",
    """kubectl annotate deploy web -n shop note='see {ticket} for POD_NAME'""",
    # UPPER_SNAKE that is not an object name: env vars, a removed label key, `config` contexts
    "kubectl set env deploy/web -n shop LOG_LEVEL=debug",
    "kubectl set env deploy/web -n shop --keys=DB_HOST --from=configmap/cfg",
    "kubectl label pod web-1 -n shop TEAM_ID-",
    "kubectl config use-context PROD_CTX",
    # the container's own command line after `--`
    "kubectl exec web-1 -n shop -- printenv POD_NAME",
    # plain reads with mixed-case selector values
    "kubectl get events -n shop --field-selector=type=Warning,reason=FailedScheduling",
    "kubectl get pods -n shop -l 'app in (web,api)'",
    "kubectl get nodes -o wide",
]


class TestTheDetector:
    @pytest.mark.parametrize("command,placeholder", PLACEHOLDERS)
    def test_names_the_placeholder(self, command, placeholder):
        assert _unresolved_placeholder(command) == placeholder

    @pytest.mark.parametrize("command", NOT_PLACEHOLDERS)
    def test_leaves_ordinary_kubectl_alone(self, command):
        assert _unresolved_placeholder(command) is None

    def test_an_open_quote_is_left_to_the_parser(self):
        # The shlex step after this reports a truncated jsonpath with its own message.
        assert _unresolved_placeholder("kubectl get pods -n shop -o jsonpath='{.items") is None

    def test_split_argv_form(self):
        # The snapshot executor's argv has no `kubectl` and no quoting left.
        assert _unresolved_placeholder_in_args(["describe", "pod", "<pod>", "-n", "shop"]) == "<pod>"
        assert _unresolved_placeholder_in_args(["describe", "pod", "web-1", "-n", "{ns}"]) == "{ns}"
        assert _unresolved_placeholder_in_args(["get", "pods", "--all-namespaces"]) is None


class TestRunKubectl:
    @pytest.mark.parametrize("command,placeholder", PLACEHOLDERS)
    def test_refuses_without_running_kubectl(self, command, placeholder):
        with patch("subprocess.run", return_value=_proc()) as mock_run:
            with pytest.raises(ValueError, match="unresolved placeholder") as exc:
                run_kubectl.invoke({"command": command})
        mock_run.assert_not_called()
        message = str(exc.value)
        assert repr(placeholder) in message
        # The refusal has to say what to do instead, or the model retries the same call.
        assert "kubectl get nodes -o wide" in message
        assert "concrete name" in message

    @pytest.mark.parametrize("command", [
        # Reads only: a write would stop at the HITL gate, which is not the subject here.
        c for c in NOT_PLACEHOLDERS if c.split()[1] == "get"
    ])
    def test_ordinary_reads_still_run(self, command):
        with patch("subprocess.run", return_value=_proc("web-1\n")) as mock_run:
            result = run_kubectl.invoke({"command": command})
        mock_run.assert_called_once()
        assert "web-1" in result

    def test_angle_brackets_are_still_refused_by_the_metachar_gate(self):
        # Acceptance criterion: `<` and `>` stay rejected. A redirection is not a placeholder,
        # so the guard passes it through and the shell-metacharacter gate still catches it.
        with patch("subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="disallowed shell characters"):
                run_kubectl.invoke({"command": "kubectl get pods > /tmp/out"})
        mock_run.assert_not_called()


def _issue_173_batch() -> list[dict]:
    return [
        {"name": "run_kubectl", "args": {"command": "kubectl get nodes -o wide"}, "id": "c-1"},
        {"name": "run_kubectl", "args": {"command": "kubectl describe node <node-name>"}, "id": "c-2"},
    ]


class TestTheIssueBatch:
    """The trace from #173, end to end through each graph's tool executor."""

    async def test_default_graph_tool_node(self):
        from app.agent.tool_execution import fault_isolated_tool_node

        graph = StateGraph(MessagesState)
        graph.add_node("tools", fault_isolated_tool_node([run_kubectl]))
        graph.add_edge(START, "tools")
        graph.add_edge("tools", END)
        compiled = graph.compile()

        with patch("subprocess.run", return_value=_proc()) as mock_run:
            result = await compiled.ainvoke(
                {"messages": [AIMessage(content="", tool_calls=_issue_173_batch())]}
            )

        by_id = {m.tool_call_id: m for m in result["messages"] if isinstance(m, ToolMessage)}
        assert by_id["c-1"].status == "success"
        assert "worker-1" in by_id["c-1"].content
        assert by_id["c-2"].status == "error"
        assert "unresolved placeholder '<node-name>'" in by_id["c-2"].content
        # Only the discovery call reached kubectl.
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0][:3] == ["kubectl", "get", "nodes"]

    async def test_cortex_gather_tools(self, mocker):
        import app.cortex.graph as G
        from app.agent.state import PlanStep

        # Same tool object, so the guard covers the Cortex graph without a second copy of it.
        assert G._TOOLS_BY_NAME["run_kubectl"] is run_kubectl

        async def _noemit(*a, **k):
            return None

        mocker.patch.object(G, "emit", _noemit)

        class Last:
            tool_calls = _issue_173_batch()

        with patch("subprocess.run", return_value=_proc()) as mock_run:
            out = await G.gather_tools(
                {
                    "investigation_plan": [PlanStep(description="Inspect nodes", status="in_progress")],
                    "plan_cursor": 0,
                    "session_id": "s",
                    "messages": [Last()],
                },
                {},
            )

        by_id = {m.tool_call_id: m for m in out["messages"]}
        assert "worker-1" in by_id["c-1"].content
        assert "unresolved placeholder '<node-name>'" in by_id["c-2"].content
        mock_run.assert_called_once()


class TestTheSnapshotExecutor:
    """`targeted_investigator` builds an argv from a `TARGETED:` line the model wrote."""

    @pytest.mark.parametrize("args", [
        ["describe", "pod", "<pod>", "-n", "shop"],
        ["describe", "pod", "payments-api-1", "-n", "{namespace}"],
        ["describe", "pod", "POD_NAME", "-n", "shop"],
    ])
    def test_refuses_without_running_kubectl(self, args):
        from app.agent.nodes.context_fetcher import _kubectl_snapshot

        with patch("subprocess.run", return_value=_proc()) as mock_run:
            ok, text, _complete = _kubectl_snapshot(args)
        mock_run.assert_not_called()
        assert ok is False
        assert "unresolved placeholder" in text

    def test_a_concrete_read_still_runs(self):
        from app.agent.nodes.context_fetcher import _kubectl_snapshot

        with patch("subprocess.run", return_value=_proc()) as mock_run:
            ok, text, _complete = _kubectl_snapshot(["describe", "pod", "payments-api-1", "-n", "shop"])
        mock_run.assert_called_once()
        assert ok is True
        assert "worker-1" in text
