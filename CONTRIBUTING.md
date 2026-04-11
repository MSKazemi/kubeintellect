# Contributing to KubeIntellect

Thank you for your interest in contributing. This guide covers dev setup, how to add tools and agents, and the PR checklist.

---

## Dev Setup

**Requirements:** Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), Docker, [kind](https://kind.sigs.k8s.io/), kubectl, helm

```bash
git clone https://github.com/MSKazemi/kubeintellect.git
cd kubeintellect
cp .env.example .env          # fill in your LLM API key at minimum
uv sync                        # install dependencies
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

For a full local stack with a real Kubernetes cluster:

```bash
make kind-kubeintellect-clean-deploy   # ~5 min on first run
make port-forward-librechat            # open http://localhost:3080
```

---

## Running Tests

All tests live in `tests/` at the repo root. Most tests mock the Kubernetes client and run without a cluster. A small number of integration tests require a real cluster (see below).

```bash
# Run everything — safe without a cluster (K8s calls are mocked)
uv run pytest tests/

# Run a single test or file
uv run pytest tests/test_agent_routing_tool_assignment.py
uv run pytest tests/test_hitl_sse_schema.py::test_sse_schema_complete

# Verbose output
uv run pytest tests/ -v

# Stop on first failure
uv run pytest tests/ -x
```

**Which tests require a cluster?**

| Test file | Requires Kind cluster? | Why |
|---|---|---|
| `test_agent_routing_tool_assignment.py` | No | Mocked LLM + tools |
| `test_chat_validation.py` | No | HTTP schema validation |
| `test_hitl_sse_schema.py` | No | SSE event structure |
| `test_routing_corpus.py` | No | Routing logic, mocked agents |
| `test_tool_registry.py` | No | PostgreSQL via test DB |
| `test_runtime_safety_bugs.py` | No | AST sandbox, no K8s calls |
| `test_tools_typed_outputs.py` | No | Output schema validation |
| `tests/eval/corpus/fault_scenarios/` | **Yes** | Injects faults into a real cluster |

To run the eval fault scenarios (requires a running Kind cluster):

```bash
make kind-kubeintellect-clean-deploy   # cluster must be running
# Then follow tests/eval/README.md
```

Before submitting a PR, ensure `uv run pytest tests/` passes locally (no cluster required).

---

## How to Add a New Static Tool

Static tools live in `app/agents/tools/tools_lib/`. Each file follows this pattern:

```python
# 1. Input schema
class GetPodLogsInput(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace")
    pod_name: str = Field(..., description="Pod name")

# 2. Tool function — always returns str
def get_pod_logs(namespace: str, pod_name: str) -> str:
    try:
        # ... k8s API call ...
        return formatted_result
    except Exception as e:
        return f"Error: {e}"

# 3. Tool instance
get_pod_logs_tool = StructuredTool.from_function(
    func=get_pod_logs,
    name="get_pod_logs",
    description="Fetch logs from a specific pod.",
    args_schema=GetPodLogsInput,
)

# 4. Exported list at bottom of file
pod_tools = [get_pod_logs_tool, ...]
```

**Steps:**
1. Find or create `app/agents/tools/tools_lib/<resource>_tools.py`
2. Define `class <Action><Resource>Input(BaseModel)` with `Field(description=...)` on every param
3. Write the tool function returning `str`
4. Wrap with `StructuredTool.from_function()`
5. Add to the list at the bottom of the file
6. In `app/agents/tools/kubernetes_tools.py` — import the list and add to the relevant category
7. In `app/orchestration/workflow.py` — add the category to the relevant agent's tool list

**Naming conventions:**

| Thing | Pattern | Example |
|-------|---------|---------|
| Tool file | `<resource>_tools.py` | `pod_tools.py` |
| Input class | `<Action><Resource>Input` | `GetPodLogsInput` |
| Tool function | `<action>_<resource>` | `get_pod_logs` |
| Tool name (LangChain) | `snake_case verb_noun` | `get_pod_logs` |
| Tool list | `<resource>_tools` | `pod_tools` |

---

## How to Add a New Agent

1. In `app/orchestration/workflow.py`:
   - Define a tool list for the agent (from `kubernetes_tools.py` categories)
   - Add an agent node: `new_agent = create_react_agent(llm, tools=new_agent_tools)`
   - Add `workflow.add_node("new_agent", new_agent)`
   - Add routing edge from supervisor: `workflow.add_edge("new_agent", "supervisor")`
2. Update the supervisor's system prompt to include the new agent name and its scope
3. Add an entry to the Agent Catalog table in `README.md`

> **Note:** If the capability can be a new tool on an existing agent, do that instead of adding a new agent.

---

## PR Checklist

Before opening a pull request, verify:

- [ ] Tool has a single purpose; input is a typed Pydantic model; output is `str`
- [ ] No ambiguous overlap with existing tools
- [ ] All write operations have dry-run + diff + HITL gate
- [ ] Error handling returns `"Error: <message>"` — no silent swallowing
- [ ] Secret values are never logged or returned (key names only)
- [ ] At least one happy-path test and one error-path test
- [ ] `uv run pytest tests/` passes

---

## Code Style

- **Package manager:** `uv` only — no `pip install` directly
- **Secrets:** always read from `settings` (`from app.core.config import settings`) — never hardcode
- **Kubernetes API:** go through `app/services/kubernetes_service.py` — never call directly from endpoints
- **Exception handling:** return `"Error: <message>"` from tools — do not catch bare `Exception` silently

---

## Reporting Bugs

Open a GitHub issue with:
- KubeIntellect version / commit
- Steps to reproduce
- Expected vs actual behavior
- Relevant logs (`kubectl logs -n kubeintellect deployments/kubeintellect-core`)
