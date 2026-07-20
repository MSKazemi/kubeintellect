# app/services/conversation_context_service.py
"""
Persistent conversation working-context service.

Stores the current Kubernetes working context (namespace, resource name/type)
per conversation in PostgreSQL so that agents can recall it even after the
message window has been trimmed.

The extracted context is injected as a pinned SystemMessage at the start of
every request — before the message-window slice — so it is never evicted.

Table DDL (created automatically on first call to setup_schema):

    CREATE TABLE IF NOT EXISTS conversation_context (
        id              SERIAL PRIMARY KEY,
        conversation_id TEXT        NOT NULL UNIQUE,
        user_id         TEXT,
        context_json    JSONB       NOT NULL DEFAULT '{}',
        context_blob    JSONB       NOT NULL DEFAULT '{}',
        updated_at      TIMESTAMPTZ DEFAULT NOW()
    );

context_json: lightweight scalars — namespace, resource_name, resource_type, last_tool
context_blob: richer working context — last_action (str), confirmed_facts (List[str])
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from app.utils.logger_config import setup_logging

if TYPE_CHECKING:
    pass  # psycopg_pool.AsyncConnectionPool imported lazily

logger = setup_logging(app_name="kubeintellect")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS conversation_context ("
    "id              SERIAL PRIMARY KEY, "
    "conversation_id TEXT        NOT NULL UNIQUE, "
    "user_id         TEXT, "
    "context_json    JSONB       NOT NULL DEFAULT '{}', "
    "context_blob    JSONB       NOT NULL DEFAULT '{}', "
    "updated_at      TIMESTAMPTZ DEFAULT NOW()"
    ")"
)
_CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_conversation_context_conversation_id "
    "ON conversation_context (conversation_id)"
)
# Idempotent migration — adds context_blob if the table was created before this column existed.
_MIGRATE_ADD_BLOB_SQL = (
    "ALTER TABLE conversation_context "
    "ADD COLUMN IF NOT EXISTS context_blob JSONB NOT NULL DEFAULT '{}'"
)


async def setup_schema(pool) -> None:
    """Create the conversation_context table + index and run pending migrations."""
    try:
        async with pool.connection() as conn:
            await conn.execute(_CREATE_TABLE_SQL)
            await conn.execute(_CREATE_INDEX_SQL)
            await conn.execute(_MIGRATE_ADD_BLOB_SQL)
        logger.info("conversation_context schema verified/created.")
    except Exception as exc:
        logger.warning("Could not create conversation_context schema: %s", exc)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def load_context(conversation_id: str, pool) -> dict:
    """
    Return the stored working context for *conversation_id*.

    Merges context_json and context_blob into one dict.
    Returns an empty dict on miss or error.
    """
    if not conversation_id or not pool:
        return {}
    try:
        async with pool.connection() as conn:
            rows = await conn.execute(
                "SELECT context_json, context_blob FROM conversation_context "
                "WHERE conversation_id = %s",
                (conversation_id,),
            )
            row = await rows.fetchone()
            if row:
                base = row[0] if isinstance(row[0], dict) else {}
                blob = row[1] if isinstance(row[1], dict) else {}
                return {**base, **blob}
            return {}
    except Exception as exc:
        logger.debug(
            "Could not load context for conversation %s: %s", conversation_id, exc
        )
        return {}


# Fields stored in context_json (lightweight scalars for namespace pinning)
_JSON_FIELDS = frozenset(["namespace", "resource_name", "resource_type", "last_tool"])
# Fields stored in context_blob (richer investigation context)
# pending_deletion: str | None — text of the Deletion agent's confirmation prompt,
#   persisted when a deletion-confirmation-pending FINISH occurs so that graph state
#   can be wiped safely and the context restored on the next request (B5 fix).
_BLOB_FIELDS = frozenset(["last_action", "confirmed_facts", "pending_deletion"])


async def save_context(
    conversation_id: str, user_id: str | None, context: dict, pool
) -> bool:
    """
    Upsert the working context for *conversation_id*.

    Splits the context dict into context_json (scalars) and context_blob (enriched).
    Returns True on success, False on failure.
    """
    if not conversation_id or not context or not pool:
        return False
    try:
        context_json = {k: v for k, v in context.items() if k in _JSON_FIELDS}
        context_blob = {k: v for k, v in context.items() if k in _BLOB_FIELDS}
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO conversation_context
                    (conversation_id, user_id, context_json, context_blob, updated_at)
                VALUES (%s, %s, %s::jsonb, %s::jsonb, NOW())
                ON CONFLICT (conversation_id) DO UPDATE SET
                    user_id      = EXCLUDED.user_id,
                    context_json = EXCLUDED.context_json,
                    context_blob = EXCLUDED.context_blob,
                    updated_at   = NOW()
                """,
                (conversation_id, user_id,
                 json.dumps(context_json), json.dumps(context_blob)),
            )
        logger.debug(
            "Saved conversation context for %s: json=%s blob=%s",
            conversation_id, context_json, context_blob,
        )
        return True
    except Exception as exc:
        logger.warning(
            "Could not save context for conversation %s: %s", conversation_id, exc
        )
        return False


async def delete_context(conversation_id: str, pool) -> bool:
    """Delete all context for *conversation_id*.  Useful for GDPR erasure."""
    if not conversation_id or not pool:
        return False
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM conversation_context WHERE conversation_id = %s",
                (conversation_id,),
            )
        return True
    except Exception as exc:
        logger.warning(
            "Could not delete context for conversation %s: %s", conversation_id, exc
        )
        return False


# ---------------------------------------------------------------------------
# Entity extraction from LangGraph message history
# ---------------------------------------------------------------------------

# Maps tool argument field names → Kubernetes resource kind strings.
_RESOURCE_TYPE_FIELDS: dict[str, str] = {
    "pod_name":          "Pod",
    "deployment_name":   "Deployment",
    "service_name":      "Service",
    "statefulset_name":  "StatefulSet",
    "daemonset_name":    "DaemonSet",
    "replicaset_name":   "ReplicaSet",
    "job_name":          "Job",
    "cronjob_name":      "CronJob",
    "configmap_name":    "ConfigMap",
    "secret_name":       "Secret",
    "pvc_name":          "PersistentVolumeClaim",
    "ingress_name":      "Ingress",
    "hpa_name":          "HorizontalPodAutoscaler",
    "namespace_name":    "Namespace",
}

# Maps tool names to brief human-readable action descriptions.
_TOOL_ACTION_MAP: dict[str, str] = {
    "get_pod_logs":                   "fetched pod logs",
    "get_previous_pod_logs":          "fetched previous container logs",
    "scale_deployment":               "scaled deployment",
    "scale_statefulset":              "scaled statefulset",
    "rollout_restart":                "triggered rollout restart",
    "rollout_undo":                   "rolled back",
    "rollout_status":                 "checked rollout status",
    "rollout_history":                "listed rollout history",
    "rollout_pause":                  "paused rollout",
    "rollout_resume":                 "resumed rollout",
    "describe_resource":              "described resource",
    "top_pods":                       "fetched pod metrics",
    "top_nodes":                      "fetched node metrics",
    "events_watch":                   "fetched events",
    "set_env":                        "updated environment variables",
    "patch_resource":                 "patched resource",
    "label_resource":                 "updated labels",
    "annotate_resource":              "updated annotations",
    "create_kubernetes_deployment":   "created deployment",
    "create_statefulset":             "created statefulset",
    "create_kubernetes_namespace":    "created namespace",
    "delete_pod":                     "deleted pod",
    "delete_deployment":              "deleted deployment",
    "delete_statefulset":             "deleted statefulset",
    "delete_service":                 "deleted service",
    "apply_manifest":                 "applied manifest",
    "cordon_node":                    "cordoned node",
    "uncordon_node":                  "uncordoned node",
    "drain_node":                     "drained node",
    "create_service_for_deployment":  "created service",
}

# Patterns for extracting confirmed facts from AIMessage text.
# Each entry: (compiled regex, human-readable fact string)
_FACT_PATTERNS: list[tuple] = [
    (re.compile(r'\bOOMKill(?:ed)?\b', re.I),            "OOMKilled detected"),
    (re.compile(r'\bCrashLoopBackOff\b', re.I),          "CrashLoopBackOff detected"),
    (re.compile(r'\bImagePullBackOff\b', re.I),          "ImagePullBackOff detected"),
    (re.compile(r'\bpod[s]?\b.{0,30}\bPending\b', re.I), "pod is Pending"),
    (re.compile(r'\bPVC\b.{0,20}\bBound\b', re.I),       "PVC is Bound"),
    (re.compile(r'\bPVC\b.{0,20}\bPending\b', re.I),     "PVC is Pending"),
    (re.compile(r'deployment.{0,30}\bavailable\b', re.I), "deployment is available"),
    (re.compile(r'rollout.{0,20}\bcomplete\b', re.I),     "rollout is complete"),
    (re.compile(r'\bnode\b.{0,20}\bNotReady\b', re.I),   "node is NotReady"),
    (re.compile(r'\bnode\b.{0,20}\bReady\b', re.I),      "node is Ready"),
    (re.compile(r'\bEvicted\b', re.I),                    "pod evicted"),
    (re.compile(r'\bReadinessProbe\b.{0,30}\bfail', re.I), "readiness probe failing"),
    (re.compile(r'\bBackOff\b', re.I),                    "BackOff event present"),
    (re.compile(r'successfully\s+(?:created|scaled|restarted|applied)', re.I), "operation succeeded"),
]


def _extract_confirmed_facts(text: str) -> list[str]:
    """Scan AIMessage text for key Kubernetes status observations."""
    facts: list[str] = []
    seen: set[str] = set()
    for pattern, fact in _FACT_PATTERNS:
        if pattern.search(text) and fact not in seen:
            facts.append(fact)
            seen.add(fact)
    return facts


def extract_context_from_messages(messages: list) -> dict:
    """
    Scan a list of LangChain messages in reverse and extract the most recent
    Kubernetes working context.

    Extracts from tool-call arguments (deterministic):
        namespace, resource_name, resource_type, last_tool

    Derives from last_tool:
        last_action — human-readable description of the last operation

    Extracts from last AIMessage content (heuristic):
        confirmed_facts — list of key observations (OOMKilled, CrashLoopBackOff, etc.)

    Returns {} if no relevant context is found.
    """
    try:
        from langchain_core.messages import AIMessage
    except ImportError:
        return {}

    found_namespace: str | None = None
    found_resource_name: str | None = None
    found_resource_type: str | None = None
    found_tool: str | None = None
    last_ai_content: str = ""

    for msg in reversed(messages):
        # Capture the most recent AIMessage content for fact extraction.
        if isinstance(msg, AIMessage) and msg.content and not last_ai_content:
            last_ai_content = str(msg.content)

        if not isinstance(msg, AIMessage):
            continue
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in reversed(tool_calls):
            if not isinstance(tc, dict):
                continue
            args = tc.get("args", {}) or {}
            tool_name = tc.get("name", "")

            # --- Namespace extraction ---
            ns = args.get("namespace")
            if not ns:
                ns_list = args.get("namespaces")
                if isinstance(ns_list, list) and len(ns_list) == 1:
                    ns = ns_list[0]
            if not ns:
                ns = args.get("namespace_name")

            if ns and found_namespace is None:
                found_namespace = ns
                if args.get("namespace_name") and found_resource_name is None:
                    found_resource_name = ns
                    found_resource_type = "Namespace"
                    found_tool = tool_name

            # --- Resource name extraction ---
            if found_resource_name is None:
                for field, rtype in _RESOURCE_TYPE_FIELDS.items():
                    val = args.get(field)
                    if val and field != "namespace_name":
                        found_resource_name = val
                        found_resource_type = rtype
                        found_tool = tool_name
                        break

            # Short-circuit once we have namespace + resource
            if found_namespace and found_resource_name:
                break

        if found_namespace and found_resource_name:
            break

    result: dict = {}
    if found_namespace:
        result["namespace"] = found_namespace
    if found_resource_name:
        result["resource_name"] = found_resource_name
        result["resource_type"] = found_resource_type
    if found_tool:
        result["last_tool"] = found_tool
        result["last_action"] = _TOOL_ACTION_MAP.get(found_tool, found_tool.replace("_", " "))

    if last_ai_content:
        facts = _extract_confirmed_facts(last_ai_content)
        if facts:
            result["confirmed_facts"] = facts

    return result


# ---------------------------------------------------------------------------
# Formatting for injection
# ---------------------------------------------------------------------------

def format_context_pinned_message(context: dict) -> str:
    """
    Format the working context as a concise system message injected at the
    start of every request so agents never re-ask the user for known context.

    Returns an empty string if *context* is empty.
    """
    if not context:
        return ""

    parts: list[str] = []
    if "namespace" in context:
        parts.append(f"namespace={context['namespace']}")
    if "resource_name" in context:
        rtype = context.get("resource_type", "resource")
        parts.append(f"resource={context['resource_name']} ({rtype})")

    if not parts:
        return ""

    lines: list[str] = [f"[Working Kubernetes context: {', '.join(parts)}."]

    if "last_action" in context:
        lines.append(f"Last action: {context['last_action']}.")

    facts = context.get("confirmed_facts") or []
    if facts:
        lines.append(f"Confirmed: {'; '.join(facts)}.")

    lines.append("Use these values if the user does not specify otherwise.]")

    pending = context.get("pending_deletion")
    if pending:
        lines.append(
            f"[Pending deletion awaiting user confirmation: {pending} "
            "— if the user says 'confirm', 'yes', or 'proceed', execute the deletion now.]"
        )

    return " ".join(lines)
