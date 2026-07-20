# app/orchestration/routing.py
"""
KubeIntellect supervisor and worker routing.

Contains the supervisor prompt/chain, the supervisor router node function,
the clarification-loop checker, and the worker-node factory.
"""

import re
import warnings
from typing import Any, Dict, Optional, Sequence, Tuple

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

# Suppress Pydantic v2 serialization warnings from LangChain's structured output parser.
# with_structured_output() uses an internal PydanticToolsParser whose `parsed` field is
# typed Optional[None] but receives a SupervisorRoute object — this is a known
# LangChain/Pydantic v2 compatibility issue and does not affect correctness.
warnings.filterwarnings(
    "ignore",
    message=".*PydanticSerializationUnexpectedValue.*",
    category=UserWarning,
)

from app.orchestration.schemas import (  # noqa: E402
    build_agent_result,
    PlanExecutionState,
    SequentialStep,
    TaskPlan,
)
from app.orchestration.state import (  # noqa: E402
    AGENT_MEMBERS,
    MAX_CLARIFICATIONS,
    SUPERVISOR_OPTIONS,
    KubeIntellectState,
    SupervisorRoute,
)
from app.utils.logger_config import setup_logging  # noqa: E402
from app.utils.metrics import agent_invocations_total, tool_call_suppressed_total  # noqa: E402
from app.utils.otel_guard import safe_otel_ctx  # noqa: E402

logger = setup_logging(app_name="kubeintellect")

# ---------------------------------------------------------------------------
# 4xx tool-error detection
# ---------------------------------------------------------------------------

_HTTP_STATUS_RE = re.compile(r"\b(4\d{2})\b")


def _extract_http_status(text: str) -> Optional[int]:
    """Return the first HTTP 4xx status code found in *text*, or None."""
    m = _HTTP_STATUS_RE.search(text)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Capability manifest — auto-generated from actual tool lists
# ---------------------------------------------------------------------------

# Maps each agent name to its tool_categories key(s).
AGENT_TO_CATEGORIES: dict[str, list[str]] = {
    "Logs":                  ["logs"],
    "ConfigMapsSecrets":     ["configs"],
    "RBAC":                  ["rbac"],
    "Metrics":               ["metrics"],
    "Security":              ["security"],
    "Lifecycle":             ["lifecycle"],
    "Execution":             ["execution"],
    "Deletion":              ["deletion"],
    "Infrastructure":        ["advancedops"],
    "CodeGenerator":         ["code_gen"],
    "Apply":                 ["apply"],
    "DynamicToolsExecutor":  ["dynamic"],
    # DiagnosticsOrchestrator is a custom meta-node (no direct tools);
    # it orchestrates Logs + Metrics + Events in parallel via Send API.
    # It does not appear in the capability manifest — described in the supervisor prompt.
}

# Agents that are implemented as custom nodes (not as create_react_agent workers).
# These are present in AGENT_MEMBERS so the supervisor can route to them, but they
# are wired directly into the graph in workflow.py rather than via create_worker_nodes.
CUSTOM_NODE_AGENTS: frozenset[str] = frozenset({"DiagnosticsOrchestrator"})


def build_agent_capability_manifest(tool_categories: dict) -> str:
    """Auto-generate the agent capability section from actual tool lists.

    Introspects each agent's tool list so the supervisor prompt always reflects
    the real tools — no manual sync required when tools are added or renamed.
    """
    lines = [
        "\n## Agent Capability Map",
        "Route to the agent whose tools match the request. "
        "Each specialist agent can ONLY call tools listed under its name.\n",
    ]
    for agent_name, keys in AGENT_TO_CATEGORIES.items():
        tools: list = []
        seen: set = set()
        for key in keys:
            for t in tool_categories.get(key, []):
                if t.name not in seen:
                    seen.add(t.name)
                    tools.append(t)
        tool_entries = ", ".join(
            f"{t.name} ({t.description[:70].rstrip()}{'…' if len(t.description) > 70 else ''})"
            for t in tools
        ) or "(no tools)"
        lines.append(f"- **{agent_name}**: {tool_entries}")

    return "\n".join(lines)


class _NullAgentCtx:
    """No-op context manager — used when Langfuse is disabled or import fails."""
    def __enter__(self): return self
    def __exit__(self, *_): pass


# ---------------------------------------------------------------------------
# Supervisor chain
# ---------------------------------------------------------------------------

def create_supervisor_prompt(tool_categories: dict | None = None) -> ChatPromptTemplate:
    """Create the supervisor prompt template.

    Args:
        tool_categories: Optional dict from load_all_tool_categories(). When provided,
            an auto-generated agent capability map is injected into the prompt so routing
            decisions are grounded in the actual tool lists — no manual sync needed.
    """
    # Auto-generate the capability manifest from real tool lists (if available).
    capability_section = (
        build_agent_capability_manifest(tool_categories) if tool_categories else ""
    )

    system_prompt = (
        "You are an expert Kubernetes operations supervisor managing a team: {members}. "
        "Your primary goal is to fulfill the user's request by delegating to the appropriate worker or concluding if the task cannot be done. "
        "Always review the ENTIRE conversation history, especially the LATEST worker's report, before making a decision. "
        "If a ⚠️ REFLECTION MEMORY block appears in the conversation, read it before applying any routing rule. "
        "Routing lessons in that block are higher-priority overrides — if a lesson conflicts with a default rule below, the lesson wins. "
        "CRITICAL: Do NOT re-delegate the exact same task to a worker if it has just reported it CANNOT perform it due to missing tools or capabilities. Instead, choose a different strategy. "
        "\n**IMPORTANT: If any worker reports 'I don't have the necessary tools available', immediately delegate to CodeGenerator to create the needed tool.**"
        "\n**SCOPE CHECK: Before delegating, verify the request is Kubernetes-related. If the user's query is completely unrelated to Kubernetes operations "
        "(e.g., general programming, unrelated system administration, personal questions), immediately select FINISH and provide a polite message: "
        "'I'm sorry, but this question is outside the scope of KubeIntellect. I specialize in Kubernetes operations and cluster management. "
        "Could you please ask a Kubernetes-related question instead?'**"
        "\n**CAPABILITY QUESTIONS: If the user asks about your capabilities, what you can do, or whether you can handle certain queries "
        "(e.g., 'can you solve complex queries?', 'what can you do?', 'are you able to...?'), immediately select FINISH and respond with a "
        "helpful, enthusiastic answer that showcases KubeIntellect's power. Include a concrete list of example queries the user can try, such as: "
        "'Roll back the payment-service deployment to the previous version', "
        "'Scale the api-gateway to 5 replicas in the production namespace', "
        "'Show me all pods that are in CrashLoopBackOff across all namespaces', "
        "'Create a deployment for nginx with 3 replicas, cpu_limit=500m, memory_limit=256Mi', "
        "'List all services that expose port 8080', "
        "'Check which pods are consuming the most CPU in the default namespace', "
        "'Drain node worker-2 safely before maintenance', "
        "'Create a ClusterRole that allows read-only access to pods and deployments', "
        "'Show me the last 100 error logs from the order-service pod', "
        "'What events happened in the kube-system namespace in the last hour?'. "
        "Emphasize that for operations not natively supported, KubeIntellect can generate custom tools on demand. "
        "Do NOT delegate to a worker or run any Kubernetes commands for capability questions.**"
        "\n**NEXT STEP / PLANNING QUERIES: If the user asks what to do next, what the next step is, "
        "or asks for a plan or guidance (e.g., 'what is the next step?', 'what should I do?', "
        "'what should I do next?', 'help me plan', 'what do you suggest?', 'what can I check?', "
        "'where do I start?', 'what now?', 'any suggestions?'): "
        "Look at the ENTIRE conversation history to understand what has already been done, "
        "then immediately select FINISH and respond with 3–5 concrete, actionable next steps tailored to the context. "
        "Rules for your response: "
        "(a) If prior operations are visible in the history (e.g., a deployment was just created, a pod is in CrashLoopBackOff, "
        "a namespace was just set up), suggest the natural follow-up actions — e.g., check rollout status, inspect logs, "
        "set resource limits, configure an HPA, add a NetworkPolicy, create a Service or Ingress. "
        "(b) If there is no prior Kubernetes context, suggest a general cluster health workflow — e.g., "
        "list all pods across namespaces, check for Warning events, inspect resource usage, review RBAC policies. "
        "(c) Always phrase each suggestion as a concrete query the user can paste directly, for example: "
        "'Check rollout status: get_deployment_rollout_status for <deployment-name> in <namespace>', "
        "'Inspect logs: show me the last 50 lines of logs from <pod-name>', "
        "'Set resource limits: update the deployment to add cpu_limit=500m memory_limit=256Mi'. "
        "(d) Do NOT delegate to any worker agent. Do NOT run any Kubernetes commands. "
        "Select FINISH and provide the suggestions directly.**"
        "\n**MULTI-STEP PLAN GENERATION: For queries that clearly require 3+ sequential operations "
        "where each step maps to a single specialist agent AND later steps do NOT depend on the output "
        "of earlier steps (e.g., 'drain node X, check cluster health, then list pending pods'), "
        "return a task_plan with steps. Each step has: 'agent' (agent node name from the list above), "
        "'task' (precise description of ONLY what this step should do — scoped to a single action), "
        "and optional 'input_spec' (dict mapping local key → 'AgentName.field' from a prior step's result). "
        "Also set query_summary to one sentence describing the full plan. "
        "The plan will be executed deterministically — you will NOT be called again for plan steps. "
        "CRITICAL: Each step's 'task' field must describe ONLY that step's action. "
        "Never include 'and clean up' or 'and delete' in a step that is not the dedicated cleanup step. "
        "Example: query='drain node worker-1, check namespace health, list recent events' → "
        "task_plan={{'steps': ["
        "{{'agent': 'Lifecycle', 'task': 'Cordon and drain node worker-1', 'input_spec': {{}}}}, "
        "{{'agent': 'Metrics', 'task': 'Check namespace health metrics', 'input_spec': {{}}}}, "
        "{{'agent': 'Logs', 'task': 'List recent warning events', 'input_spec': {{}}}}], "
        "'query_summary': 'Drain node worker-1, collect metrics, then fetch recent events'}}, "
        "next='Lifecycle'. "
        "ONLY generate a task_plan when: (a) 3–10 clearly distinct sequential sub-tasks, "
        "(b) each sub-task maps unambiguously to one agent, "
        "(c) no step needs the previous step's output to determine ROUTING "
        "(you may set input_spec to pass data between steps). "
        "Do NOT generate a task_plan for 'find the failing pod THEN show its logs' — step 2 routing depends on step 1. "
        "When in doubt, route one step at a time.**"
        "\n" + capability_section.replace("{", "{{").replace("}", "}}") + "\n"
        "\nBased on the user's request and conversation history, follow these routing rules: "
        "0. **CONTEXT-ONLY messages** (greetings, acknowledgements, social filler): "
        "   If the user's latest message contains NONE of these action-intent verbs — "
        "   'check', 'show', 'list', 'get', 'describe', 'find', 'fix', 'what', 'why', 'how', 'is', 'are', 'can', "
        "   'create', 'delete', 'scale', 'restart', 'update', 'set', 'add', 'remove', 'patch', 'apply', 'run', "
        "   'drain', 'cordon', 'rollout', 'deploy', 'fetch', 'give', 'tell', 'explain', 'watch' — "
        "   AND the message is a short social phrase (e.g., 'ok', 'thanks', 'got it', 'hello', 'hi', 'sure', 'great'), "
        "   immediately select FINISH and respond conversationally. Do NOT delegate to any worker. "
        "   Log: supervisor:context_only_shortcircuit. "
        "   IMPORTANT: Apply this rule ONLY when there is genuinely no action intent. "
        "   When in doubt, route to a worker — false positives (treating real requests as context-only) are worse than a 2-second overhead. "
        "1. **If a worker is asking for clarification** (e.g., 'Which namespace?', 'Please specify the pod name', "
        "   'Should I...?', 'Do you want...?', questions that REQUIRE user input before proceeding), "
        "   immediately select FINISH and set needs_human_input=true. Do not delegate to another worker. "
        "   EXCEPTION — Incident RCA context: If DiagnosticsOrchestrator has already run (its report appears "
        "   in the conversation), do NOT treat 'Would you like me to...' from a worker as a blocking clarification. "
        "   Instead: "
        "   (a) If Logs just ran but the api-server/crash root cause is not yet confirmed → route to 'Logs' again "
        "       for describe_kubernetes_pod on the crashing pod, OR route to 'Lifecycle' to describe the pod. "
        "   (b) After Logs investigates crash pods, ALWAYS route to 'Infrastructure' to check service endpoints "
        "       in the namespace (list_services + check endpoints) — silent selector mismatches won't appear "
        "       in the DiagnosticsOrchestrator triage. "
        "   (c) Only FINISH after Infrastructure has reported on service endpoints AND crash root causes are named. "
        "   These 'Would you like me to...' offers are NOT blocking — continue investigation without user input. "
        "1b. **CRITICAL — Dry-run confirmation re-route**: "
        "If a worker agent previously showed a [DRY RUN] diff and asked the user to confirm, "
        "and the user's latest message is a short confirmation ('yes', 'confirm', 'proceed', 'apply', 'do it', 'go ahead'), "
        "route back to the SAME worker agent that showed the diff. "
        "The agent will re-invoke its tool with dry_run=False. "
        "Do NOT apply any other routing rules in this case. Do NOT route to FINISH. Do NOT route to a different agent. "
        "2. **If any worker reports 'I don't have the necessary tools available'** or similar messages indicating missing capabilities, "
        "   immediately delegate to 'CodeGenerator' with a clear description of what tool needs to be created. "
        "3. **If CodeGenerator reports 'Tool Already Exists'**, immediately delegate to 'DynamicToolsExecutor' to use the existing tool. "
        "4. **If CodeGenerator successfully creates a new tool**, the workflow will automatically FINISH due to the need for system reload. "
        "5. **For straightforward Kubernetes read/write/create operations**, route to the most specific specialist: "
        "   - 'list pods', 'list all pods', 'give me list of pods', 'describe pod', 'get pod status', 'show pods', 'what pods are running' → 'Lifecycle' (has pod_tools). "
        "   - 'describe deployment', 'show env vars', 'show environment variables', 'what env vars does X have', "
        "     'show image tag', 'what image is X running', 'show probes', 'show resource limits' → 'Lifecycle' (has deployment_tools). "
        "   - 'create deployment', 'deploy X', 'create a deployment for X' → 'Lifecycle'. NEVER use CodeGenerator for deployment creation. "
        "   - 'create service', 'list services', 'describe service', 'expose port' → 'Infrastructure' (has service_tools). NEVER use CodeGenerator for service creation. "
        "   - 'add labels/annotations to namespace', 'patch namespace labels' → 'Infrastructure' (has namespace_tools). "
        "   5a. **CRITICAL — Deployment inspection queries ALWAYS go to 'Lifecycle', NEVER to 'ConfigMapsSecrets'**: "
        "   'show env vars', 'show environment variables', 'what env vars does X have', 'show image tag', "
        "   'what image is X running', 'show probes', 'describe deployment', 'show resource limits' — "
        "   these read from the Deployment spec via the Kubernetes API. They have NOTHING to do with ConfigMaps or Secrets. "
        "   5b. For 'create deployment' → 'Lifecycle'. "
        "   5c. For 'create service' or 'list services' → 'Infrastructure'. "
        "   5d. For 'add labels/annotations to namespace' → 'Infrastructure'. "
        "6. **CRITICAL — Pod restart counts are NOT metrics**: "
        "   'restart count', 'how many times restarted', 'which pods have restarted', 'pods restarted more than N times', 'last restart time' — "
        "   MUST go to 'Logs', NEVER to 'Metrics'. "
        "   Restart counts come from the Kubernetes API (pod.status.containerStatuses[].restartCount), not Prometheus. "
        "6b. **For CPU/memory/resource-usage requests** → delegate to 'Metrics'. "
        "   ALWAYS route 'top nodes', 'node cpu usage', 'node memory usage', 'which node uses most cpu', "
        "   'top pods', 'pod cpu usage', 'pod memory usage', 'which pod uses most memory' to 'Metrics' (has top_nodes, top_pods). "
        "   Only escalate to 'CodeGenerator' if Metrics explicitly says it cannot handle the request even with Prometheus. "
        "7. **For log/event requests** (pod logs, namespace events, container output) → delegate to 'Logs'. "
        "   CRITICAL: 'Logs' handles log *content* retrieval only — do NOT route pod enumeration (list pods, describe pod) to 'Logs'. "
        "   For 'Warning events' or 'events grouped by reason' — Logs has get_namespace_warning_events, do NOT use CodeGenerator. "
        "   For CrashLoopBackOff pod logs — Logs can retry with previous=True to fetch the previous container's logs. "
        "   ALWAYS route 'show events', 'what events happened', 'watch events', 'events for pod X', "
        "   'events in namespace Y', 'recent Warning events' to 'Logs' (has events_watch). "
        "   For 'rollout status' of a Deployment → delegate to 'Lifecycle' instead. "
        "8. **For ConfigMap and Secret CRUD** → delegate to 'ConfigMapsSecrets'. "
        "   CRITICAL: Do NOT route 'show env vars', 'show image tag', 'describe deployment', 'show probes', or 'show resource limits' to 'ConfigMapsSecrets'. "
        "   Those are Deployment spec queries — route them to 'Lifecycle'. 'ConfigMapsSecrets' handles ONLY ConfigMap and Secret CRUD. "
        "   ALWAYS route 'create secret', 'create configmap', 'patch configmap key', 'update configmap', 'update secret', "
        "   'fix password in secret', 'patch secret key', 'change secret value' to 'ConfigMapsSecrets' — do NOT use CodeGenerator for these. "
        "   EXCEPTION — Pod startup failure suspected to involve ConfigMap/Secret: "
        "   Route to 'Logs' FIRST to get pod events (which name the specific missing ConfigMap or Secret). "
        "   Only route to 'ConfigMapsSecrets' AFTER Logs identifies the missing resource by name. "
        "   Example: 'app failing to start, investigate ConfigMap' → Logs (pod events name 'nonexistent-config') → ConfigMapsSecrets (confirm it doesn't exist) → FINISH. "
        "8b. **For RBAC/access-control/role/rolebinding requests** → delegate to 'RBAC'. "
        "8c. **For security/audit/vulnerability/policy requests** → delegate to 'Security'. "
        "   EXCEPTION: 'cannot reach', 'traffic blocked', 'connectivity issue', 'NetworkPolicy blocking' are CONNECTIVITY investigations → delegate to 'Infrastructure', NOT 'Security'. "
        "   Use 'Security' only for security posture audits, vulnerability scans, RBAC/policy compliance — NOT for diagnosing pod-to-pod connectivity failures. "
        "8d. **For scaling/restart/rollout/cordon/uncordon/drain/HPA/StatefulSet/lifecycle operations, "
        "   OR creating a deployment, OR updating a deployment's container image or command, OR checking rollout status** → delegate to 'Lifecycle'. "
        "   ALWAYS route 'cordon node', 'uncordon node', 'drain node' to 'Lifecycle' — do NOT use CodeGenerator. "
        "   ALWAYS route 'update image', 'fix image', 'change container image', 'update command', 'fix command', "
        "   'CrashLoopBackOff fix', 'rollout status', 'rollout restart', 'restart deployment' to 'Lifecycle'. "
        "   ALWAYS route 'rollback statefulset', 'undo statefulset', 'rollout undo statefulset', "
        "   'pause rollout', 'resume rollout', 'rollout history', 'rollout pause', 'rollout resume' to 'Lifecycle'. "
        "   ALWAYS route 'set env', 'add env var', 'update env var', 'remove env var', "
        "   'change environment variable', 'patch resource', 'add label', 'remove label', "
        "   'update label', 'add annotation', 'remove annotation', 'annotate resource', "
        "   'rollout restart statefulset', 'restart daemonset', 'rollout restart daemonset' to 'Lifecycle'. "
        "   NEVER use CodeGenerator for env var changes, labels, annotations, or patch operations. "
        "   ALWAYS route HPA and StatefulSet create/scale/describe/delete to 'Lifecycle' — do NOT use CodeGenerator for these. "
        "   ALWAYS route 'describe X', 'what is wrong with X', 'show status of X', 'describe pod/deployment/service/node' to 'Lifecycle' (has describe_resource for any kind). "
        "   NEVER use CodeGenerator for describe operations. "
        "   For 'patch ConfigMap key and restart deployment' — first delegate to 'ConfigMapsSecrets', then route to 'Lifecycle' for rollout restart. "
        "   For 'pod is Pending' investigations: after Lifecycle reports scheduler events (e.g. Insufficient memory/cpu), "
        "   ALSO route to 'Infrastructure' to check node allocatable capacity unless the root cause is already clear. "
        "   After Infrastructure reports node capacity, FINISH with a recommendation. "
        "8e. **For exec/attach/port-forward/interactive container operations** → delegate to 'Execution'. "
        "8f. **For delete/remove/cleanup operations on any resource** → delegate to 'Deletion'. "
        "   ALWAYS route any delete/remove/cleanup operation to 'Deletion' — do NOT use CodeGenerator for deletes. "
        "   For queries that delete MULTIPLE resources, delegate to 'Deletion' once — it can handle multiple deletes. "
        "8g. **For networking/storage/PV/PVC/NetworkPolicy/ResourceQuota/LimitRange/Job/CronJob/Service operations** → delegate to 'Infrastructure'. "
        "   ALWAYS route ResourceQuota, LimitRange, NetworkPolicy, and PVC operations to 'Infrastructure' — do NOT use CodeGenerator. "
        "   For 'cannot reach', 'traffic blocked', 'NetworkPolicy blocking', 'pod cannot connect' → route to 'Infrastructure' to check service endpoints AND list/describe NetworkPolicies. "
        "   ALWAYS route 'list jobs', 'list failed jobs', 'describe job', 'list cronjobs' to 'Infrastructure'. "
        "   ALWAYS route all service operations (create service, list services, describe service, patch service) to 'Infrastructure'. "
        "   CRITICAL — Namespace operations (list namespaces, create namespace, delete namespace, describe namespace) → 'Infrastructure'. "
        "   For 'service has no backends / traffic cannot reach app' investigations: route to 'Infrastructure' to check endpoints. "
        "   If endpoints show pods in 'NotReady' state, ALSO route to 'Lifecycle' to describe the pod/deployment "
        "   and check the readiness probe configuration — the root cause is likely a probe port or path mismatch. "
        "   After Lifecycle identifies the probe misconfiguration, FINISH with a fix recommendation. "
        "8h. **For Service selector updates or blue-green traffic switching** → delegate to 'Infrastructure' (has patch_service_selector). "
        "   Do NOT generate new code for selector changes. "
        "8i. **DynamicToolsExecutor handles ONLY custom gen_* tools**: "
        "   Route to 'DynamicToolsExecutor' when: "
        "   (a) The system context lists a [Registered custom tools] section AND a tool there matches the user's request — route directly to DynamicToolsExecutor WITHOUT going to CodeGenerator first. "
        "   (b) CodeGenerator just finished creating a new tool and it needs to be executed. "
        "   (c) The user explicitly refers to a custom tool by its gen_* name. "
        "   Do NOT route any standard Kubernetes operation to DynamicToolsExecutor. "
        "9. If any worker reports a MISSING tool or capability: "
        "   a. FIRST check [Registered custom tools] in system context — if a matching tool is listed, route to DynamicToolsExecutor instead. "
        "   b. Only if no registered tool matches: delegate to 'CodeGenerator' with a clear description of the needed tool. "
        "   c. If code generation is not suitable, ask the user for clarification. "
        "   d. If nothing works, explain why and select FINISH. "
        "   NOTE: DynamicToolsExecutor has ONLY dynamically generated 'gen_' tools from PVC. "
        "   Standard Kubernetes operations are not available there — route those to the appropriate specialist. "
        "10. **Multi-step and investigation queries** (HEALTH CHECK, SECURITY AUDIT, INCIDENT INVESTIGATION, 'find X then show Y then check Z'): "
        "   ORCHESTRATE across specialized agents — do NOT send directly to CodeGenerator. "
        "   Identify distinct sub-tasks and route each to the appropriate specialist. "
        "   Only escalate to 'CodeGenerator' if a sub-task fails because a tool is genuinely missing. "
        "10b. **DiagnosticsOrchestrator — multi-signal parallel debugging**: "
        "   Route to 'DiagnosticsOrchestrator' ONLY when the query requires SIMULTANEOUSLY collecting "
        "   logs, metrics, AND events with NO specific symptom known upfront — i.e., a general "
        "   'something is wrong, check everything' request (e.g. 'check everything for namespace X', "
        "   'full diagnosis of namespace X', 'investigate the entire stack', 'incident RCA'). "
        "   DO NOT route to DiagnosticsOrchestrator when: "
        "   (a) the query already describes a specific symptom (CrashLoopBackOff, OOMKilled, "
        "       ImagePullBackOff, Pending, probe failure, RBAC denied, PVC unbound, etc.), "
        "   (b) the query asks about a single pod ('a pod is crashing', 'pod X is failing'), "
        "   (c) the query is about a single resource type (service, configmap, rbac, pvc). "
        "   For specific symptoms → route to the specialist agent (Logs, RBAC, Infrastructure, etc.). "
        "   DiagnosticsOrchestrator runs Logs + Metrics + Events in parallel and returns a structured "
        "   multi-signal report. "
        "   **CRITICAL — Post-DiagnosticsOrchestrator follow-up rules (apply AFTER the triage report):** "
        "   (i) If the triage report shows ANY pod in CrashLoopBackOff → MUST route to 'Logs' next "
        "       to fetch the crash logs for those specific pods and identify the root cause (error message). "
        "       Do NOT FINISH after triage when CrashLoopBackOff is present. "
        "   (ia) If the triage report shows ANY pod in ImagePullBackOff or ErrImagePull → MUST route to 'Logs' "
        "       next to describe the pod and retrieve the exact image pull error message and image name. "
        "       Do NOT FINISH after triage when ImagePullBackOff is present. "
        "   (ii) If the triage report shows ANY pod in Pending state AND the cause is not yet clear → "
        "       route to 'Infrastructure' to check node allocatable capacity. "
        "   (iii) After Logs investigates CrashLoopBackOff pods, ALSO route to 'Infrastructure' "
        "       to check service endpoints for ALL Running pods in the namespace — "
        "       service selector mismatches are silent failures (pod Running, 0 endpoints) "
        "       not detectable without an explicit endpoint check. "
        "   (iv) MANDATORY for incident RCA queries: after Logs investigation, ALWAYS route to "
        "       'Infrastructure' to list_services and check service endpoints in the namespace "
        "       before concluding. This catches silent network failures (selector mismatches, "
        "       port mismatches, missing endpoints). "
        "   (v) Only FINISH after DiagnosticsOrchestrator when: Logs has investigated crashes/image pulls, "
        "       AND Infrastructure has verified service endpoints (for RCA queries), AND all fault root causes are named. "
        "   Example incident RCA path: DiagnosticsOrchestrator (triage) → Logs (crash details) → "
        "   Infrastructure (service endpoints) → FINISH with full prioritized RCA. "
        "11. **Task Completion Detection**: "
        "   a. For simple queries: if the worker provided the requested information, IMMEDIATELY select FINISH. "
        "   b. For complex queries: only delegate further if additional information is clearly needed. "
        "   c. If the same worker responds with the same content multiple times, IMMEDIATELY select FINISH. "
        "   d. If the original request has been fully answered, select FINISH immediately. "
        "12. Avoid re-delegating to the same worker for the same failed action without a change in approach. "
        "13. If all approaches fail, explain why and select FINISH. "
        "14. **If a worker reports the query is out of scope, immediately select FINISH with a polite message.** "
        "15. **No repeat clarifications**: If the user already answered a question (e.g., 'Which namespace?'), do NOT ask again. "
        "Always provide a brief reasoning trail for your routing decision. "
        "16. **DEFAULT — when in doubt, identify the most relevant specialist**: "
        "   Workload ops → 'Lifecycle'; pod info/logs → 'Logs'; networking/storage/services → 'Infrastructure'; "
        "   ConfigMap/Secret → 'ConfigMapsSecrets'; RBAC → 'RBAC'; security → 'Security'; exec/attach → 'Execution'; "
        "   delete/cleanup → 'Deletion'; apply manifests → 'Apply'. "
        "   Route to 'DynamicToolsExecutor' ONLY when the request involves a custom gen_* tool. "
        "   Only select FINISH when: (a) a worker already provided a complete answer, "
        "   (b) the query is unambiguously out of scope, or "
        "   (c) it is a capability/planning/next-step question handled inline."
    )

    return ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        MessagesPlaceholder(variable_name="messages"),
        ("system", "Given the conversation, who should act next? Or should we FINISH? Select one of: {options}"),
    ]).partial(
        options=str(SUPERVISOR_OPTIONS),
        members=", ".join(AGENT_MEMBERS)
    )


def create_supervisor_chain(llm, tool_categories: dict | None = None):
    """Create the supervisor decision chain.

    Args:
        llm: The language model to use for routing decisions.
        tool_categories: Optional tool categories dict. When provided, an auto-generated
            capability manifest is injected into the supervisor prompt.
    """
    try:
        prompt = create_supervisor_prompt(tool_categories)
        return prompt | llm.with_structured_output(SupervisorRoute, method="function_calling")
    except Exception as e:
        logger.critical(f"Failed to create supervisor chain: {e}", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Supervisor router node
# ---------------------------------------------------------------------------

def supervisor_router_node_func(state: KubeIntellectState, supervisor_chain) -> Dict[str, str]:
    """
    Supervisor router node function.

    Args:
        state: Current workflow state
        supervisor_chain: The supervisor decision chain

    Returns:
        Dictionary with next step decision
    """
    # Short-circuit: DynamicToolsExecutor has just executed a tool that was
    # freshly created/confirmed this turn.  The task is complete — FINISH immediately
    # rather than letting the supervisor re-route to CodeGenerator again.
    if state.get("dynamic_executor_ran_after_creation"):
        logger.info("DynamicToolsExecutor completed post-creation task — forcing FINISH.")
        return {"next": "FINISH", "dynamic_executor_ran_after_creation": False}

    # Check if a tool was just created and handle accordingly
    if state["messages"]:
        last_message = state["messages"][-1]
        if (hasattr(last_message, "additional_kwargs") and
                last_message.additional_kwargs.get("tool_just_created", False)):
            logger.info("Detected tool_just_created — routing to DynamicToolsExecutor to execute the new tool immediately.")

            # The tool was reloaded into DynamicToolsExecutor at registration time.
            # Extract the original user request so DynamicToolsExecutor has an explicit directive.
            # Use reversed() to get the MOST RECENT HumanMessage, not the first one in the
            # conversation — the conversation may contain many prior turns.
            from langchain_core.messages import HumanMessage as _HumanMessage
            original_query = next(
                (msg.content for msg in reversed(state["messages"]) if isinstance(msg, _HumanMessage)),
                None,
            )

            if original_query:
                # Inject a system bridge message so DynamicToolsExecutor knows exactly what to do.
                # Return as a state delta (not a mutation) so the operator.add reducer persists it.
                from langchain_core.messages import SystemMessage as _SystemMessage
                bridge_msg = _SystemMessage(
                    content=(
                        f"The new tool has been created and loaded. "
                        f"Use it now to complete the original request: {original_query}"
                    ),
                )
                logger.info(f"Routing to DynamicToolsExecutor with original query: {original_query[:120]}")
                return {
                    "messages": [bridge_msg],
                    "next": "DynamicToolsExecutor",
                    "supervisor_cycles": state.get("supervisor_cycles", 0) + 1,
                    "dynamic_executor_ran_after_creation": True,
                }
            else:
                # Fallback: no original query found — finish gracefully.
                logger.warning("tool_just_created but no original HumanMessage in state — FINISHing.")
                from langchain_core.messages import SystemMessage as _SystemMessage
                return {
                    "messages": [_SystemMessage(content="Tool created and registered. Please re-issue your request to use it.")],
                    "next": "FINISH",
                }

        # Short-circuit: deterministic 4xx tool error — retrying is pointless.
        # The worker node sets last_tool_error when a ToolMessage carries a 4xx
        # HTTP status.  We FINISH here rather than letting the LLM supervisor
        # re-dispatch the same agent with the same parameters.
        _last_tool_error = state.get("last_tool_error")
        if _last_tool_error and 400 <= _last_tool_error.get("http_status", 0) < 500:
            _http_status = _last_tool_error["http_status"]
            _err_agent = _last_tool_error.get("agent", "unknown")
            logger.warning(
                "Supervisor routing → FINISH [4xx-tool-error] agent=%s http_status=%d "
                "— deterministic error, will not retry.",
                _err_agent,
                _http_status,
            )
            # Defence-in-depth: synthesise a user-facing message for 404 errors from
            # Lifecycle so the user is never left with silence when a resource (e.g. a
            # namespace) does not exist.  Other 4xx codes (403, 409, 422) already carry
            # structured error dicts from the tool layer that the agent relays.
            _extra_messages = []
            if _http_status == 404 and _err_agent in ("Lifecycle",):
                _extra_messages = [
                    SystemMessage(
                        content=(
                            "The operation could not be completed because a required resource "
                            "was not found (404). Check that the namespace or resource name is "
                            "correct and exists in the cluster, then try again."
                        )
                    )
                ]
            # If a plan is active, exhaust it so the next invocation doesn't attempt
            # to continue a plan that has already failed deterministically.
            _plan_exhaust: Dict[str, Any] = {}
            _active_plan_4xx = state.get("plan") or []
            _active_plan_step_4xx = state.get("plan_step", 0)
            if _active_plan_4xx and _active_plan_step_4xx < len(_active_plan_4xx):
                _exec_state_4xx = {
                    **(state.get("plan_execution_state") or {}),
                    "failure_step": _active_plan_step_4xx,
                }
                _plan_exhaust = {
                    "plan_step": len(_active_plan_4xx),
                    "plan_execution_state": _exec_state_4xx,
                }
            return {
                "messages": _extra_messages,
                "next": "FINISH",
                "supervisor_cycles": state.get("supervisor_cycles", 0) + 1,
                "last_tool_error": None,  # clear so the next user turn starts clean
                **_plan_exhaust,
            }

        # Check for duplicate responses to prevent infinite loops
        # If the last 3 messages from the same worker contain very similar content, finish
        if len(state["messages"]) >= 3:
            recent_messages = state["messages"][-3:]
            worker_messages = [
                msg for msg in recent_messages
                if isinstance(msg, AIMessage) and hasattr(msg, "name") and msg.name
            ]

            if len(worker_messages) >= 2:
                # Check if we have multiple messages from the same worker with similar content
                last_worker = worker_messages[-1].name if worker_messages[-1].name else None
                if last_worker and last_worker != "Supervisor":
                    # Count how many times this worker has responded recently
                    same_worker_count = sum(1 for msg in worker_messages if msg.name == last_worker)

                    # If the same worker has responded 2+ times in the last 3 messages, check for duplicate content
                    if same_worker_count >= 2:
                        last_content = str(worker_messages[-1].content)[:200] if worker_messages[-1].content else ""
                        prev_content = (
                            str(worker_messages[-2].content)[:200]
                            if len(worker_messages) >= 2 and worker_messages[-2].content
                            else ""
                        )

                        # If contents are very similar (same start), it's likely a duplicate response
                        if last_content and prev_content and last_content[:100] == prev_content[:100]:
                            logger.warning(
                                f"Detected duplicate response from {last_worker}. Forcing FINISH to prevent infinite loop."
                            )
                            return {"next": "FINISH"}

        # Check supervisor cycle count to prevent infinite loops
        supervisor_cycles = state.get("supervisor_cycles", 0)
        if supervisor_cycles >= 10:  # Safety limit - force finish after 10 supervisor cycles
            logger.warning(f"Supervisor has cycled {supervisor_cycles} times. Forcing FINISH to prevent infinite loop.")
            return {"next": "FINISH"}

        # Check task_complete flag set by the last worker node.
        # Workers evaluate completion heuristics on their own output and surface the
        # result in state — keeping this logic out of the supervisor's routing prompt.
        # IMPORTANT: do NOT force FINISH if a committed plan still has remaining steps.
        # A plan step completing successfully sets task_complete=True for its sub-task,
        # but the plan must continue to the next step.
        _active_plan_for_tc = state.get("plan") or []
        _plan_step_for_tc = state.get("plan_step", 0)
        _plan_has_more_steps = bool(_active_plan_for_tc) and _plan_step_for_tc < len(_active_plan_for_tc)
        # For DiagnosticsOrchestrator-led incident investigations, multiple follow-up
        # agents are expected (Logs for crash details, Infrastructure for endpoint checks).
        # Require higher cycle threshold before forcing FINISH so all faults get investigated.
        _steps_taken = state.get("steps_taken") or []
        _diagnostics_ran = any("DiagnosticsOrchestrator" in s for s in _steps_taken)
        _infrastructure_ran = any(s.startswith("Infrastructure") for s in _steps_taken)
        _logs_ran = any(s.startswith("Logs") for s in _steps_taken)
        _min_cycles_for_finish = 3 if _diagnostics_ran else 1
        # For DiagnosticsOrchestrator incident investigations: ensure Infrastructure
        # has run to check service endpoints before the LLM decides to conclude.
        # Service selector mismatches are silent (pod Running, 0 endpoints) — not
        # visible without an explicit endpoint check. This guard fires independently
        # of task_complete so it catches both True and False cases.
        # Skip Infrastructure when events already show an image-pull or scheduling fault
        # (endpoint checks are irrelevant if pods never started Running).
        _events_str = str(state.get("diagnostics_events_result") or "").lower()
        _skip_infra_for_image_fault = (
            "imagepullbackoff" in _events_str
            or "errimagepull" in _events_str
            or "failed to pull image" in _events_str
        )
        if (
            _diagnostics_ran
            and not _infrastructure_ran
            and _logs_ran
            and state.get("supervisor_cycles", 0) >= 2
            and not _plan_has_more_steps
            and not _skip_infra_for_image_fault
        ):
            logger.info(
                "Post-DiagnosticsOrchestrator guard: routing to Infrastructure to check "
                "service endpoints (Infrastructure hasn't run, Logs has run, cycles=%d).",
                state.get("supervisor_cycles", 0),
            )
            _seen_for_guard = list(state.get("seen_dispatches") or [])
            if "Infrastructure" not in _seen_for_guard:
                _seen_for_guard.append("Infrastructure")
            _diag_ns = "default"
            for _s in _steps_taken:
                if "DiagnosticsOrchestrator" in _s and "namespace=" in _s:
                    import re as _re_guard
                    _ns_m = _re_guard.search(r"namespace=(\S+)", _s)
                    if _ns_m:
                        _diag_ns = _ns_m.group(1).rstrip(")")
                        break
            _infra_task = (
                f"[INCIDENT RCA - Service Endpoint Check] "
                f"List ALL services in namespace '{_diag_ns}' using list_services_in_namespace. "
                f"For EACH service found, check its endpoint count using get_service_endpoints. "
                f"Report any service with 0 endpoints — this indicates a selector mismatch or "
                f"connectivity fault. Show the service selector vs. the labels on its target pods."
            )
            return {
                "messages": [SystemMessage(content=_infra_task)],
                "next": "Infrastructure",
                "supervisor_cycles": state.get("supervisor_cycles", 0) + 1,
                "seen_dispatches": _seen_for_guard,
            }
        if (
            state.get("task_complete")
            and state.get("supervisor_cycles", 0) >= _min_cycles_for_finish
            and not _plan_has_more_steps
        ):
            logger.info(
                "task_complete=True after %d supervisor cycle(s) — forcing FINISH.",
                state.get("supervisor_cycles", 0),
            )
            return {"next": "FINISH"}

        # Short-circuit: Deletion agent issued a confirmation prompt — must await user reply.
        # The Deletion prompt format ("Reply **confirm** to proceed or **cancel** to abort")
        # contains no "?" so the generic clarification guard below does not catch it.
        _last_msg = state["messages"][-1]
        if (
            isinstance(_last_msg, AIMessage)
            and _last_msg.content
            and getattr(_last_msg, "name", None) == "Deletion"
        ):
            _lc = str(_last_msg.content).lower()
            if "confirm" in _lc and ("cancel" in _lc or "abort" in _lc):
                logger.info(
                    "Supervisor routing → FINISH [deletion-confirmation-pending] "
                    "Deletion agent issued confirmation prompt — awaiting user reply."
                )
                return {
                    "next": "FINISH",
                    "supervisor_cycles": state.get("supervisor_cycles", 0) + 1,
                }

    # Fast-path: dry-run confirmation — route back to the same agent with dry_run=False.
    # Conditions: last HumanMessage is a short confirmation phrase AND
    # the most recent named worker AIMessage contains a dry-run output marker.
    if state.get("messages"):
        _CONFIRM_WORDS = frozenset([
            "yes", "confirm", "proceed", "apply", "do it",
            "go ahead", "sure", "ok", "yep", "yeah",
        ])
        _DRY_RUN_MARKERS = [
            "[dry run]", "dry_run=false to apply",
            "call with dry_run=false", "re-run with dry_run=false",
            "dry run preview",
        ]
        from langchain_core.messages import HumanMessage as _HumanMessage
        _last_human_dry = next(
            (m for m in reversed(state["messages"]) if isinstance(m, _HumanMessage) and m.content),
            None,
        )
        if _last_human_dry:
            _human_text = str(_last_human_dry.content).strip().lower()
            _is_confirmation = (
                len(_human_text.split()) <= 8
                and any(w in _human_text for w in _CONFIRM_WORDS)
            )
            if _is_confirmation:
                _drrun_agent = next(
                    (
                        msg for msg in reversed(state["messages"])
                        if isinstance(msg, AIMessage)
                        and getattr(msg, "name", None)
                        and msg.name not in ("Supervisor", "System", "SystemError", "KubeIntellect")
                        and msg.content
                        and any(m in str(msg.content).lower() for m in _DRY_RUN_MARKERS)
                    ),
                    None,
                )
                if _drrun_agent:
                    logger.info(
                        "Supervisor routing → %s [dry-run-confirmation] user confirmed, "
                        "re-routing to same agent with dry_run=False.",
                        _drrun_agent.name,
                    )
                    return {
                        "next": _drrun_agent.name,
                        "supervisor_cycles": state.get("supervisor_cycles", 0) + 1,
                    }

    # Guard: do not re-ask a question the user has already answered.
    # Scan the last SHORT_TERM_MEMORY_WINDOW * 2 messages for the pattern
    # "AI asks Q → Human answers → AI asks same Q again".
    messages_list = state.get("messages", [])
    if len(messages_list) >= 3:
        from langchain_core.messages import HumanMessage as _HumanMessage
        recent = messages_list[-min(len(messages_list), 10):]
        # Walk backward: if we see AI-question → Human-answer → AI-question with same keyword, force FINISH
        ai_questions = [
            msg for msg in recent
            if isinstance(msg, AIMessage)
            and msg.content
            and any(kw in msg.content.lower() for kw in [
                "which namespace", "please specify", "could you provide",
                "please provide", "what namespace", "which pod", "which deployment",
            ])
        ]
        human_replies = [
            msg for msg in recent
            if isinstance(msg, _HumanMessage) and msg.content
        ]
        # If there are AI questions AND human replies AND recent AI message is again a question
        # while the latest human message appears AFTER the first AI question — the user already answered.
        if len(ai_questions) >= 2 and human_replies:
            # Find position of last AI question and latest human reply.
            # True loop = the agent asked AGAIN *after* the user's most recent reply.
            # Using first_q_idx > last_human_idx would fire on stale resolved questions.
            last_q_idx = next(
                (i for i, m in enumerate(recent) if m is ai_questions[-1]), None
            )
            last_human_idx = max(
                (i for i, m in enumerate(recent) if isinstance(m, _HumanMessage) and m.content),
                default=None,
            )
            if last_q_idx is not None and last_human_idx is not None and last_q_idx > last_human_idx:
                logger.warning(
                    "Supervisor routing → FINISH [clarification-loop] agent re-asked a question "
                    "the user already answered — forcing FINISH."
                )
                from langchain_core.messages import AIMessage as _AIMessage
                return {
                    "next": "FINISH",
                    "supervisor_cycles": state.get("supervisor_cycles", 0) + 1,
                    "messages": [_AIMessage(
                        content="I already have that information — let me proceed with your request.",
                        name="KubeIntellect",
                    )],
                }

    # Increment supervisor cycle counter
    current_cycles = state.get("supervisor_cycles", 0)

    # Deterministic plan execution — skip LLM if a committed plan step is pending.
    _plan = state.get("plan") or []
    _plan_step = state.get("plan_step", 0)
    if _plan and _plan_step < len(_plan):
        _next_planned = _plan[_plan_step]
        if _next_planned in AGENT_MEMBERS:
            # Abort plan on tool error or if the last worker asked for MANDATORY clarification.
            # Use the agent's tool-call trace as the completion signal: if the worker called
            # at least one tool, it performed real Kubernetes API work — any trailing "?" is
            # a polite offer (non-blocking). If it called zero tools AND has "?", it couldn't
            # start its work without mandatory input from the user (blocking clarification).
            # This is more robust than keyword matching: it uses the agent's actual behavior.
            _last_msg_for_plan = state["messages"][-1] if state.get("messages") else None
            _is_clarification = False
            if (
                _last_msg_for_plan
                and isinstance(_last_msg_for_plan, AIMessage)
                and "?" in str(_last_msg_for_plan.content)
            ):
                # tool_calls_made was set by the worker node just before the supervisor was called.
                _did_real_work = (state.get("tool_calls_made") or 0) > 0
                # Only abort if the agent did NO real work AND is asking a question.
                _is_clarification = not _did_real_work
            _abort = bool(state.get("last_tool_error")) or _is_clarification
            if _abort:
                _exec_state_abort = state.get("plan_execution_state") or {}
                _failure_step = _plan_step
                _exec_state_abort = {
                    **_exec_state_abort,
                    "failure_step": _failure_step,
                }
                logger.info(
                    "Plan aborted at step %d/%d (tool error or clarification) — "
                    "invalidating plan, resuming LLM routing.",
                    _plan_step, len(_plan),
                )
                # Set plan_step past the end so the fast-path never fires again;
                # hand full control back to the LLM for error recovery.
                return {
                    "next": "FINISH",  # let LLM routing take over after state update
                    "plan_step": len(_plan),
                    "plan_execution_state": _exec_state_abort,
                    "supervisor_cycles": current_cycles + 1,
                }

            # --- input_spec resolution ---
            # If the current step has an input_spec in the structured task_plan, resolve
            # the referenced fields from state.agent_results and inject a context message.
            _inject_messages = []
            _task_plan_dict = state.get("task_plan")
            if _task_plan_dict:
                try:
                    _tp = TaskPlan.model_validate(_task_plan_dict)
                    # _plan_step is 1-based position of the next step; current executing step
                    # is _plan_step (0-based index in steps list).
                    if _plan_step < len(_tp.steps):
                        _cur_step: SequentialStep = _tp.steps[_plan_step]

                        # Always inject the step task description so the agent knows
                        # exactly what to do in this step (and what NOT to do).
                        _step_task = getattr(_cur_step, "task", "") or ""
                        _step_header = (
                            f"[PLAN STEP {_plan_step + 1}/{len(_tp.steps)}] "
                            f"Your task for this step ONLY: {_step_task}. "
                            f"Complete the task, report the result, and STOP. "
                            f"Do NOT list global resources (all namespaces) or probe unrelated services. "
                            f"Do NOT perform actions that belong to other steps. "
                            f"In particular, do NOT call any delete/remove/cleanup tools unless "
                            f"this step explicitly says 'delete' or 'cleanup'."
                        )
                        _inject_messages = [SystemMessage(content=_step_header)]
                        logger.info(
                            "Plan step %d: injecting step task → %s",
                            _plan_step + 1, _step_task[:200],
                        )

                        _input_spec = _cur_step.input_spec or {}
                        if _input_spec:
                            _agent_results = state.get("agent_results") or {}
                            _resolved: dict = {}
                            for _key, _ref in _input_spec.items():
                                _parts = _ref.split(".", 1)
                                if len(_parts) == 2:
                                    _src_agent, _src_field = _parts
                                    _src_result = _agent_results.get(_src_agent) or {}
                                    _value = _src_result.get(_src_field)
                                    if _value is not None:
                                        _resolved[_key] = str(_value)
                            if _resolved:
                                _ctx_parts = ", ".join(f"{k}={v}" for k, v in _resolved.items())
                                _inject_messages.append(
                                    SystemMessage(
                                        content=(
                                            f"[Plan context for this step — resolved from prior results] "
                                            f"{_ctx_parts}"
                                        )
                                    )
                                )
                                logger.info(
                                    "Plan step %d: injecting input_spec context → %s",
                                    _plan_step, _ctx_parts[:200],
                                )
                except Exception as _ispec_exc:
                    logger.debug("input_spec resolution skipped: %s", _ispec_exc)

            # Update plan_execution_state.
            _exec_state_raw = state.get("plan_execution_state") or {}
            _completed = list(_exec_state_raw.get("completed_steps") or [])
            if _plan_step > 0 and _plan[_plan_step - 1] not in _completed:
                _completed.append(_plan[_plan_step - 1])
            _exec_state_upd = {
                "current_step": _plan_step + 1,
                "completed_steps": _completed,
                "failure_step": None,
            }

            logger.info(
                "Plan-step routing → %s (step %d/%d)",
                _next_planned, _plan_step + 1, len(_plan),
            )
            _step_result: Dict[str, Any] = {
                "next": _next_planned,
                "plan_step": _plan_step + 1,
                "plan_execution_state": _exec_state_upd,
                "supervisor_cycles": current_cycles + 1,
            }
            if _inject_messages:
                _step_result["messages"] = _inject_messages
            return _step_result

    # Inject reflection memory and steps_taken into the supervisor's message list.
    # Both are prepended as SystemMessages so they are always visible regardless
    # of the SHORT_TERM_MEMORY_WINDOW trim applied to conversation messages.
    # Trim to prevent context overflow in long multi-agent sessions:
    # Keep the first HumanMessage (original query) + last 4 AIMessages (recent agent work).
    from langchain_core.messages import HumanMessage as _HumanMessage, AIMessage as _AIMessage
    _all_msgs = list(state["messages"])
    _human_msgs = [m for m in _all_msgs if isinstance(m, _HumanMessage)]
    _ai_msgs = [m for m in _all_msgs if isinstance(m, _AIMessage)]
    _other_msgs = [m for m in _all_msgs if not isinstance(m, (_HumanMessage, _AIMessage))]
    # Keep first HumanMessage for query context + last 4 AIMessages for recent agent work
    messages_for_supervisor = _other_msgs + _human_msgs[:1] + _ai_msgs[-4:]
    if len(_ai_msgs) > 4:
        logger.debug(
            "Supervisor context trimmed: %d AI messages → last 4 (token budget protection).",
            len(_ai_msgs),
        )
    from langchain_core.messages import SystemMessage as _SystemMessage

    steps_taken = state.get("steps_taken") or []
    if steps_taken:
        steps_block = (
            "📋 Steps taken this session (do not repeat these — results already available):\n"
            + "\n".join(f"- {s}" for s in steps_taken)
        )
        messages_for_supervisor = [
            _SystemMessage(content=steps_block),
            *messages_for_supervisor,
        ]
        logger.debug("Injecting %d step(s) into supervisor context.", len(steps_taken))

    reflection_memory = state.get("reflection_memory") or []
    if reflection_memory:
        last_reflections = reflection_memory[-3:]  # inject at most the last 3
        reflection_context = (
            "⚠️ REFLECTION MEMORY (past mistakes to avoid this session):\n"
            + "\n".join(f"- {r}" for r in last_reflections)
        )
        messages_for_supervisor = [
            _SystemMessage(content=reflection_context),
            *messages_for_supervisor,
        ]
        logger.debug("Injecting %d reflection(s) into supervisor context.", len(last_reflections))

    # Otherwise proceed with normal supervisor routing
    route_result = supervisor_chain.invoke({"messages": messages_for_supervisor})
    decision = route_result.next

    # Hard guard: reject hallucinated agent names that somehow bypass Pydantic's Literal
    # constraint (e.g. degraded structured-output mode, partial parse).
    if decision not in SUPERVISOR_OPTIONS:
        logger.error(
            "Supervisor produced unregistered routing target '%s' — "
            "valid targets: %s — falling back to FINISH.",
            decision,
            SUPERVISOR_OPTIONS,
        )
        decision = "FINISH"

    # Schema-enforced clarification gate: if the LLM flagged that the last worker
    # message is a question directed at the user, override to FINISH immediately.
    # EXCEPTION: During post-DiagnosticsOrchestrator incident RCA, worker "offer" phrases
    # (e.g., "Would you like me to describe the pod?") are not blocking clarifications —
    # they're incomplete investigations. Route to Infrastructure to check service endpoints
    # instead of stopping the investigation prematurely.
    if getattr(route_result, "needs_human_input", False):
        _steps_for_hi = state.get("steps_taken") or []
        _diagnostics_ran_for_hi = any("DiagnosticsOrchestrator" in s for s in _steps_for_hi)
        _agents_that_ran = [s.split(":")[0].strip() for s in _steps_for_hi if ":" in s]
        _infrastructure_ran = "Infrastructure" in _agents_that_ran
        if _diagnostics_ran_for_hi and current_cycles < 5 and not _infrastructure_ran:
            # Override: route to Infrastructure to check service endpoints before concluding.
            logger.info(
                "Supervisor routing → Infrastructure (overriding needs_human_input=True "
                "during post-DiagnosticsOrchestrator RCA — checking service endpoints)."
            )
            _seen_for_hi = list(state.get("seen_dispatches") or [])
            if "Infrastructure" not in _seen_for_hi:
                _seen_for_hi.append("Infrastructure")
            return {
                "next": "Infrastructure",
                "supervisor_cycles": current_cycles + 1,
                "seen_dispatches": _seen_for_hi,
            }
        logger.info(
            "Supervisor routing → FINISH [needs_human_input=True] "
            "LLM detected worker is awaiting user reply."
        )
        return {
            "next": "FINISH",
            "supervisor_cycles": current_cycles + 1,
        }

    # If the LLM proposed a multi-step plan and no plan is currently active, commit it.
    # Prefer task_plan (structured with input_spec) over legacy plan (list of agent names).
    _active_plan = state.get("plan") or []
    _proposed_task_plan: Optional[TaskPlan] = getattr(route_result, "task_plan", None)
    _proposed_plan_names: Optional[list] = getattr(route_result, "plan", None)

    if not _active_plan:
        # --- Structured task_plan path (preferred) ---
        if _proposed_task_plan is not None:
            try:
                _valid_steps = [
                    s for s in _proposed_task_plan.steps if s.agent in AGENT_MEMBERS
                ]
                if len(_valid_steps) < 2:
                    logger.warning(
                        "Supervisor task_plan has fewer than 2 valid steps (%d) — "
                        "falling back to single-step routing.",
                        len(_valid_steps),
                    )
                elif len(_valid_steps) > 10:
                    logger.warning(
                        "Supervisor task_plan exceeds max_plan_steps=10 (%d steps) — "
                        "truncating to 10.",
                        len(_valid_steps),
                    )
                    _valid_steps = _valid_steps[:10]
                else:
                    _agent_names = [s.agent for s in _valid_steps]
                    _exec_state = PlanExecutionState(current_step=1).model_dump()
                    # Always use plan's first step as the routing target, not the LLM's
                    # `decision` field — LLM's `next` may disagree with task_plan.steps[0].
                    _first_step_agent = _valid_steps[0].agent
                    if _first_step_agent != decision:
                        logger.info(
                            "Supervisor task_plan first step (%s) overrides LLM next=%s — "
                            "using plan order.",
                            _first_step_agent, decision,
                        )
                    logger.info(
                        "Supervisor committed structured %d-step TaskPlan: %s — "
                        "starting at step 1 (routing to %s now). summary=%s",
                        len(_valid_steps), _agent_names, _first_step_agent,
                        _proposed_task_plan.query_summary or "(none)",
                    )
                    # Rebuild task_plan with only valid steps so stale agent names don't leak.
                    _clean_task_plan = TaskPlan(
                        steps=_valid_steps,
                        query_summary=_proposed_task_plan.query_summary,
                    )
                    return {
                        "next": _first_step_agent,
                        "plan": _agent_names,
                        "plan_step": 1,          # Step 0 is being executed now
                        "task_plan": _clean_task_plan.model_dump(),
                        "plan_execution_state": _exec_state,
                        "supervisor_cycles": current_cycles + 1,
                    }
            except Exception as _tp_exc:
                logger.warning(
                    "task_plan validation failed (%s) — falling back to single-step routing.",
                    _tp_exc,
                )

        # --- Legacy plan path (backward compat) ---
        if _proposed_plan_names:
            _valid_plan = [a for a in _proposed_plan_names if a in AGENT_MEMBERS]
            if len(_valid_plan) >= 2:
                _exec_state = PlanExecutionState(current_step=1).model_dump()
                _legacy_first = _valid_plan[0]
                logger.info(
                    "Supervisor committed %d-step legacy plan: %s — "
                    "starting at step 1 (routing to %s now).",
                    len(_valid_plan), _valid_plan, _legacy_first,
                )
                return {
                    "next": _legacy_first,
                    "plan": _valid_plan,
                    "plan_step": 1,
                    "plan_execution_state": _exec_state,
                    "supervisor_cycles": current_cycles + 1,
                }

    # Dispatch fingerprinting: block re-dispatch of the same agent for the same
    # user turn.  Fingerprint = "agent:hash(last_human_msg[:200]):steps_taken_count".
    # Including steps_taken count means the fingerprint changes after each successful
    # tool call, so a legitimate multi-step workflow that needs the same agent again
    # (with new state) is not blocked.  The set is implicitly cleared on every new
    # user turn because the human message hash changes.
    if decision not in ("FINISH",):
        from langchain_core.messages import HumanMessage as _HumanMessage
        _last_human = next(
            (m for m in reversed(state["messages"]) if isinstance(m, _HumanMessage) and m.content),
            None,
        )
        _human_hash = str(hash(str(_last_human.content)[:200])) if _last_human else "0"
        _steps_count = len(state.get("steps_taken") or [])
        _fingerprint = f"{decision}:{_human_hash}:{_steps_count}"
        _seen = list(state.get("seen_dispatches") or [])
        if _fingerprint in _seen:
            # Build a user-facing message when collision guard fires so the conversation
            # doesn't end silently.  Only inject if no worker has already replied.
            _worker_msgs = [
                m for m in state["messages"]
                if isinstance(m, AIMessage)
                and getattr(m, "name", None) not in (None, "Supervisor", "KubeIntellect", "System", "SystemError")
                and m.content
            ]
            _collision_extra = []
            if not _worker_msgs:
                _collision_extra = [AIMessage(
                    content=(
                        "I wasn't able to fully complete that request — the operation "
                        "encountered a routing loop. Please try rephrasing or breaking "
                        "it into smaller steps."
                    ),
                    name="KubeIntellect",
                )]
            logger.warning(
                "supervisor:dispatch_collision agent=%s fingerprint=%s cycle=%d steps=%d — "
                "already dispatched for this user turn, routing to FINISH.",
                decision, _fingerprint, current_cycles + 1, _steps_count,
            )
            return {
                "next": "FINISH",
                "supervisor_cycles": current_cycles + 1,
                "messages": _collision_extra,
            }
        _seen.append(_fingerprint)
    else:
        _seen = list(state.get("seen_dispatches") or [])

    logger.info(
        "Supervisor routing → %s (cycle=%d, messages=%d)",
        decision,
        current_cycles + 1,
        len(state["messages"]),
    )

    # Only warn when task_complete=True but the LLM still routes to an agent — that's an
    # actual guard miss.  task_complete=False is the normal working state and should not log.
    if state.get("task_complete") is True and decision not in ("FINISH",):
        logger.warning(
            "task_complete=True in state but supervisor still routing to %s — "
            "guard did not short-circuit (supervisor_cycles=%d). Check routing guard.",
            decision, current_cycles,
        )

    # If supervisor finishes immediately (no worker involved), inject a helpful response
    # so the user receives content instead of silence.
    if decision == "FINISH":
        worker_responses = [
            msg for msg in state["messages"]
            if isinstance(msg, AIMessage)
            and hasattr(msg, "name")
            and msg.name
            and msg.name not in ("Supervisor", "System", "SystemError")
            and msg.content
        ]
        if not worker_responses:
            from langchain_core.messages import SystemMessage as _SM, HumanMessage as _HM
            # Detect mid-conversation: a conversation summary was prepended (trimmed history),
            # or more than one human turn is present in the state window.
            _has_summary = any(
                isinstance(m, _SM) and m.content.startswith("[Conversation summary:")
                for m in state["messages"]
            )
            _human_count = sum(1 for m in state["messages"] if isinstance(m, _HM))
            if _has_summary or _human_count > 1:
                # Mid-conversation FINISH with no worker: the supervisor misrouted — it chose
                # FINISH before any agent answered the user's question.  Only auto-route to
                # DynamicToolsExecutor when the last human message has genuine Kubernetes intent.
                # Short/social/meta messages ("why did you...", "what did you mean") should pass
                # through to FINISH so the LLM can respond directly without a wasted agent round-trip.
                _K8S_KEYWORDS = frozenset([
                    "pod", "pods", "deploy", "deployment", "namespace", "service", "node",
                    "container", "cluster", "kubectl", "replica", "scale", "log", "logs",
                    "secret", "configmap", "ingress", "pvc", "volume", "rbac", "role",
                    "hpa", "autoscal", "cronjob", "job", "statefulset", "daemonset",
                    "event", "metric", "cpu", "memory", "image", "restart", "rollout",
                ])
                _last_human = next(
                    (m for m in reversed(state["messages"]) if isinstance(m, _HM) and m.content),
                    None,
                )
                _has_k8s_intent = bool(
                    _last_human
                    and len(_last_human.content.split()) > 3
                    and any(kw in _last_human.content.lower() for kw in _K8S_KEYWORDS)
                )
                if _has_k8s_intent:
                    logger.warning(
                        "Supervisor routing → DynamicToolsExecutor [catch-all] mid-conversation, "
                        "no worker response, K8s intent detected "
                        "(summary_present=%s, human_msgs=%d).",
                        _has_summary, _human_count,
                    )
                    return {
                        "next": "DynamicToolsExecutor",
                        "supervisor_cycles": current_cycles + 1,
                    }
                logger.info(
                    "Supervisor routing → FINISH [no-worker, no-k8s-intent] mid-conversation "
                    "(summary_present=%s, human_msgs=%d) — letting LLM reply directly.",
                    _has_summary, _human_count,
                )
            welcome = (
                "Hello! I'm **KubeIntellect**, your AI-powered Kubernetes operations assistant.\n\n"
                "I can help you with:\n"
                "- **Cluster overview** — list nodes, namespaces, pods, deployments, services\n"
                "- **Logs & events** — fetch pod/container logs, namespace events\n"
                "- **Metrics** — CPU and memory usage for pods and nodes\n"
                "- **Configuration** — inspect ConfigMaps, Secrets, deployment specs\n"
                "- **RBAC** — view roles, role bindings, service accounts\n"
                "- **Lifecycle** — scale, restart, rollout, cordon/drain nodes\n"
                "- **Security** — audit policies, PSP/PSA, vulnerability checks\n"
                "- **Networking & storage** — services, ingresses, PVs, PVCs\n"
                "- **Deletion** — safely remove resources\n"
                "- **Custom tools** — if a capability is missing, I can generate a new tool on the fly\n\n"
                "**Example queries:**\n"
                "- *\"List all pods in the default namespace\"*\n"
                "- *\"Show logs for pod my-app-7d9f in namespace production\"*\n"
                "- *\"What is the CPU usage of my nodes?\"*\n"
                "- *\"Scale deployment web to 3 replicas\"*\n"
                "- *\"Are there any failing pods in the cluster?\"*\n\n"
                "What would you like to do with your cluster?"
            )
            logger.info("Supervisor finished with no worker response — injecting welcome/help message.")
            return {
                "next": "FINISH",
                "supervisor_cycles": current_cycles + 1,
                "messages": [AIMessage(content=welcome, name="KubeIntellect")],
            }

    return {"next": decision, "supervisor_cycles": current_cycles + 1, "seen_dispatches": _seen}


# ---------------------------------------------------------------------------
# Clarification-loop guard
# ---------------------------------------------------------------------------

def check_clarification_loop(
    messages: Sequence[BaseMessage],
    max_clarifications: int = MAX_CLARIFICATIONS
) -> Tuple[bool, int]:
    """
    Check if we've had too many clarification requests in a row.

    Args:
        messages: Sequence of conversation messages
        max_clarifications: Maximum allowed consecutive clarifications

    Returns:
        Tuple of (is_loop_detected, clarification_count)
    """
    clarification_count = 0
    recent_messages = list(messages)[-10:]  # Check last 10 messages

    for msg in reversed(recent_messages):
        if isinstance(msg, AIMessage) and msg.content:
            content_lower = msg.content.lower()
            clarification_indicators = [
                "please specify", "which namespace", "could you provide",
                "missing parameter", "clarification", "please provide"
            ]

            if (any(indicator in content_lower for indicator in clarification_indicators) or
                    "?" in msg.content):
                clarification_count += 1
            else:
                break  # Stop counting if we hit a non-clarification message

    return clarification_count >= max_clarifications, clarification_count


# ---------------------------------------------------------------------------
# Worker node factory
# ---------------------------------------------------------------------------

def worker_node_factory(agent_runnable, agent_name: str):
    """
    Factory function to create worker node functions.

    Args:
        agent_runnable: The agent instance
        agent_name: Name of the agent

    Returns:
        Worker node function
    """
    def worker_node(state: KubeIntellectState) -> Dict[str, Any]:
        import time as _time
        _start = _time.monotonic()
        logger.info(
            f"Worker starting: {agent_name}",
            extra={"agent": agent_name, "message_count": len(state.get("messages", []))},
        )
        agent_invocations_total.labels(agent=agent_name).inc()
        agent_input = state

        # Enrich Langfuse spans for this worker with the agent name so the trace
        # tree shows which agent handled each step.
        from app.core.config import settings as _settings
        _agent_lf_ctx = _NullAgentCtx()
        if _settings.LANGFUSE_ENABLED:
            try:
                import langfuse as _lf
                _agent_lf_ctx = safe_otel_ctx(_lf.propagate_attributes(
                    metadata={"agent_name": agent_name},
                    tags=[agent_name],
                ))
            except Exception:
                pass

        with _agent_lf_ctx:
            # Support both proper LangChain runnables (.invoke) and plain callables
            # (returned by create_worker_agent when tools list is empty).
            if callable(agent_runnable) and not hasattr(agent_runnable, "invoke"):
                result = agent_runnable(agent_input)
            else:
                # Cap per-agent ReAct iterations to 16 (LangGraph default is 25).
                # Each agent tool call = 2 recursion steps (agent→tool→agent),
                # so limit=16 allows ~8 tool calls — enough for RBAC/complex investigations
                # while still catching runaway loops (scenario 09's 23-call loop cut at 8 calls).
                result = agent_runnable.invoke(
                    agent_input,
                    config={"recursion_limit": 16},
                )

        # B3: Intra-agent duplicate tool-call guard.
        # Scans the result messages for consecutive identical (tool_name, args_hash, output_hash)
        # triples. If 3+ identical triples appear in a row the agent is stuck in a loop —
        # inject a synthetic error so the supervisor can FINISH.
        # Guard does NOT fire if the output changes between calls (legitimate polling).
        _DUPLICATE_TOOL_CALL_THRESHOLD = 3
        if result and "messages" in result and result["messages"]:
            import hashlib as _hashlib
            import json as _json_b3
            # Map tool_call_id → (tool_name, args_hash) from AIMessage tool_calls.
            _tc_id_to_info: dict = {}
            for _m in result["messages"]:
                if isinstance(_m, AIMessage):
                    for _tc in (getattr(_m, "tool_calls", None) or []):
                        if isinstance(_tc, dict):
                            _tc_id = _tc.get("id") or _tc.get("tool_call_id")
                            if _tc_id:
                                _args_h = _hashlib.md5(
                                    _json_b3.dumps(_tc.get("args", {}), sort_keys=True).encode()
                                ).hexdigest()[:8]
                                _tc_id_to_info[_tc_id] = (_tc.get("name", ""), _args_h)
            # Build ordered list of (tool_name, args_hash, output_hash) from ToolMessages.
            _call_triples = []
            for _m in result["messages"]:
                if isinstance(_m, ToolMessage):
                    _out_h = _hashlib.md5(str(_m.content).encode()).hexdigest()[:8]
                    _tc_id = getattr(_m, "tool_call_id", None)
                    if _tc_id and _tc_id in _tc_id_to_info:
                        _tname, _ah = _tc_id_to_info[_tc_id]
                        _call_triples.append((_tname, _ah, _out_h))
            # Find max consecutive run of identical triples.
            if len(_call_triples) >= _DUPLICATE_TOOL_CALL_THRESHOLD:
                _max_run, _cur_run, _dup_tool = 1, 1, None
                for _i in range(1, len(_call_triples)):
                    if _call_triples[_i] == _call_triples[_i - 1]:
                        _cur_run += 1
                        if _cur_run > _max_run:
                            _max_run = _cur_run
                            _dup_tool = _call_triples[_i][0]
                    else:
                        _cur_run = 1
                if _max_run >= _DUPLICATE_TOOL_CALL_THRESHOLD:
                    tool_call_suppressed_total.labels(
                        agent=agent_name, tool_name=_dup_tool or "unknown"
                    ).inc()
                    logger.warning(
                        "Duplicate tool-call suppressed",
                        extra={
                            "agent": agent_name,
                            "tool_name": _dup_tool,
                            "run_length": _max_run,
                        },
                    )
                    result["messages"].append(
                        ToolMessage(
                            content=(
                                f"Error: duplicate tool call suppressed — '{_dup_tool}' was called "
                                f"{_max_run} times with identical arguments and identical results. "
                                "The agent appears to be stuck. Stop and report what you found so far, "
                                "or ask the user to rephrase the request."
                            ),
                            tool_call_id="suppressed",
                        )
                    )

        # Process agent response
        response_content = "Agent action completed."  # Default
        tool_just_created = False

        if result and "messages" in result and result["messages"]:
            last_message = result["messages"][-1]
            response_content = last_message.content

            # Check for tool creation (only for CodeGenerator).
            # The [TOOL_CREATED] marker is returned by the tool function itself, so it
            # lands in a ToolMessage — NOT in the agent's final AIMessage synthesis.
            # Scanning only the last message therefore misses the marker every time.
            # Scan all messages from this agent invocation instead.
            if agent_name == "CodeGenerator":
                all_content = " ".join(
                    str(getattr(msg, "content", "")) for msg in result["messages"]
                ).lower()
                if "[tool_created]" in all_content:
                    tool_just_created = True
                    logger.info("CodeGenerator signaled tool_just_created=True using marker (found in ToolMessage).")
                    # Strip the marker from the final user-facing response if present
                    response_content = response_content.replace("[TOOL_CREATED]", "").strip()

        _elapsed = _time.monotonic() - _start
        logger.info(
            f"Worker finished: {agent_name}",
            extra={
                "agent": agent_name,
                "agent_latency_s": round(_elapsed, 2),
                "tool_just_created": tool_just_created,
                "response_preview": response_content[:200],
            },
        )

        additional_kwargs = {"tool_just_created": tool_just_created} if tool_just_created else {}

        # Detect deterministic 4xx tool errors.  When a Kubernetes API call is
        # rejected with a 4xx status (e.g. 422 Unprocessable Entity), retrying
        # with identical parameters will always produce the same error.  We
        # surface this in state so the supervisor can FINISH immediately instead
        # of re-dispatching the same agent.
        last_tool_error = None
        if result and "messages" in result:
            for _msg in result["messages"]:
                if isinstance(_msg, ToolMessage) and getattr(_msg, "status", None) == "error":
                    _http_status = _extract_http_status(str(_msg.content))
                    if _http_status is not None and 400 <= _http_status < 500:
                        last_tool_error = {
                            "http_status": _http_status,
                            "agent": agent_name,
                            "message": str(_msg.content)[:500],
                        }
                        logger.warning(
                            "Deterministic tool error detected",
                            extra={"agent": agent_name, "http_status": _http_status},
                        )
                        break  # First 4xx is sufficient

        # Count tool calls made by this agent invocation.
        # A non-zero count means the agent executed real Kubernetes API work.
        # Used by the plan fast-path to distinguish "completed + polite offer"
        # from "blocking clarification with zero work done".
        _tool_calls_made = sum(
            1 for _m in (result.get("messages") or []) if isinstance(_m, ToolMessage)
        )

        # Detect whether this response fully answers the user's request.
        # Workers set task_complete=True so the supervisor can FINISH without
        # brittle string-matching on the AIMessage content.
        _COMPLETION_SIGNALS = frozenset([
            "here is", "here are", "here's", "list of", "found", "showing", "result",
            "completed", "successfully", "has been created", "has been deleted",
            "has been scaled", "has been restarted", "has been applied",
            "already exists",
            # Diagnostic completion phrases
            "root cause", "analysis:", "diagnosis:", "the issue is", "the problem is",
            "indicates that", "this indicates", "this confirms", "confirmed:",
            "the error", "oom-killed", "oomkilled", "crashloopbackoff",
            "detailed analysis", "findings:", "summary:", "evidence:",
            # Job/batch failure phrases
            "backofflimitexceeded", "job has failed", "job failed", "has reached the specified backoff",
            "exceeded quota", "quota exceeded", "forbidden:", "is forbidden",
        ])
        _ERROR_OR_CLARIF = frozenset([
            "i don't have", "cannot perform", "please specify", "could you provide",
            "clarification needed", "need clarification",
            # Partial investigation signals — task is not complete, follow-up required
            "readiness probe configuration needs", "probe configuration needs to be inspected",
            "requires further investigation", "needs to be inspected",
        ])
        # Blocking clarification phrases — agent needs mandatory input to proceed.
        # Intentionally excludes non-blocking offer phrases ("would you like",
        # "shall I also") which appear AFTER completed work and should not prevent
        # task_complete=True.
        _BLOCKING_CLARIF_PHRASES = [
            "which namespace", "what namespace",
            "which pod", "which deployment", "which service", "which node",
            "please specify", "could you specify", "could you provide",
            "please provide the", "what name", "which one",
        ]
        _cl = response_content.lower()
        _has_blocking_clarif = "?" in response_content and any(
            p in _cl for p in _BLOCKING_CLARIF_PHRASES
        )
        task_complete = (
            any(s in _cl for s in _COMPLETION_SIGNALS)
            and not any(s in _cl for s in _ERROR_OR_CLARIF)
            and not _has_blocking_clarif
        )

        # Build typed result and merge into the agent_results dict in state.
        # Using a shallow merge so results from earlier agents are preserved
        # and downstream agents can read them without re-parsing raw strings.
        typed_result = build_agent_result(agent_name, response_content)
        existing_results: dict = state.get("agent_results") or {}
        updated_results = {**existing_results, agent_name: typed_result.model_dump()}

        # Build a 1-line step summary for steps_taken.
        # Strip markdown symbols, collapse whitespace, cap at 140 chars.
        import re as _re
        _summary_raw = _re.sub(r'[*#`\[\]_~>]', '', response_content).strip()
        _summary_raw = ' '.join(_summary_raw.split())
        _truncated = len(_summary_raw) > 140
        _summary_text = _summary_raw[:140] + ("…" if _truncated else "")
        step_entry = f"{agent_name}: {_summary_text}"
        existing_steps: list = list(state.get("steps_taken") or [])
        updated_steps = (existing_steps + [step_entry])[-20:]  # cap at 20

        result_state: Dict[str, Any] = {
            "messages": [AIMessage(
                content=response_content,
                name=agent_name,
                additional_kwargs=additional_kwargs,
            )],
            "agent_results": updated_results,
            "task_complete": task_complete,
            "steps_taken": updated_steps,
            "tool_calls_made": _tool_calls_made,
        }
        if last_tool_error is not None:
            result_state["last_tool_error"] = last_tool_error

        return result_state

    return worker_node


def create_worker_nodes(worker_agents: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create all worker node functions.

    Args:
        worker_agents: Dictionary of worker agent instances

    Returns:
        Dictionary of worker node functions
    """
    nodes = {}

    # AgentDefinition.name now matches AGENT_MEMBERS exactly (PascalCase, no _agent suffix),
    # so worker_agents is keyed by the same name used for the LangGraph node.
    # No translation dict needed.
    for agent_name in AGENT_MEMBERS:
        if agent_name in CUSTOM_NODE_AGENTS:
            # Custom nodes are wired directly in create_workflow_graph — skip here.
            continue
        if agent_name in worker_agents and worker_agents[agent_name]:
            nodes[agent_name] = worker_node_factory(worker_agents[agent_name], agent_name)
        else:
            logger.error(f"Agent '{agent_name}' not found in worker_agents or is None")

    return nodes
