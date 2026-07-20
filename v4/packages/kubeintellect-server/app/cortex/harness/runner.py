"""ADR-101 read-only investigation fan-out runner (v5 P2 body; specs/00 §R-p0-06/07).

The gather node dispatches ``run_fanout`` instead of the flat ``gather_once`` round when
``CORTEX_V5_ENABLED and KI_V5_HARNESS_FANOUT`` is set. This module now carries the P2 *body*
(not just the P0 seam): it decomposes the investigation into up to ``KI_V5_HARNESS_MAX_SUBAGENTS``
isolated read-only investigators, runs them in parallel, and reconciles their bounded summaries
into a single evidence bundle the lead agent synthesizes from. "Parallel diagnosis, serialized
mutation" — investigators may call ONLY the four ACI read verbs (structural, via bind_tools), so
the fan-out cannot mutate the cluster.

Stability contract (this is the "most stable" bar the whole feature is default-off behind):
- Each subagent is fully isolated (fresh context: objective + optional snapshot, never the lead's
  message history) and bounded (``KI_V5_HARNESS_MAX_SUBAGENT_ROUNDS`` ACI rounds, ≤2k-token summary).
- A subagent that raises is caught and degraded to an error-note result — one investigator can
  never crash the turn (CLAUDE.md: perception failures must never break a user response).
- If EVERY subagent comes back empty/failed, ``run_fanout`` falls back to the flat ``gather_once``
  round, so enabling the flag can never leave an investigation with no evidence.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from app.cortex.harness.subagent import SubagentContract, SubagentResult, finalize_result
from app.core.config import settings
from app.tools.aci import ACI_READ_VERBS
from app.utils.logger import get_logger

if TYPE_CHECKING:  # CortexState lives in graph.py; import only for typing (avoids a cycle).
    from app.cortex.graph import CortexState

logger = get_logger(__name__)

_SUBAGENT_SYSTEM = (
    "You are an isolated read-only investigation subagent of KubeIntellect. Your single objective:\n"
    "  {objective}\n\n"
    "You may call ONLY these read-only verbs: {verbs}. You cannot mutate the cluster. Gather just "
    "enough evidence to address the objective, then STOP and reply with a concise finding "
    "(root-cause hypothesis + the specific evidence lines that support it). Do not pad.\n\n"
    "CRITICAL — never conclude a resource is missing from a `search` alone: `search` matches by "
    "LABEL/selector, so an empty result usually means no label match, NOT that the object is absent. "
    "Before ever stating a named object 'does not exist', confirm with `inspect` using its exact "
    "kind and name (e.g. inspect the pod by name). Only an explicit by-name lookup that returns "
    "NotFound proves absence. If a cluster snapshot is provided below, consult it FIRST — any "
    "resource listed there exists, so investigate its state (logs, events, status), not its "
    "existence. If the evidence is genuinely inconclusive, say so plainly."
)

# A callable that runs one subagent; injectable so the fan-out is unit-testable without an LLM.
SubagentRunner = Callable[[SubagentContract, RunnableConfig], Awaitable[SubagentResult]]


def plan_subagents(state: CortexState, max_subagents: int) -> list[SubagentContract]:
    """Decompose the current investigation into read-only subagent contracts.

    One contract per actionable plan step (pending/in-progress), capped at ``max_subagents``;
    falls back to a single objective built from the last user turn when there is no plan. Each
    contract is validated against the frozen ADR-101 read-only allowlist on construction.
    """
    if max_subagents < 1:
        return []
    plan = state.get("investigation_plan") or []
    steps = [s.description for s in plan if getattr(s, "status", "") in ("pending", "in_progress")]
    if not steps:
        steps = [_lead_objective(state)]
    # SubagentContract.__post_init__ enforces the read-only allowlist + no-mutation invariant.
    return [SubagentContract(objective=s) for s in steps[:max_subagents]]


def _lead_objective(state: CortexState) -> str:
    """A single objective distilled from the latest user message (isolation-preserving)."""
    for msg in reversed(state.get("messages") or []):
        text = getattr(msg, "content", "")
        if isinstance(msg, HumanMessage) and isinstance(text, str) and text.strip():
            return text.strip()[:300]
    return "investigate the reported issue"


async def run_subagent(
    contract: SubagentContract,
    config: RunnableConfig,
    *,
    snapshot: str = "",
    llm: Optional[BaseChatModel] = None,
    max_rounds: Optional[int] = None,
) -> SubagentResult:
    """Run one isolated read-only investigator to a bounded, structured summary.

    Robust by contract: any exception degrades to an error-note result rather than propagating,
    so a single failed investigator never breaks the fan-out.
    """
    rounds = max_rounds if max_rounds is not None else settings.KI_V5_HARNESS_MAX_SUBAGENT_ROUNDS
    by_name = {t.name: t for t in ACI_READ_VERBS}
    verbs_used: list[str] = []
    try:
        if llm is None:
            if settings.KI_V5_HARNESS_SUBAGENT_LARGE_MODEL:
                from app.cortex.models import get_synthesis_llm
                llm = get_synthesis_llm()   # larger tier: small models mis-investigate
            else:
                from app.cortex.models import get_specialist_llm
                llm = get_specialist_llm()
        bound = llm.bind_tools(ACI_READ_VERBS)

        system = _SUBAGENT_SYSTEM.format(
            objective=contract.objective, verbs=", ".join(sorted(contract.allowed_verbs)),
        )
        messages: list = [SystemMessage(content=system)]
        if snapshot:
            messages.append(SystemMessage(content=f"## Cluster snapshot\n{snapshot}"))
        messages.append(HumanMessage(content=contract.objective))

        summary = ""
        for _ in range(max(1, rounds)):
            reply = await bound.ainvoke(messages, config)
            messages.append(reply)
            tool_calls = getattr(reply, "tool_calls", None) or []
            if not tool_calls:
                summary = reply.content if isinstance(reply.content, str) else str(reply.content)
                break
            for tc in tool_calls:
                name = tc.get("name", "")
                tool = by_name.get(name)
                if tool is None:  # outside the allowlist — structurally shouldn't happen (bind).
                    content = f"[blocked] {name!r} is not a read-only verb"
                else:
                    result = await tool.ainvoke(tc.get("args") or {}, config)
                    content = result.content if hasattr(result, "content") else str(result)
                    verbs_used.append(name)
                messages.append(ToolMessage(tool_call_id=tc.get("id", ""), name=name, content=str(content)))
        else:
            # Rounds exhausted while still calling tools — summarize from the last reply.
            last = messages[-1]
            summary = last.content if isinstance(getattr(last, "content", ""), str) else ""

        return finalize_result(contract, summary, verbs_used)
    except Exception as exc:  # never let one investigator break the turn.
        logger.warning("harness subagent failed for %r: %s", contract.objective, exc)
        return finalize_result(contract, f"[investigation error: {exc}]", verbs_used)


def reconcile_results(results: list[SubagentResult]) -> str:
    """Deterministically fold subagent findings into one evidence bundle for the lead agent."""
    blocks = []
    for i, r in enumerate(results, 1):
        verbs = ", ".join(r.verbs_used) if r.verbs_used else "none"
        trunc = " (truncated)" if r.truncated else ""
        blocks.append(
            f"### Finding {i}: {r.objective}\n"
            f"_read verbs used: {verbs}{trunc}_\n\n{r.summary.strip()}"
        )
    return "\n\n".join(blocks)


async def run_fanout(
    state: CortexState,
    config: RunnableConfig,
    *,
    runner: Optional[SubagentRunner] = None,
) -> dict:
    """Dispatch the parallel read-only investigation fan-out for one gather round.

    Returns a single evidence-bundle AIMessage (no tool_calls ⇒ the graph routes straight to
    synthesize, which concludes from the fan-out evidence). ``runner`` is injectable for tests.
    """
    from app.cortex.graph import gather_once  # lazy: avoids a graph<->runner import cycle.

    contracts = plan_subagents(state, settings.KI_V5_HARNESS_MAX_SUBAGENTS)
    if not contracts:
        return await gather_once(state, config)

    snapshot = state.get("cluster_snapshot") or ""
    if runner is None:
        async def runner(c: SubagentContract, cfg: RunnableConfig) -> SubagentResult:  # noqa: E731
            return await run_subagent(c, cfg, snapshot=snapshot)

    results = await asyncio.gather(*(runner(c, config) for c in contracts))

    # All investigators empty/failed ⇒ fall back to the flat gather round (never no evidence).
    if all(not r.summary.strip() or r.summary.strip().startswith("[investigation error")
           for r in results):
        logger.info("harness fan-out produced no usable evidence; falling back to flat gather")
        return await gather_once(state, config)

    evidence = reconcile_results(list(results))
    msg = AIMessage(content="Read-only investigation fan-out complete. Evidence:\n\n" + evidence)
    return {"messages": [msg], "gather_rounds": state.get("gather_rounds", 0) + 1}
