"""Coordinator node — decides whether to answer directly or fan-out to RCA subagents."""
from __future__ import annotations

import re
import time
from typing import Optional

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt, GraphRecursionError

from app.agent.state import AgentState, PlanStep
from app.core.config import settings
from app.core.llm import get_coordinator_llm
from app.streaming.emitter import PlanEvent, StatusEvent, emit
from app.answer_contract import PREMISE_CLAUSE
from app.tools.output_policy import POLICY_LINE_RE
from app.tools.registry import ALL_TOOLS
from app.utils.logger import get_logger

logger = get_logger(__name__)

_TARGETED_RE = re.compile(
    r"TARGETED:\s*namespace\s*=\s*(\S+?),\s*pod\s*=\s*(\S+?),\s*issue\s*=\s*(.+)",
    re.IGNORECASE,
)

# Investigation plan parser.
# Matches a leading "INVESTIGATION_PLAN:" block followed by one or more
# "- <step>" lines. The block is stripped from the message body before storage.
_PLAN_BLOCK_RE = re.compile(
    r"^\s*INVESTIGATION_PLAN:\s*\n[ \t]*\n?((?:[ \t]*(?:[-•*]|\d+\.?)\s+.+\n?)+)",
    re.MULTILINE,
)
_PLAN_STEP_RE = re.compile(r"^[ \t]*(?:[-•*]|\d+\.?)\s+(.+)$", re.MULTILINE)
_PLAN_MIN_STEPS = 3

# Keep the last N messages from session history to prevent context bloat.
# Each exchange ≈ 4 messages (HumanMessage + AIMessage(tool_call) + ToolMessage + AIMessage).
# 20 messages ≈ 5 prior exchanges — enough context while capping prompt growth.
_MAX_SESSION_MESSAGES = 20


def _compress_dropped_messages(dropped: list[BaseMessage]) -> str:
    """Build a compact deterministic summary of dropped messages.

    Extracts: user topics, kubectl/query commands run, and key tool results.
    No LLM call — synchronous and zero-latency.
    """
    lines: list[str] = ["## Earlier Session Context (compressed)"]
    for msg in dropped:
        msg_type = getattr(msg, "type", None)
        if msg_type == "human" and isinstance(msg.content, str):
            topic = msg.content.strip().replace("\n", " ")[:120]
            lines.append(f"- User: {topic}")
        elif msg_type == "ai":
            # Extract tool calls (kubectl commands, queries)
            tool_calls = getattr(msg, "tool_calls", None) or []
            for tc in tool_calls:
                args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                cmd = args.get("command") or args.get("query") or args.get("logql") or ""
                if cmd:
                    lines.append(f"- Ran: {str(cmd)[:100]}")
            # Plain AI text (answers, decisions)
            if not tool_calls and isinstance(msg.content, str):
                snippet = msg.content.strip().replace("\n", " ")[:120]
                if snippet:
                    lines.append(f"- Assistant: {snippet}")
        elif msg_type == "tool" and isinstance(msg.content, str):
            # Keep only first line of tool output as a hint
            first_line = msg.content.strip().splitlines()[0][:100] if msg.content.strip() else ""
            if first_line:
                lines.append(f"  → {first_line}")
    return "\n".join(lines)


def _trim_session_messages(messages: list[BaseMessage]) -> tuple[list[BaseMessage], str | None]:
    """Return (recent_messages, compressed_summary_of_dropped).

    Caps history at _MAX_SESSION_MESSAGES, preserving exchange integrity by
    advancing to the first HumanMessage in the window (a naive tail-slice can
    start with a ToolMessage whose parent AIMessage(tool_calls) was cut off,
    causing Azure to reject with 400).

    Returns a non-None summary string when messages were dropped, so callers
    can inject it into the system prompt to preserve earlier context.
    """
    if len(messages) <= _MAX_SESSION_MESSAGES:
        return messages, None

    original_len = len(messages)
    keep = messages[-_MAX_SESSION_MESSAGES:]

    # Advance past any leading non-human messages (ToolMessage / AIMessage orphans).
    first_human = next(
        (i for i, m in enumerate(keep) if hasattr(m, "type") and m.type == "human"),
        0,
    )
    keep = keep[first_human:]
    dropped = messages[: original_len - len(keep)]

    summary = _compress_dropped_messages(dropped) if dropped else None

    logger.debug(
        f"coordinator: compressed session history {original_len} → {len(keep)} messages "
        f"({len(dropped)} dropped, summary={'yes' if summary else 'no'})"
    )
    return keep, summary


# ── Tool output trimmer (A4 — ISS-01) ────────────────────────────────────────

_TOOL_OUTPUT_MAX_CHARS = 2_000
_KUBECTL_TABLE_ROWS = 30
_LOG_LINES_KEPT = 60
_KUBECTL_KEEP_RE = re.compile(
    r"error|warning|failed|pending|oomkilled|crashloop|backoff|imagepull|containercreating",
    re.IGNORECASE,
)

# A line the *tool* added to say its own output is already incomplete — the withheld-namespace
# sentence, a `[Protected]` refusal, a truncation marker it wrote itself. These are lifted out
# before the row and line caps run and re-attached afterwards, because they sit at the END of a
# listing, which is precisely where those caps cut. Losing one turns "3 namespaces were withheld"
# back into a listing that reads as complete, undoing the guarantee `run_kubectl` exists to make.
# Two markers, both reachable and neither a superset of the other: every `namespace_guard`
# notice opens with `[Protected]`, and a tool that truncated its own output writes
# `[truncated: N chars omitted …]` (see `kubectl_tool._cap_output`) with no `[Protected]` in it.
# Shared with `cortex.graph._bound_tool_content`, which bounds the same tool results on the
# other route — two copies of this predicate would drift, and silently.
_POLICY_LINE_RE = POLICY_LINE_RE


def _dropped_note(dropped: int, noun: str) -> str:
    """Say that rows were dropped *here*, in the vocabulary the prompt block below names.

    `_SYSTEM_PROMPT` instructs the model to raise a visible warning when tool output "contains a
    truncation marker (text like `[truncated` or `chars omitted`)". The marker this function used
    to emit said "chars trimmed", matching neither — an instruction and its trigger, four hundred
    lines apart in one file, that did not agree on a string.
    """
    if dropped <= 0:
        return ""
    return (
        f"\n[truncated: {dropped} {noun}(s) omitted from LLM context — this listing is NOT the "
        f"complete set; narrow the query (-n, -l, --tail) to see the rest]"
    )


def _trim_tool_output(content: str) -> str:
    """Shrink tool output to fit the model's context — and say what was taken out.

    Until 2026-08-24 it said so only when the *remainder* still exceeded the char cap, which is
    the uncommon case. Measured on a 200-pod `kubectl get pods`: the model received a 30-row
    table, **170 rows gone, no marker of any kind** — and since `_KUBECTL_KEEP_RE` retains the
    unhealthy rows, the ones dropped are the healthy ones, so "how many pods are Running?"
    answered 30. A trimmed listing that reads as complete is the same defect the withheld-note
    vocabulary was built to close, one layer further in: `run_kubectl` announced the short
    listing correctly and this function deleted the announcement on the way to the model.
    """
    if len(content) <= _TOOL_OUTPUT_MAX_CHARS:
        return content

    lines = content.splitlines(keepends=True)
    policy = "".join(ln for ln in lines if _POLICY_LINE_RE.search(ln))
    body = [ln for ln in lines if not _POLICY_LINE_RE.search(ln)]

    if body and "NAME" in body[0].upper():
        # kubectl table: header + first N rows + any important rows
        header = body[0]
        kept: list[str] = []
        row_count = dropped = 0
        for line in body[1:]:
            if not line.strip():
                continue  # a separator, not a row — counting it would report 121 rows of 120
            if _KUBECTL_KEEP_RE.search(line):
                kept.append(line)
            elif row_count < _KUBECTL_TABLE_ROWS:
                kept.append(line)
                row_count += 1
            else:
                dropped += 1
        trimmed = header + "".join(kept)
        note = _dropped_note(dropped, "row")
    else:
        # logs / describe / prometheus / loki: keep the first N lines
        trimmed = "".join(body[:_LOG_LINES_KEPT])
        note = _dropped_note(max(0, len(body) - _LOG_LINES_KEPT), "line")

    if len(trimmed) > _TOOL_OUTPUT_MAX_CHARS:
        omitted = len(trimmed) - _TOOL_OUTPUT_MAX_CHARS
        trimmed = trimmed[:_TOOL_OUTPUT_MAX_CHARS]
        # Two different losses, so two counts rather than one merged number: the caller can tell
        # "I only see 30 of 200 rows" from "the last row I see is cut in half".
        note += f"\n[truncated: {omitted} chars omitted from LLM context]"

    # The tool's own notice goes last, exactly where it sat before the trim.
    return trimmed + note + ("\n" + policy.strip() if policy.strip() else "")


def _trim_tool_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Cap ToolMessage content before it enters the LLM context (ISS-01)."""
    result: list[BaseMessage] = []
    for msg in messages:
        if isinstance(msg, ToolMessage) and isinstance(msg.content, str):
            trimmed = _trim_tool_output(msg.content)
            if trimmed != msg.content:
                msg = ToolMessage(
                    content=trimmed,
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                )
        result.append(msg)
    return result


def _fill_orphan_tool_calls(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Inject placeholder ToolMessages for tool_calls that never executed.

    When a HITL interrupt fires mid-batch, only the resumed tool call gets a
    ToolMessage; the other tool_calls in the same AIMessage are orphaned. The
    LLM then re-proposes them on the next loop, causing redundant HITL prompts.
    Filling the orphans with a "skipped" placeholder breaks that loop.
    """
    existing_ids = {
        m.tool_call_id for m in messages
        if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None)
    }
    extras: list[ToolMessage] = []
    for msg in messages:
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or []:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tc_id and tc_id not in existing_ids:
                    extras.append(ToolMessage(
                        tool_call_id=tc_id,
                        content="Skipped — pending approval; will be re-proposed in a separate response.",
                    ))
                    existing_ids.add(tc_id)
    return list(messages) + extras


# ── Investigation plan extraction ────────────────────────────────────────────


def _annotate_plan_steps(plan: list[PlanStep], messages: list[BaseMessage]) -> None:
    """Mark plan steps done/skipped based on how many tool calls actually executed.

    Counts individual tool-call invocations (each item in AIMessage.tool_calls)
    from the completed agent run, then transitions the first N steps to "done"
    and any remainder to "skipped". Mutates plan in-place.
    """
    tool_call_count = sum(
        len(getattr(m, "tool_calls", None) or [])
        for m in messages
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None)
    )
    for i, step in enumerate(plan):
        step.status = "done" if i < tool_call_count else "skipped"


def _extract_plan(messages: list[BaseMessage]) -> tuple[list[PlanStep], list[BaseMessage]]:
    """Strip an INVESTIGATION_PLAN block from the first AIMessage; return steps + cleaned messages.

    Returns ([], messages) when no plan block is found or the block has fewer
    than _PLAN_MIN_STEPS steps. Steps with only whitespace are skipped.
    """
    if not messages:
        return [], messages
    cleaned: list[BaseMessage] = []
    plan: list[PlanStep] = []
    consumed = False
    for msg in messages:
        if (
            not consumed
            and isinstance(msg, AIMessage)
            and isinstance(msg.content, str)
        ):
            match = _PLAN_BLOCK_RE.search(msg.content)
            if match:
                steps_text = match.group(1)
                step_lines = [
                    s.strip()
                    for s in _PLAN_STEP_RE.findall(steps_text)
                    if s.strip()
                ]
                if len(step_lines) >= _PLAN_MIN_STEPS:
                    plan = [PlanStep(description=s) for s in step_lines]
                    new_content = (msg.content[:match.start()] + msg.content[match.end():]).strip()
                    msg = AIMessage(
                        content=new_content,
                        tool_calls=getattr(msg, "tool_calls", []) or [],
                    )
                    consumed = True
        cleaned.append(msg)
    return plan, cleaned


# ── Coordinator system prompt ─────────────────────────────────────────────────

# Raw string on purpose. The prompt quotes jsonpath separators verbatim — `{"\n"}`,
# `{"\t"}` and a lone backslash. Interpreted, those became a real newline and tab, which
# split the example kubectl commands mid-line before the model ever saw them, and the
# lone `\` raised a SyntaxWarning that Python will eventually make a SyntaxError.
_COORDINATOR_SYSTEM = r"""You are KubeIntellect, an expert Kubernetes operations AI.

You have access to four tools:
- run_kubectl: run any kubectl command against the cluster
- run_helm: inspect Helm release state (list, get values/manifest/notes, status, history) — use this whenever diagnosing workloads deployed via Helm
- query_prometheus: query Prometheus metrics (PromQL)
- query_loki: query Loki for application logs (LogQL)

## Cluster Snapshot
A real-time snapshot is pre-loaded in your context (see "## Cluster Snapshot" section).
ALWAYS consult this before making tool calls.
- If the answer is in the snapshot (e.g. pod state, warning events), answer without extra tool calls.
- If a Warning Event shows the exact error message, use it directly.
- Only call tools to DRILL DOWN into specific resources found in the snapshot.

## Parallel Tool Execution
Emit ALL independent tool calls in a SINGLE response. The runtime executes them concurrently.
Use sequential calls ONLY when the second call depends on the first result.

Parallel (always):   (get pods) + (get events) + (describe node)
Parallel (always):   (loki error query) + (prometheus CPU query)
Sequential (only):   (get pod name) → (describe that pod) → (patch that pod)

## Investigation Discipline
For any query that requires tool calls, follow these phases strictly:
  1. PLAN  — decide every tool call needed to answer the question completely.
  2. FETCH — emit ALL independent tool calls in ONE response (parallel).
  3. SYNTHESIZE — after all tool results return, produce ONE final answer.

Never respond with a partial answer and then call more tools to refine it.
Exception (sequential dependency): the second call genuinely depends on the
first result — e.g. "find the failing pod's name → describe THAT pod". Even
then, gather everything you can in parallel at each step.

CRITICAL — never ask any permission or guidance question mid-investigation.
Banned phrases (and all equivalents):
  "Would you like me to proceed?", "Shall I continue?", "Would you like me
  to apply this?", "Would you like me to guide you on…?", "Should I try
  another approach?", "Do you want me to…?".
These are wasted round trips. Always proceed autonomously:
  - When a tool returns no data → state what you found (or didn't find) and
    continue with the next logical step or provide the best available answer.
  - When a namespace is empty or metrics unavailable → say so clearly and
    provide whatever partial information IS available (e.g. list current
    deployments, note that no metrics exist). Do NOT ask how to proceed.
Only stop for the user when a HITL gate fires (write operations) or when
you have genuinely exhausted all investigative paths.

## Fix Verification (REQUIRED after every mutation)
After kubectl patch / apply / create / delete, you MUST verify the outcome:
1. Run kubectl get on the affected resource (e.g. kubectl get pods -n <ns>)
2. If the fix was for a connectivity issue, ALSO run kubectl get endpoints -n <ns>
   to confirm traffic can actually reach the pods — a Running pod with a
   mismatched service selector still has endpoints=<none> and is unreachable.
3. Report ACTUAL state: "Pod is now Running (verified)" or "Fix applied — pod still in <state>"
Never declare a service "operational" without confirming endpoints are populated.

## Mutation Batching (HITL Safety)
When proposing kubectl mutations (patch / apply / create / delete / scale / set / rollout),
emit at most ONE mutation per response. Wait for the result before the next.
Reads (get / describe / logs / top) may still be batched in parallel — only mutations
are restricted. Each mutation triggers an approval gate; batching multiple in one
response causes redundant approval prompts and can re-queue the unapproved calls.

## Service-Endpoint Cross-Check (MANDATORY for namespace-level queries)
On EVERY namespace-level investigation ("check ns X", "what's wrong in X",
"solve issues in X", "diagnose X"), include these calls in your INITIAL
parallel tool batch — alongside `get pods` and `get events`:

  - `kubectl get endpoints -n <ns>`
  - `kubectl get services -n <ns>`

Then flag any service whose ENDPOINTS column is `<none>` while its target pods
are Running. This is a silent fault — no warning event fires for a selector/label
drift, so this cross-check is the ONLY way to surface it. Do NOT skip this even
when the obvious failing pods are already explained.

When endpoints=<none>, ALWAYS diagnose the cause explicitly:
  - If pods are failing: endpoints are none because pods aren't ready (expected).
  - If pods are Running but endpoints are still <none>: the service selector does
    not match the pod labels. Run:
      kubectl get svc <name> -n <ns> -o jsonpath='{.spec.selector}'
      kubectl get pods -n <ns> --show-labels
    and compare. A label mismatch must be called out as a separate root cause.

## Tool-Selection by Intent (CRITICAL — kubectl is authoritative for cluster state)
Pick the right tool based on what the user is asking for. Prometheus and Loki
are for *history and aggregations*; kubectl is the source of truth for the
*current declared and observed state* of the cluster.

  "Events", "warnings", "warning events", "what events occurred":
    - ALWAYS use: kubectl get events --field-selector type=Warning -A
      (or `-n <ns>` when the question scopes to one namespace).
    - DO NOT use query_prometheus for events. Prometheus does not store
      Kubernetes events; you will get metric data that does not answer the
      question.

  "Resource limits / requests", "pods without limits", "memory limit",
  "CPU request", or any question about a pod's resource specification:
    - ALWAYS read the pod spec via kubectl:
        kubectl get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\t"}{.spec.containers[*].resources.limits}{"\n"}{end}'
      or, for a single pod:
        kubectl get pod <name> -n <ns> -o jsonpath='{.spec.containers[*].resources}'
    - Prometheus exposes USAGE (`container_memory_working_set_bytes`,
      `container_cpu_usage_seconds_total`) — never the spec. Use Prometheus
      only to compare actual usage against the spec values you read with
      kubectl.

  "Endpoints", "service has no endpoints", "is the service reachable":
    - ALWAYS use: kubectl get endpoints -n <ns> + kubectl get services -n <ns>.
    - DO NOT infer reachability from Prometheus scrape success — a missing
      endpoint shows up as `<none>` in `kubectl get endpoints` and is the
      authoritative signal.

## Prometheus Empty-Result Handling
When `query_prometheus` returns no data (empty result set, "no data" message,
or metric not found):
  - Do NOT conclude "Metrics Server is not available" or "metrics API unavailable" —
    those are kubectl-top / metrics-server concepts; Prometheus is a separate system.
  - Do NOT ask the user how to proceed.
  - State clearly: "No metrics found for <metric> in <namespace/cluster>. This may
    mean the workload has not generated data, the query window predates pod creation,
    or the metric series does not exist."
  - If the task requires metrics that don't exist, provide the best available
    alternative (e.g. kubectl top if available, or list current deployments with
    their resource requests as a proxy).
  - NEVER infer "near-zero usage" from an empty result — absence of data is not
    the same as zero usage.

## Quantile Coverage for Latency / Duration Queries
When the user asks about "latency", "duration", "response time", or names
multiple quantiles (e.g. "p50/p95/p99"), emit ALL requested quantiles in one
parallel batch. Do not answer with a single quantile when more were asked for.

  Example — user: "Show p50/p95/p99 request latency for the api service":
    Emit three parallel query_prometheus calls:
      histogram_quantile(0.50, sum(rate(http_request_duration_seconds_bucket{service="api"}[5m])) by (le))
      histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{service="api"}[5m])) by (le))
      histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{service="api"}[5m])) by (le))

When the user says "latency" with no quantile, default to p50 + p95 + p99 in
parallel — a single quantile is rarely a complete answer.

## Spec-Before-Logs for CrashLoopBackOff
When diagnosing a CrashLoop pod, ALWAYS read `spec.containers[].command` and
`spec.containers[].args` from `kubectl describe pod` BEFORE inferring a root
cause from log output. A log line like "DB not configured" might be hardcoded
in the command itself; in that case no env or secret patch will fix it — the
spec's `command` field must be edited.

Cross-check: if the command contains a hardcoded `exit 1` or unconditional
error message, patching env vars will NOT fix it. The command itself is the bug.

## Node Drain / Maintenance Plans
When producing a node drain plan, ALWAYS include ALL three phases:

  1. **Cordon** — prevents new pods from being scheduled:
       kubectl cordon <node>
  2. **Drain** — evicts existing pods with PDB awareness:
       kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
     Add --grace-period=<N> if pods need a clean shutdown window.
     ALWAYS mention PodDisruptionBudgets: if a PDB's minAvailable would be
     violated, drain will wait; use --disable-eviction only in emergencies
     with explicit warnings about PDB bypass.
  3. **Uncordon** — re-enables scheduling after maintenance:
       kubectl uncordon <node>
     NEVER omit the uncordon step. A drained node stays permanently
     unschedulable until uncordoned.

When showing steps as a list, number them and call out the uncordon step explicitly
so the operator knows the node must be uncordoned after maintenance is complete.

## Shell-Metacharacter Constraints (applies to ALL kubectl commands)
For safety, the runner rejects any kubectl command containing shell
metacharacters: `&&`, `||`, `|`, `;`, `>`, `<`. This applies to the FULL
command string, including arguments inside `--patch '[...]'`, `-p '[...]'`,
or `-- sh -c "..."`.

Backslashes (`\`) ARE allowed — they are required for jsonpath separators
like `{"\n"}` and `{"\t"}`.

Output format selection (CRITICAL — pick the simplest format that works):
  - 1 field from many objects → `-o name` or `-o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'`
  - 2–4 fields in a table    → `-o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,IMAGE:'.spec.containers[0].image'`
                                 (NO quotes around the whole expression; column paths use dot-notation)
  - Complex/nested structure → `-o json` then reference specific fields in your analysis
  - NEVER write a jsonpath expression longer than 120 characters — use custom-columns or -o json instead.
    Long jsonpath expressions are fragile and error-prone. Prefer simple formats.

  - `kubectl exec ... -- sh -c "a && b"`     ❌ blocked — split into two execs.
  - `kubectl patch ... --patch '[{...; ...}]'` ❌ blocked — even if the `;` is
    inside a JSON string. Use `kubectl apply -f -` with stdin instead.
  - Multi-line commands or chained shell expressions ❌ — never work.

For setting a container `command` or `args` (which usually contain `;` or `&&`),
the ONLY reliable path is:
  1. `kubectl get <kind> <name> -n <ns> -o yaml` to fetch the current spec.
  2. Build the corrected manifest in your response.
  3. `kubectl apply -f -` with the manifest passed via stdin.

Do NOT attempt `kubectl patch --type=json --patch '[...]'` for changes whose
JSON body contains shell-unsafe characters.

Tool-selection by time intent — CRITICAL:

  "Current / active issues" (no time qualifier, or "now", "today"):
    - Use kubectl get pods --all-namespaces → shows live state only.
    - kubectl describe pod → shows Last State (exit reason + timestamp).
    - query_prometheus(range_minutes=0) → current metric snapshot (optional).
    - Do NOT use range_minutes>0 or query_loki for this case: they surface
      already-resolved problems and produce false positives.

  "Historical issues" (user says "last N hours/days", "yesterday", "last night"):
    - query_prometheus with range_minutes matching the window:
        increase(kube_pod_container_status_restarts_total[Nm]) ← pods that restarted
        kube_pod_container_status_last_terminated_reason        ← termination cause
    - query_loki with since="Nh" or since="Nd" → logs from that window.
    - kubectl describe pod → still useful for Last State timestamps.
    - Do NOT rely on kubectl get pods for history: it shows only current state.

  "Pods with issues" (no qualifier) means pods NOT in a desired state RIGHT NOW:
    - Non-Running phases: CrashLoopBackOff, Error, OOMKilled, ImagePullBackOff,
      Pending (stuck), Terminating (stuck), ContainerCreating (stuck).
    - kubectl get pods --all-namespaces is the correct and sufficient tool.

## Routing Decision
Choose investigation depth based on the request:

SIMPLE — answer directly from the Cluster Snapshot and/or tool calls.
  Use for: list requests, status checks, single-resource lookups, mutations.
  When listing resources, always show COMPLETE raw output in a code block.

TARGETED — emit exactly on its own line:
  TARGETED: namespace=<ns>, pod=<pod>, issue=<one-line description>
  Use for: ONE specific resource is failing and needs deeper investigation
  (describe, events, deployment check). The system runs parallel reads and
  returns the results to you for the final answer.
  Do NOT escalate to RCA_REQUIRED for single-resource issues — TARGETED is sufficient.

RCA_REQUIRED — emit exactly: RCA_REQUIRED
  Use for: multi-pod / cross-namespace outages, unknown root cause, cascading failures.
  The system dispatches 4 specialist subagents in parallel.

For mutations, NEVER use kubectl edit (no interactive terminal available).
Use kubectl patch or kubectl apply -f - with stdin instead.

IMPORTANT — ConfigMaps and content with special characters:
  When creating or updating a ConfigMap whose values contain HTML, JSON, YAML,
  or any multi-line / special-character content, ALWAYS use kubectl apply -f -
  with a YAML manifest passed via stdin. NEVER use --from-literal with such
  content — the argument quoting becomes fragile and error-prone.
  Example: kubectl apply -f - (then pass the full ConfigMap YAML in stdin)

When synthesizing subagent findings (messages contain <findings> XML):
  Produce a comprehensive root-cause analysis with a concrete fix recommendation.
  Be specific: name the exact resource, namespace, and remediation command.

{premise_clause}

IMPORTANT — Truncated output:
  If any tool output contains a truncation marker (text like "[truncated" or "chars omitted"),
  you MUST include a visible warning in your response, for example:
  "> ⚠️ Output was truncated — use narrower filters (e.g. `-n <namespace>`, `-l <label>`, `--tail`) to see the full result."
  Never silently drop this warning. The user must know the list is incomplete.
""".replace("{premise_clause}", PREMISE_CLAUSE)


# ── Investigation Plan prompt block ───────────────────────────────────────────

_PLAN_PROMPT_BLOCK = """\

## Investigation Plan
For queries requiring 3+ tool calls, write the plan as the FIRST line of your
response, in this exact format:

INVESTIGATION_PLAN:
- <step 1 description>
- <step 2 description>
- <step 3 description>
- ...

Then proceed with your tool calls. After all tool results return, your final
answer must address every step. Do not emit a plan for trivial single-call
queries — only when 3 or more steps are needed.
"""


# Shown to the operator when the coordinator's inner ReAct loop hits its budget.
# This is an escalation, not a result: it says what happened, that the work is
# incomplete, and what to do next — never a silent stop.
_BUDGET_EXHAUSTED_MESSAGE = (
    "I stopped because this investigation hit its tool-call budget "
    "({limit} recursion units) without reaching a conclusion.\n\n"
    "Nothing further was attempted, and no action was taken beyond what you can see above. "
    "This usually means the question was too broad, or I was looping on a tool that kept failing.\n\n"
    "You can: narrow the question to one namespace or workload, re-run it, "
    "or raise `AGENT_COORDINATOR_RECURSION_LIMIT` if this investigation legitimately needs more steps."
)


# ── Proactive Fix prompt block (injected when hitl_bypass=True) ───────────────

_PROACTIVE_FIX_BLOCK = """\

## Proactive Fix Mode (auto-approve active)
You are running in auto-approve mode — no human confirmation is required before
mutations. When you have identified the root cause and the fix is clear:

1. Apply the fix immediately via run_kubectl (patch / apply / create / delete).
   Do NOT say "let me know if you'd like me to apply this" — just apply it.
2. For ambiguous parameters (e.g. which image tag to use), pick the safest
   well-known default: `latest` for public images, the lowest-severity change
   for resource limits, etc. State your choice in the response.
3. Verify after every mutation: run `kubectl get` on the affected resource and
   report the actual post-fix state ("Pod is now Running (verified)").
4. If the fix cannot be determined with confidence, say so explicitly and stop —
   do not apply a guess.
"""


# ── Snapshot Sufficiency prompt block ─────────────────────────────────────────


def _snapshot_sufficiency_block(state: AgentState) -> str:
    """Render the Snapshot Sufficiency block when the mode is on.

    Returns an empty string when SNAPSHOT_SUFFICIENCY_MODE='off'.
    """
    mode = settings.SNAPSHOT_SUFFICIENCY_MODE
    if mode == "off":
        return ""
    age_s = max(0, int(time.time() - state.get("snapshot_built_at", time.time())))
    if state.get("snapshot_read_failed"):
        # The snapshot is not a snapshot. Asserting a pod count here is how a
        # credentials outage used to reach the model as "contains 0 pods,
        # issues=false, warnings=false" — together with an instruction to prefer
        # answering "how many pods" and "is the cluster healthy" without fetching.
        return """

## Snapshot Sufficiency

**The cluster snapshot is UNAVAILABLE — the read failed.** It reports no pod
count and no health flags, because none are known. An unavailable snapshot is
not an empty or healthy cluster.

- ALWAYS use a tool to fetch what you need; never answer from the snapshot.
- If the tool call fails too, say the cluster could not be reached and surface
  the error. Do not report zero pods, no warnings, or a healthy cluster.
"""
    issues = bool(state.get("snapshot_has_issues", False))
    warnings = bool(state.get("snapshot_has_warnings", False))
    pod_count = int(state.get("snapshot_pod_count", 0))
    fresh_threshold = settings.SNAPSHOT_FRESHNESS_SECONDS

    bias_strength = "Strongly prefer" if mode == "strict" else "Prefer"

    return f"""

## Snapshot Sufficiency

The cluster snapshot above was fetched {age_s}s ago and contains {pod_count} pods.
Health flags: issues={str(issues).lower()}, warnings={str(warnings).lower()}.

When the user asks a LIST-SHAPED, READ-ONLY question AND issues=false AND
warnings=false AND the snapshot is fresher than {fresh_threshold}s:
  - {bias_strength} answering directly from the snapshot.
  - Examples that qualify: "how many pods", "list namespaces", "is the cluster
    healthy", "show pods in default", "what's running".

ALWAYS fetch fresh data (regardless of the flags above) when:
  - The question mentions logs, metrics, history, "yesterday", "last N hours",
    "trend", or any time-windowed signal.
  - The question targets a SPECIFIC named pod/deployment/service for detail
    (use describe, get -o yaml, or logs).
  - You just performed a mutation (patch/apply/create/delete) — verify with a
    fresh get.
  - The question contains "now", "right now", "currently", "this second" — the
    user is asking explicitly about freshness.
  - The snapshot is older than {fresh_threshold}s.

If unsure, fetch. Stale answers are worse than redundant calls.
"""


# ── Matched-playbooks prompt block ────────────────────────────────────────────


def _playbooks_block(state: AgentState) -> str:
    """Render details of any playbooks whose triggers fired against the snapshot."""
    if not settings.PLAYBOOKS_ENABLED:
        return ""
    matched: list[str] = list(state.get("matched_playbooks") or [])
    if not matched:
        return ""

    try:
        from app.agent.playbooks import get_playbook
    except Exception:
        return ""

    sections: list[str] = [("\n## Recognized Failure Patterns\n"
                           "The snapshot matches these known patterns. Follow their\n"
                           "investigation steps before improvising.\n")]
    for name in matched:
        pb = get_playbook(name)
        if pb is None:
            continue
        steps = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(pb.investigation_steps))
        evidence = "\n".join(f"  - {e}" for e in pb.expected_evidence)
        sections.append(
            f"### {pb.name}\n"
            f"Investigation steps:\n{steps}\n"
            f"Look for:\n{evidence}\n"
            f"Fix template: {pb.recommended_fix_template}\n"
        )
    return "\n".join(sections)


# `Optional[RunnableConfig]`, not `RunnableConfig | None`: this module uses
# `from __future__ import annotations`, so LangGraph sees the annotation as a string and
# matches it against a fixed list ("RunnableConfig", "Optional[RunnableConfig]", …) in
# langgraph._internal._runnable.KWARGS_CONFIG_KEYS. The PEP-604 spelling is not on that
# list, so it would silently stop the run config being injected — taking user_role and
# hitl_bypass with it. Covered by tests/test_workflow_config_injection.py.
async def coordinator(state: AgentState, config: Optional[RunnableConfig] = None) -> dict:  # noqa: UP045
    """
    Coordinator node.  Always returns a plain state-update dict — never Send objects.

    Two modes:
    - Decision  : no findings yet → ask the LLM; if it says RCA_REQUIRED set the flag.
    - Synthesis : findings present → synthesize them into a final RCAResult.

    The fan-out itself is the responsibility of route_coordinator (workflow.py), which
    reads the rca_required flag and returns list[Send] for LangGraph to execute.
    """
    session_id = state.get("session_id", "-")
    user_id = state.get("user_id", "-")

    # ── Synthesis mode: subagent findings are ready ───────────────────────────
    if state.get("findings"):
        logger.debug(f"coordinator: synthesizing {len(state['findings'])} findings session={session_id}")
        await emit(session_id, StatusEvent(
            phase="synthesizing",
            message="Synthesizing specialist findings…",
            session_id=session_id,
        ))
        return await _synthesize(state)

    # ── Decision mode: ask the LLM ────────────────────────────────────────────
    await emit(session_id, StatusEvent(
        phase="analyzing",
        message="Analyzing your request…",
        session_id=session_id,
    ))

    last_user_msg = ""
    for m in reversed(state.get("messages", [])):
        if hasattr(m, "type") and m.type == "human":
            last_user_msg = m.content[:120] if isinstance(m.content, str) else ""
            break
    logger.debug(f"coordinator: invoking LLM user={user_id} session={session_id} msg={last_user_msg!r}")

    t0 = time.monotonic()
    try:
        result = await _direct_answer(state, config=config)
    except GraphInterrupt:
        raise  # HITL — expected, not a failure
    except Exception as exc:
        logger.error(f"coordinator: LLM call failed session={session_id} error={exc!r}")
        raise
    elapsed = time.monotonic() - t0

    # Guard: LLM returned nothing — context too large or rate-limited silently
    if not result.get("messages"):
        logger.warning(f"coordinator: LLM returned no messages session={session_id} — likely context overflow")
        error_text = (
            "I was unable to generate a response — the session context may have grown too large. "
            "Please start a new session to continue."
        )
        return {"messages": [AIMessage(content=error_text)]}

    last = result["messages"][-1].content.strip()
    is_rca = last == "RCA_REQUIRED"
    targeted_match = _TARGETED_RE.search(last) if not is_rca else None
    route = "RCA" if is_rca else "TARGETED" if targeted_match else "direct"
    logger.info(
        f"coordinator: routing_decision route={route} elapsed_ms={int(elapsed * 1000)}",
        extra={"session_id": session_id, "routing_decision": route, "elapsed_ms": int(elapsed * 1000)},
    )

    if targeted_match:
        ns = targeted_match.group(1).rstrip(",")
        pod = targeted_match.group(2).rstrip(",")
        issue = targeted_match.group(3).strip()
        logger.info(f"coordinator: TARGETED ns={ns} pod={pod} issue={issue!r} session={session_id}")
        await emit(session_id, StatusEvent(
            phase="investigating",
            message=f"Targeting {pod} in {ns}…",
            session_id=session_id,
        ))
        return {
            "targeted_investigation": {"namespace": ns, "pod": pod, "issue": issue},
            "rca_required": False,
        }

    if is_rca:
        logger.info("coordinator: LLM requested RCA — setting rca_required flag for fan-out")
        await emit(session_id, StatusEvent(
            phase="dispatching",
            message="Dispatching specialist subagents (pod · metrics · logs · events)…",
            session_id=session_id,
        ))
        # Do NOT add the "RCA_REQUIRED" sentinel text to message history.
        # route_coordinator reads rca_required and issues the Send fan-out.
        return {"rca_required": True}

    return result


async def _direct_answer(state: AgentState, config: Optional[RunnableConfig] = None) -> dict:  # noqa: UP045
    """Run coordinator LLM with tools for simple queries."""
    from langgraph.prebuilt import create_react_agent

    memory_context = state.get("memory_context", "")
    cluster_snapshot = state.get("cluster_snapshot", "")
    session_id = state.get("session_id", "-")

    system_parts: list[str] = [_COORDINATOR_SYSTEM]
    if settings.INVESTIGATION_PLAN_ENABLED:
        system_parts.append(_PLAN_PROMPT_BLOCK)
    if memory_context:
        system_parts.append(f"\n\n## Cluster Context\n{memory_context}")
    if cluster_snapshot:
        system_parts.append(f"\n\n{cluster_snapshot}")
    snapshot_block = _snapshot_sufficiency_block(state)
    if snapshot_block:
        system_parts.append(snapshot_block)
    playbook_block = _playbooks_block(state)
    if playbook_block:
        system_parts.append(playbook_block)
    hitl_bypass = bool((config or {}).get("configurable", {}).get("hitl_bypass", False))
    if hitl_bypass:
        system_parts.append(_PROACTIVE_FIX_BLOCK)

    llm = get_coordinator_llm()
    agent = create_react_agent(llm, tools=ALL_TOOLS)

    history, history_summary = _trim_session_messages(list(state["messages"]))
    if history_summary:
        system_parts.append(f"\n\n{history_summary}")
    input_messages = [SystemMessage(content="\n".join(system_parts))] + history

    # Bound the coordinator's inner ReAct loop explicitly. Without this it inherits
    # LangGraph's default (10007 ≈ 3,300 steps) from the parent config — unbounded in
    # practice, and this is the loop that holds the write-capable toolset. On exhaustion
    # we halt and escalate with whatever was found; we never truncate silently.
    react_config: RunnableConfig = {
        **(config or {}),
        "recursion_limit": settings.AGENT_COORDINATOR_RECURSION_LIMIT,
    }
    try:
        result = await agent.ainvoke({"messages": input_messages}, config=react_config)
    except GraphRecursionError:
        logger.error(
            "coordinator: tool-call budget exhausted "
            f"session={session_id} limit={settings.AGENT_COORDINATOR_RECURSION_LIMIT} — "
            "halting and escalating to the operator",
            extra={"session_id": session_id},
        )
        return {
            "messages": [AIMessage(content=_BUDGET_EXHAUSTED_MESSAGE.format(
                limit=settings.AGENT_COORDINATOR_RECURSION_LIMIT,
            ))],
        }

    new_messages = result["messages"][len(input_messages):]
    new_messages = _trim_tool_messages(new_messages)  # A4: cap tool output before state storage
    new_messages = _fill_orphan_tool_calls(new_messages)  # F2: HITL parallel-batch safety

    update: dict = {"messages": new_messages}

    # Extract investigation plan, annotate step statuses, emit PlanEvent.
    if settings.INVESTIGATION_PLAN_ENABLED:
        plan, new_messages = _extract_plan(new_messages)
        update["messages"] = new_messages
        if plan:
            _annotate_plan_steps(plan, new_messages)
            update["investigation_plan"] = plan
            await emit(session_id, PlanEvent(
                steps=[s.model_dump() for s in plan],
                session_id=session_id,
            ))
            logger.info(
                f"investigation_plan_emitted session={session_id} step_count={len(plan)}",
                extra={"session_id": session_id, "steps": [s.description for s in plan]},
            )

    tool_calls = sum(1 for m in new_messages if hasattr(m, "tool_calls") and m.tool_calls)
    logger.debug(
        f"coordinator: direct answer complete new_msgs={len(new_messages)} tool_calls={tool_calls}"
    )

    # F5b — Reflexion: record outcomes from the direct-answer path so future
    # sessions can learn from successful fixes (not only RCA-fan-out runs).
    _maybe_record_direct_outcome(state, new_messages)

    return update


# ── F5b/v2: outcome extraction & recording for direct-answer path ────────────

_MUTATION_VERBS = ("patch", "apply", "create", "delete", "scale", "set", "rollout", "edit", "replace")
_NAMESPACE_RE = re.compile(r"(?:^|\s)(?:-n|--namespace)[ =]([a-zA-Z0-9][a-zA-Z0-9\-]*)")
# Match `namespace: <ns>` inside YAML metadata blocks.
_YAML_NS_RE = re.compile(r"^\s*namespace:\s*([a-zA-Z0-9][a-zA-Z0-9\-]*)", re.MULTILINE)
# Transitional pod statuses — verification waits for these to clear.
_TRANSITIONAL_STATUSES = frozenset({"ContainerCreating", "Pending", "Terminating", "Init", "PodInitializing"})


def _ran_mutation(messages: list[BaseMessage]) -> tuple[bool, list[str]]:
    """Return (mutation_happened, list_of_kubectl_commands_run).

    Inspects AIMessage.tool_calls for `run_kubectl` invocations whose command
    starts with a mutation verb. Used both as a flag and as input to
    `_extract_mutation_pairs` for the structured fix payload.
    """
    commands: list[str] = []
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            cmd = (args or {}).get("command") or ""
            if not cmd:
                continue
            head = cmd.strip().split()
            verb_candidates = []
            if head and head[0] == "kubectl" and len(head) > 1:
                verb_candidates.append(head[1].lower())
            elif head:
                verb_candidates.append(head[0].lower())
            if any(v in _MUTATION_VERBS for v in verb_candidates):
                commands.append(cmd.strip()[:200])
    return bool(commands), commands


def _extract_mutation_pairs(messages: list[BaseMessage]) -> list[dict]:
    """Pair each kubectl mutation command with its stdin YAML if present.

    Reads tool_calls for `run_kubectl` and captures both `command` and any
    of {stdin, stdin_yaml, manifest, yaml} fields where the manifest payload
    typically lives. Each YAML is redacted via redact_secrets before storage.
    """
    from app.utils.redact import redact_secrets

    pairs: list[dict] = []
    redact = settings.REFLEXION_REDACT_SECRETS
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            args = args or {}
            cmd = args.get("command") or ""
            if not cmd:
                continue
            head = cmd.strip().split()
            verb_candidates = []
            if head and head[0] == "kubectl" and len(head) > 1:
                verb_candidates.append(head[1].lower())
            elif head:
                verb_candidates.append(head[0].lower())
            if not any(v in _MUTATION_VERBS for v in verb_candidates):
                continue
            stdin_yaml = (
                args.get("stdin")
                or args.get("stdin_yaml")
                or args.get("manifest")
                or args.get("yaml")
                or ""
            )
            cmd_clean = redact_secrets(cmd, max_chars=200) if redact else cmd[:200]
            yaml_clean = redact_secrets(stdin_yaml, max_chars=1500) if redact else (stdin_yaml or "")[:1500]
            pairs.append({"command": cmd_clean, "stdin_yaml": yaml_clean or None})
    return pairs


def _last_user_text(state: AgentState) -> str:
    """Most recent human message content (for episode trigger_detail)."""
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage) and isinstance(message.content, str):
            return message.content
    return ""


def _outcome_key(state: AgentState, namespace: str | None) -> str:
    """Build a stable structured pattern key (R2)."""
    cluster_id = state.get("cluster_id") or "unknown"
    matched = sorted(set(state.get("matched_playbooks") or []))
    if matched:
        playbook_part = "+".join(matched)
        ns_part = f" | ns={namespace}" if namespace else ""
        return f"playbook={playbook_part}{ns_part} | cluster={cluster_id}"

    # Fallback: query stub (low-quality path; never promotes).
    last_user = ""
    for m in reversed(state.get("messages", [])):
        if hasattr(m, "type") and m.type == "human" and isinstance(m.content, str):
            last_user = m.content.strip().replace("\n", " ")[:60]
            break
    return f"query={last_user} | cluster={cluster_id}"


def _infer_namespace(commands: list[str], pairs: list[dict] | None = None) -> str | None:
    """Pick the most-cited namespace across kubectl args AND stdin YAML metadata.

    `kubectl apply -f -` has no `-n` flag — the namespace is inside the YAML.
    Without scanning the manifest the namespace column ends up empty.
    """
    counts: dict[str, int] = {}
    for cmd in commands:
        m = _NAMESPACE_RE.search(cmd)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    for pair in (pairs or []):
        yaml_text = pair.get("stdin_yaml") or ""
        for m in _YAML_NS_RE.finditer(yaml_text):
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _wait_for_rollout(namespace: str, max_wait_s: int = 30, poll_interval_s: float = 2.0) -> None:
    """Block until pods in `namespace` clear transitional statuses, or timeout.

    A `kubectl apply` returns immediately but the rollout takes seconds. Without
    waiting, verification snapshots a half-rolled cluster and reports "partial"
    even when the fix is correct. We poll cheaply via the existing snapshot
    helper — no extra dependency.
    """
    from app.agent.nodes.context_fetcher import _run_kubectl_snapshot

    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        pods_out = _run_kubectl_snapshot(["get", "pods", "-n", namespace, "--no-headers"])
        in_transition = False
        for line in pods_out.splitlines():
            cols = line.split()
            if len(cols) < 3:
                continue
            status = cols[2]
            # `Init:0/1` etc — match the prefix.
            if status in _TRANSITIONAL_STATUSES or status.startswith(("Init:", "PodInitializing")):
                in_transition = True
                break
        if not in_transition:
            return
        # Sleep with deadline guard — never overshoot max_wait_s significantly.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(poll_interval_s, remaining))


def _verify_resolution(namespace: str | None, pre_state: dict | None = None) -> tuple[bool | None, str | None]:
    """Re-snapshot the cluster after a fix to verify resolution (R4).

    Returns (verified_resolved, feedback_label) where:
      verified_resolved: True if no issues+warnings, False if still broken,
                         None if verification disabled or failed to run.
      feedback_label: 'resolved' | 'partial' | 'regression' | None
    """
    if not settings.REFLEXION_VERIFY_RESOLUTION:
        return None, None
    try:
        from app.agent.nodes.context_fetcher import (
            _kubectl_snapshot,
            _scan_snapshot,
            _unavailable_reason,
        )

        # Wait for any rolling deployment to settle before snapshotting.
        # Without this, freshly-applied fixes get penalised for transitional
        # ContainerCreating / Pending statuses they're about to leave.
        if namespace:
            _wait_for_rollout(namespace)

        ns_arg = ["-n", namespace] if namespace else ["--all-namespaces"]
        pods_ok, pods_out = _kubectl_snapshot(["get", "pods", *ns_arg])
        events_ok, events_out = _kubectl_snapshot([
            "get", "events", *ns_arg,
            "--sort-by=.lastTimestamp",
            "--field-selector=type=Warning",
        ])
        if not pods_ok:
            # This is the "failed to run" case the docstring promises. It never
            # fired before: the runner swallowed kubectl's exit code, so a failed
            # post-fix read scanned clean and this function returned
            # (True, "resolved") — recording an unverified fix as verified, which
            # `promotion.py` then selects on (`WHERE verified = TRUE`) to mint
            # learned rules and detector candidates. A read is most likely to fail
            # right after a disruptive change, which is exactly when this runs.
            logger.warning(
                "reflexion: cannot verify resolution — the post-fix cluster read "
                "failed (%s); recording the outcome as unverified, not resolved",
                _unavailable_reason(pods_out),
            )
            return None, None
        has_issues, has_warnings, _ = _scan_snapshot(
            pods_out, events_out, pods_ok=pods_ok, events_ok=events_ok)

        if not has_issues:
            # Pods are healthy — even if old warning events linger, the fix
            # is in effect. Stale events are not regressions.
            return True, "resolved"

        # Still broken — compare with pre-state if provided to detect regression.
        if pre_state and pre_state.get("had_issues") is False and has_issues:
            return False, "regression"
        return False, "partial"
    except Exception as exc:
        logger.debug(f"reflexion: verify_resolution failed — {exc}")
        return None, None


def _resolve_confidence(
    state: AgentState,
    *,
    has_playbook: bool,
    verified: bool | None,
) -> float:
    """Confidence resolution table (R5).

    Synthesis path uses model-reported confidence (handled in _synthesize).
    This is the direct-answer path:
      verified + playbook  → 0.9 (eligible for promotion)
      verified, no playbook → 0.7
      not verified, playbook → 0.7 (rca_outcomes only)
      not verified, no playbook → 0.5 (low-priority retention)
    """
    if verified and has_playbook:
        return 0.9
    if verified or has_playbook:
        return 0.7
    return 0.5


def _maybe_record_direct_outcome(state: AgentState, messages: list[BaseMessage]) -> None:
    """Persist a structured, verified outcome when a direct-answer run mutated state.

    Fire-and-forget. Skipped on read-only queries and SQLite mode. Verification
    happens synchronously (one extra ~150ms kubectl pair) so we know whether
    the outcome is eligible for pattern promotion before we write.
    """
    if not settings.REFLEXION_ENABLED or settings.USE_SQLITE:
        return
    mutated, commands = _ran_mutation(messages)
    if not mutated:
        return

    pairs = _extract_mutation_pairs(messages)
    # Namespace inference must include stdin YAML — `kubectl apply -f -` has no -n flag.
    namespace = _infer_namespace(commands, pairs)

    # R4 — verify on cluster (waits for rollout to settle before snapshotting)
    pre_state = {
        "had_issues": bool(state.get("snapshot_has_issues")),
        "had_warnings": bool(state.get("snapshot_has_warnings")),
    }
    verified, feedback = _verify_resolution(namespace, pre_state=pre_state)

    # R2 — structured key
    key = _outcome_key(state, namespace)

    # R3 — manifest-aware fix payload
    import json as _json
    fix_payload = _json.dumps(pairs, ensure_ascii=False)[:8000]

    # R5 — confidence
    has_playbook = bool(state.get("matched_playbooks"))
    confidence = _resolve_confidence(state, has_playbook=has_playbook, verified=verified)

    # R1 / R8 — cluster identity + redacted root_cause
    from app.cluster_id import get_cluster_id
    cluster_id = state.get("cluster_id") or get_cluster_id()

    try:
        import asyncio as _asyncio

        from app.db.memory_store import record_rca_outcome

        _asyncio.create_task(record_rca_outcome(
            session_id=state.get("session_id", "-"),
            user_id=state.get("user_id", "-"),
            root_cause=key[:240],
            confidence=confidence,
            recommended_fix=fix_payload,
            outcome_feedback=feedback,
            cluster_id=cluster_id,
            namespace=namespace,
            verified_resolved=verified,
            playbooks_matched=list(state.get("matched_playbooks") or []),
            created_by_role=state.get("user_role"),
        ))
        # V4 hippocampus: the same outcome becomes an L1 episode.
        from app.memory.episodes import write_episode
        _asyncio.create_task(write_episode(
            cluster_id=cluster_id,
            namespace=namespace,
            trigger_kind="user_query",
            trigger_detail=_last_user_text(state)[:300],
            summary=f"Direct fix in ns={namespace}: {key[:200]}",
            root_cause=key[:240],
            actions=pairs,
            outcome=feedback,
            verified=verified,
            confidence=confidence,
            playbooks=list(state.get("matched_playbooks") or []),
            created_by_role=state.get("user_role"),
            request_id=state.get("session_id"),
        ))
        logger.info(
            f"rca_outcome_written session={state.get('session_id', '-')} "
            f"cluster={cluster_id} confidence={confidence:.2f}",
            extra={
                "session_id": state.get("session_id", "-"),
                "cluster_id": cluster_id,
                "namespace": namespace,
                "confidence": confidence,
                "verified": verified,
            },
        )
    except Exception as exc:
        logger.warning(f"reflexion(direct): failed to schedule outcome write — {exc}")


async def _synthesize(state: AgentState) -> dict:
    """Synthesize subagent findings into a final RCAResult."""
    import json

    from langchain_core.messages import HumanMessage

    from app.agent.state import RCAResult

    findings = state["findings"]
    findings_xml = "\n".join(
        f"<finding domain='{f.domain}' confidence='{f.confidence}'>\n"
        f"  hypothesis: {f.hypothesis}\n"
        f"  signals: {', '.join(f.signals)}\n"
        f"  evidence: {chr(10).join(f.evidence[:3])}\n"
        f"</finding>"
        for f in findings
    )

    synthesis_prompt = f"""
You have received findings from 4 specialist subagents:

<findings>
{findings_xml}
</findings>

Synthesize these into a single root-cause analysis. Respond with ONLY a JSON object:
{{
  "root_cause": "<single-sentence root cause>",
  "confidence": <0.0-1.0>,
  "supporting_evidence": ["<evidence 1>", ...],
  "conflicting_evidence": ["<conflict 1>" or []],
  "reasoning": "<chain-of-thought over the findings>",
  "recommended_fix": "<concrete kubectl/config fix>",
  "affected_domain": ["<domain>", ...]
}}
"""

    llm = get_coordinator_llm()
    response = await llm.ainvoke(
        [SystemMessage(content=_COORDINATOR_SYSTEM), HumanMessage(content=synthesis_prompt)]
    )

    # `.text` flattens a content-block list (some providers never return a bare
    # string) into the concatenated text; `.content.strip()` would raise on one.
    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        raw = raw.removeprefix("json")

    try:
        rca = RCAResult.model_validate(json.loads(raw.strip()))
    except Exception as exc:
        logger.warning(f"coordinator: failed to parse RCA JSON — {exc}")
        rca = RCAResult(
            root_cause="Synthesis failed — see individual findings",
            confidence=0.0,
            supporting_evidence=[f.hypothesis for f in findings],
            reasoning=f"Parse error: {exc}",
            recommended_fix="Review findings manually",
        )

    summary = (
        f"**Root Cause**: {rca.root_cause}\n\n"
        f"**Confidence**: {rca.confidence:.0%}\n\n"
        f"**Recommended Fix**: {rca.recommended_fix}\n\n"
        f"**Reasoning**: {rca.reasoning}"
    )

    # F5 — Reflexion loop: persist high-confidence outcomes so future sessions
    # benefit. Fire-and-forget; failure to write must never break the response.
    if (
        settings.REFLEXION_ENABLED
        and rca.confidence >= settings.REFLEXION_MIN_CONFIDENCE
        and not settings.USE_SQLITE
    ):
        try:
            import asyncio as _asyncio

            from app.db.memory_store import record_rca_outcome

            _asyncio.create_task(record_rca_outcome(
                session_id=state.get("session_id", "-"),
                user_id=state.get("user_id", "-"),
                root_cause=rca.root_cause,
                confidence=rca.confidence,
                recommended_fix=rca.recommended_fix,
                outcome_feedback=None,
            ))
            from app.cluster_id import get_cluster_id as _gcid
            from app.memory.episodes import write_episode
            _asyncio.create_task(write_episode(
                cluster_id=state.get("cluster_id") or _gcid(),
                trigger_kind="user_query",
                trigger_detail=_last_user_text(state)[:300],
                summary=summary[:1200],
                root_cause=rca.root_cause,
                outcome="report_only",
                verified=None,
                confidence=rca.confidence,
                playbooks=list(state.get("matched_playbooks") or []),
                created_by_role=state.get("user_role"),
                request_id=state.get("session_id"),
            ))
            logger.info(
                f"rca_outcome_written session={state.get('session_id', '-')} "
                f"confidence={rca.confidence:.2f}",
                extra={
                    "session_id": state.get("session_id", "-"),
                    "confidence": rca.confidence,
                },
            )
        except Exception as exc:
            logger.warning(f"reflexion: failed to schedule outcome write — {exc}")

    return {
        "rca_result": rca.model_dump(),  # plain dict avoids LangGraph msgpack serialization warning
        "rca_required": False,
        "messages": [AIMessage(content=summary)],
    }
