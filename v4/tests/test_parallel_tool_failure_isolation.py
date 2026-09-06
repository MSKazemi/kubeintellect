"""One malformed read must not discard successful calls from the same batch."""
from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, MessagesState, StateGraph

from app.agent.tool_execution import fault_isolated_tool_node


def _compiled_tool_node(tools):
    graph = StateGraph(MessagesState)
    graph.add_node("tools", fault_isolated_tool_node(tools))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return graph.compile()


@tool
async def run_kubectl(command: str) -> str:
    """Return deterministic output for a read-only kubectl command."""
    if "<" in command or ">" in command:
        raise ValueError("Command contains disallowed shell characters")
    return f"result for {command}"


@tool
async def mutate_cluster(command: str) -> str:
    """Model the interrupt raised by a mutation awaiting human approval."""
    raise GraphInterrupt()


async def test_one_invalid_call_does_not_abort_parallel_read_results():
    graph = _compiled_tool_node([run_kubectl])
    commands = [
        "kubectl get nodes -o wide",
        "kubectl describe pod payments -n shop",
        "kubectl get events -n shop",
        "kubectl logs checkout -n shop",
        "kubectl describe pod worker -n shop",
        "kubectl get pods -n shop",
        "kubectl describe node <node-name>",
    ]
    batch = AIMessage(
        content="",
        tool_calls=[
            {"name": "run_kubectl", "args": {"command": command}, "id": f"call-{index}"}
            for index, command in enumerate(commands)
        ],
    )

    result = await graph.ainvoke({"messages": [batch]})
    messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]

    assert len(messages) == 7
    assert sum(message.status == "success" for message in messages) == 6
    failures = [message for message in messages if message.status == "error"]
    assert len(failures) == 1
    assert isinstance(failures[0], ToolMessage)
    assert "kubectl describe node <node-name>" in failures[0].content
    assert "Command contains disallowed shell characters" in failures[0].content


async def test_hitl_graph_interrupt_is_not_converted_to_a_tool_error():
    graph = _compiled_tool_node([mutate_cluster])
    batch = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "mutate_cluster",
                "args": {"command": "kubectl delete pod checkout"},
                "id": "mutation-1",
            }
        ],
    )

    result = await graph.ainvoke({"messages": [batch]})

    assert not any(isinstance(message, ToolMessage) for message in result["messages"])


async def test_failed_command_and_reason_are_redacted_before_returning():
    @tool
    async def failing_tool(command: str) -> str:
        """Fail with the submitted command in the exception text."""
        raise ValueError(f"rejected {command}")

    graph = _compiled_tool_node([failing_tool])
    secret = "sk-test-abcdefghijklmnopqrstuvwxyz123456"
    batch = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "failing_tool",
                "args": {"command": f"kubectl get pods --token={secret}"},
                "id": "failure-1",
            }
        ],
    )

    result = await graph.ainvoke({"messages": [batch]})
    failures = [
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.status == "error"
    ]

    assert len(failures) == 1
    assert secret not in failures[0].content
    assert "<redacted>" in failures[0].content
