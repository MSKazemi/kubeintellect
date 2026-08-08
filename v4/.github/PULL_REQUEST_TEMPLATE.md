# Description

<!-- What does this PR do and why? -->

## Type of change

- [ ] Bug fix
- [ ] New static tool
- [ ] New agent
- [ ] Feature / enhancement
- [ ] Documentation
- [ ] Infrastructure / deployment

---

## Checklist

### All PRs
- [ ] `uv run pytest tests/` passes locally
- [ ] No new bare `Exception` swallowed silently — tools return `"Error: <message>"`
- [ ] No secrets or cluster-specific values hardcoded
- [ ] `uv run ruff check app/ tests/` passes

### New or modified tools
- [ ] Single purpose — one tool, one action
- [ ] Input is a typed Pydantic model with `Field(description=...)` on every param
- [ ] Output is a typed Pydantic model (not raw string or dict)
- [ ] No overlap with existing tools
- [ ] Added to the correct category in `app/agents/tools/kubernetes_tools.py`
- [ ] Added to the relevant agent's tool list in `app/orchestration/workflow.py`
- [ ] At least one happy-path test and one error-path test

### Write operations (scale, delete, patch, apply, exec)
- [ ] Dry-run diff shown to user before execution
- [ ] HITL gate in place (`interrupt_before` checkpoint or conversational confirm)
- [ ] Secret values are never logged or injected into prompts

### New agents
- [ ] Max iteration limit set (`max_iterations` or recursion guard)
- [ ] Unit test covering the happy path
- [ ] Supervisor system prompt updated with agent name and routing condition
- [ ] `CLAUDE.md` architecture section updated
