# app/services/tool_output_summarizer.py
"""
Heuristic tool output summarizer — pure token counting and line slicing.

No LLM calls. Applied to tool outputs before they enter agent message state so
that large free-text payloads (pod logs, event dumps) do not consume excessive
context tokens. Structured identity / state fields always pass through intact.

Budget table (tokens, full output including JSON envelope):
    STRUCTURED_READ  300   get_pod_status, describe_resource, rollout_status, top_*
    EVENT            600   events_watch, list_namespace_events, warning events
    LOG            2_200   get_pod_logs — free-text log lines
    GENERIC          500   fallback for any unclassified tool
    LIST           2_000   list_* tools default — item-level truncation
    (per-tool)     4_000   list_all_pods_across_namespaces — covers ~100 pods

LOG-tool truncation strategy:
    1. Scan for the first exception/error anchor line.
    2. Always preserve: anchor line + ANCHOR_CONTEXT_LINES after it (traceback body).
    3. Fill remaining budget from the tail (most recent lines).
    4. Drop everything else, emitting explicit "[... N lines omitted ...]" markers.
    5. Result structure: [startup omission] + anchor block + [mid omission] + tail block.

All other tool types: truncate from the tail, keep the head.
"""

from __future__ import annotations

import copy
import json
import re
from enum import Enum
from typing import Optional

import tiktoken
from opentelemetry import trace

from app.core.config import settings
from app.utils.logger_config import setup_logging
from app.utils.metrics import list_output_truncated_total, tool_output_truncated_total

logger = setup_logging(app_name="kubeintellect")

# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

_ENCODER: Optional[tiktoken.Encoding] = None


def _get_encoder() -> tiktoken.Encoding:
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = tiktoken.encoding_for_model("gpt-4o")
    return _ENCODER


def _count_tokens(text: str) -> int:
    return len(_get_encoder().encode(text))


# ---------------------------------------------------------------------------
# Budget constants
# ---------------------------------------------------------------------------

BUDGET_STRUCTURED_READ: int = 300
BUDGET_EVENT:           int = 600
BUDGET_LOG:             int = 2_200
BUDGET_GENERIC:         int = 500
BUDGET_LIST:            int = 2_000
# Per-tool budget overrides — take precedence over BUDGET_BY_TYPE.
# Use for LIST tools whose item count routinely exceeds the type-level default.
# Ceiling is 4_000 (architectural max for retrieved cluster evidence per
# Principle 2 in CLAUDE.md — keeps total agent context under 8k tokens).
BUDGET_BY_TOOL: dict[str, int] = {
    "list_all_pods_across_namespaces": 4_000,  # up to ~100 pods (40 tok/pod × 100 = 4000)
}


# ---------------------------------------------------------------------------
# Tool type classification
# ---------------------------------------------------------------------------

class ToolType(Enum):
    STRUCTURED_READ = "structured_read"
    EVENT           = "event"
    LOG             = "log"
    GENERIC         = "generic"
    LIST            = "list"


BUDGET_BY_TYPE: dict[ToolType, int] = {
    ToolType.STRUCTURED_READ: BUDGET_STRUCTURED_READ,
    ToolType.EVENT:           BUDGET_EVENT,
    ToolType.LOG:             BUDGET_LOG,
    ToolType.GENERIC:         BUDGET_GENERIC,
    ToolType.LIST:            BUDGET_LIST,
}

# Resolved from tool name — never inferred from output content.
TOOL_TYPE_MAP: dict[str, ToolType] = {
    # ── Structured read ──────────────────────────────────────────────────────
    "get_pod_status":                   ToolType.STRUCTURED_READ,
    "describe_resource":                ToolType.STRUCTURED_READ,
    "rollout_status":                   ToolType.STRUCTURED_READ,
    "top_nodes":                        ToolType.STRUCTURED_READ,
    "top_pods":                         ToolType.STRUCTURED_READ,
    "get_deployment_status":            ToolType.STRUCTURED_READ,
    "describe_kubernetes_deployment":   ToolType.STRUCTURED_READ,
    "describe_kubernetes_statefulset":  ToolType.STRUCTURED_READ,
    "get_pod_diagnostics":              ToolType.STRUCTURED_READ,
    # ── Event ────────────────────────────────────────────────────────────────
    "events_watch":                     ToolType.EVENT,
    "list_namespace_events":            ToolType.EVENT,
    "get_namespace_warning_events":     ToolType.EVENT,
    "get_pod_events":                   ToolType.EVENT,
    # ── Log ──────────────────────────────────────────────────────────────────
    "get_pod_logs":                     ToolType.LOG,
    # ── List (item-level truncation, 800-token budget) ────────────────────────
    "list_all_pods_across_namespaces":          ToolType.LIST,
    "list_pods_in_namespace":                   ToolType.LIST,
    "list_error_pods":                          ToolType.LIST,
    "list_pods_with_two_containers":            ToolType.LIST,
    "list_pods_with_label":                     ToolType.LIST,
    "list_kubernetes_nodes":                    ToolType.LIST,
    "get_kubernetes_nodes_info":                ToolType.LIST,
    "list_all_deployments_across_namespaces":   ToolType.LIST,
    "list_deployments_in_namespace":            ToolType.LIST,
    "list_all_kubernetes_services":             ToolType.LIST,
    "list_services_in_namespace":               ToolType.LIST,
    "list_services_by_type":                    ToolType.LIST,
    "list_external_services":                   ToolType.LIST,
    "list_configmaps":                          ToolType.LIST,
    "list_kubernetes_namespaces":               ToolType.LIST,
}

# Human-readable resource type label used in truncation annotations and Prometheus labels.
# Maps LangChain tool name → singular resource noun (plural via +s suffix).
RESOURCE_TYPE_BY_TOOL: dict[str, str] = {
    "list_all_pods_across_namespaces":          "pod",
    "list_pods_in_namespace":                   "pod",
    "list_error_pods":                          "pod",
    "list_pods_with_two_containers":            "pod",
    "list_pods_with_label":                     "pod",
    "list_kubernetes_nodes":                    "node",
    "get_kubernetes_nodes_info":                "node",
    "list_all_deployments_across_namespaces":   "deployment",
    "list_deployments_in_namespace":            "deployment",
    "list_all_kubernetes_services":             "service",
    "list_services_in_namespace":               "service",
    "list_services_by_type":                    "service",
    "list_external_services":                   "service",
    "list_configmaps":                          "configmap",
    "list_kubernetes_namespaces":               "namespace",
}

# ---------------------------------------------------------------------------
# Free-text fields eligible for truncation.
# Structured identity / state fields are NEVER in this set — they bypass the
# budget entirely regardless of their size.
# ---------------------------------------------------------------------------

FREE_TEXT_FIELDS: frozenset[str] = frozenset({
    "logs",
    "message_text",
    "raw_output",
    "output",
    "events",   # raw event dump from list_namespace_events
    "describe", # raw describe text from non-structured tools
})

# ---------------------------------------------------------------------------
# Exception anchor patterns (configurable — add patterns here, no code change)
# ---------------------------------------------------------------------------

# Each entry is a regex pattern. The first line matching any pattern becomes
# the anchor. Lines from the anchor through ANCHOR_CONTEXT_LINES after it are
# always preserved regardless of token budget.
ANCHOR_PATTERNS: list[str] = [
    # Python
    r"Traceback \(most recent call last\)",
    r"\bError:",
    r"\bException:",
    # Java
    r"Exception in thread",
    r"(?i)caused by:",
    r"OutOfMemoryError",
    # Generic
    r"\bFATAL\b",
    r"\bpanic:",
    r"fatal error:",
]

# Number of lines after the anchor line to include in the preserved anchor block
# (covers the traceback body — file paths, line numbers, exception message).
ANCHOR_CONTEXT_LINES: int = 15

_COMPILED_ANCHORS: list[re.Pattern] = [re.compile(p) for p in ANCHOR_PATTERNS]


# ---------------------------------------------------------------------------
# ToolOutputSummarizer
# ---------------------------------------------------------------------------

class ToolOutputSummarizer:
    """
    Heuristic, LLM-free tool output summarizer.

    Usage::

        summarizer = ToolOutputSummarizer()
        truncated = summarizer.summarize("get_pod_logs", raw_tool_output)
    """

    def summarize(self, tool_name: str, raw_output: str) -> str:
        """
        Summarize *raw_output* for *tool_name*.

        Returns the original string unchanged when:
        - SUMMARIZE_TOOL_OUTPUTS is False (debug bypass)
        - raw_output is already within budget (fast path)

        LIST tools receive item-level truncation (whole array entries dropped)
        with a semantic "[Showing N of M ...]" annotation rather than a token-
        count footer. All other types use the existing text-level truncation.
        """
        if not settings.SUMMARIZE_TOOL_OUTPUTS:
            return raw_output

        tool_type  = TOOL_TYPE_MAP.get(tool_name, ToolType.GENERIC)
        budget     = BUDGET_BY_TOOL.get(tool_name, BUDGET_BY_TYPE[tool_type])
        raw_tokens = _count_tokens(raw_output)

        if raw_tokens <= budget:
            return raw_output

        # LIST tools: item-level truncation with semantic annotation
        if tool_type is ToolType.LIST:
            return self._apply_list_budget(tool_name, raw_output, budget)

        result         = self._apply_budget(tool_type, raw_output, budget)
        result_tokens  = _count_tokens(result)

        logger.debug(
            "[summarizer] tool=%s type=%s raw_tokens=%d truncated_tokens=%d dropped_tokens=%d",
            tool_name,
            tool_type.value,
            raw_tokens,
            result_tokens,
            raw_tokens - result_tokens,
        )

        if result_tokens < raw_tokens:
            result += (
                f"\n[Output truncated: {result_tokens} of {raw_tokens} tokens shown"
                " — narrow your query to see more]"
            )
            # Prometheus
            tool_output_truncated_total.labels(tool_type=tool_type.value).inc()
            # OTel — no-op when no SDK is configured; captured when OTel is wired up
            span = trace.get_current_span()
            span.set_attribute("tool_output.truncated", True)
            span.set_attribute("tool_output.raw_tokens", raw_tokens)
            span.set_attribute("tool_output.shown_tokens", result_tokens)
            # Structured log — captured by Loki and surfaced in Langfuse trace via log correlation
            logger.info(
                "[summarizer] truncation_event tool=%s type=%s raw_tokens=%d shown_tokens=%d",
                tool_name,
                tool_type.value,
                raw_tokens,
                result_tokens,
            )

        return result

    def _apply_list_budget(self, tool_name: str, raw_output: str, budget: int) -> str:
        """
        Item-level truncation for LIST tools.

        Strategy:
        1. Parse JSON. If not JSON or if `data` is not a list, fall back to GENERIC.
        2. Read `total_count` from the top-level JSON (set by the tool).
        3. Drop whole items from `data` until the serialised output fits within budget.
        4. Emit a semantic annotation: "[Showing N of M pods — filter by ...]"
        5. Increment list_output_truncated_total Prometheus counter and OTel span attributes.
        """
        try:
            data = json.loads(raw_output)
        except json.JSONDecodeError:
            # Non-JSON: fall back to generic head-keep truncation
            return self._apply_budget(ToolType.GENERIC, raw_output, BUDGET_GENERIC)

        items = data.get("data")
        if not isinstance(items, list):
            # data field is not a list (e.g., dict-structured response) — fall back to GENERIC
            return self._apply_budget(ToolType.GENERIC, raw_output, BUDGET_GENERIC)

        total_count = data.get("total_count", len(items))
        kept = list(items)

        # Greedy: drop items from the tail until the serialised output fits
        while kept and _count_tokens(json.dumps({**data, "data": kept}, indent=2)) > budget:
            kept.pop()

        result_data = {**data, "data": kept}
        result = json.dumps(result_data, indent=2)
        shown = len(kept)

        if shown < total_count:
            resource_type = RESOURCE_TYPE_BY_TOOL.get(tool_name, "item")
            result += (
                f"\n[Showing {shown} of {total_count} {resource_type}s"
                " — filter by namespace, label selector, or status to see all.]"
            )
            # Prometheus
            list_output_truncated_total.labels(resource_type=resource_type).inc()
            # OTel
            span = trace.get_current_span()
            span.set_attribute("tool_output.truncated", True)
            span.set_attribute("list_items_shown", shown)
            span.set_attribute("list_items_total", total_count)
            # Structured log
            logger.info(
                "[summarizer] list_truncation_event tool=%s resource_type=%s total=%d shown=%d",
                tool_name,
                resource_type,
                total_count,
                shown,
            )

        return result

    # ── Internal helpers ────────────────────────────────────────────────────

    def _apply_budget(self, tool_type: ToolType, raw_output: str, budget: int) -> str:
        """Parse JSON, truncate only FREE_TEXT_FIELDS, re-serialise."""
        try:
            data = json.loads(raw_output)
        except json.JSONDecodeError:
            return self._truncate_text_blob(
                raw_output, budget, keep_tail=(tool_type is ToolType.LOG)
            )

        keep_tail       = (tool_type is ToolType.LOG)
        skeleton_tokens = _count_tokens(self._serialise_with_placeholder(data))
        content_budget  = budget - skeleton_tokens

        self._truncate_fields_inplace(data, content_budget, keep_tail)
        return json.dumps(data, indent=2)

    def _truncate_fields_inplace(
        self, node: object, budget: int, keep_tail: bool
    ) -> None:
        """Recursively walk dict/list and truncate FREE_TEXT_FIELDS string values."""
        if isinstance(node, dict):
            for key, value in node.items():
                if key in FREE_TEXT_FIELDS and isinstance(value, str):
                    node[key] = self._truncate_log_field(value, budget, keep_tail)
                else:
                    self._truncate_fields_inplace(value, budget, keep_tail)
        elif isinstance(node, list):
            for item in node:
                self._truncate_fields_inplace(item, budget, keep_tail)

    def _truncate_log_field(self, text: str, budget: int, keep_tail: bool) -> str:
        """
        Truncate a multi-line text field to fit within *budget* tokens.

        keep_tail=True  (LOG tools):   anchor extraction + keep tail, drop head
        keep_tail=False (other tools): keep head, drop tail
        """
        if keep_tail:
            return self._truncate_log_with_anchor(text, budget)
        return self._truncate_head_keep(text, budget)

    def _truncate_log_with_anchor(self, text: str, budget: int) -> str:
        """
        LOG truncation strategy:
        1. Find first exception anchor line.
        2. Preserve anchor + ANCHOR_CONTEXT_LINES (traceback body) — this always fits.
        3. Fill remaining budget from the tail (most recent lines).
        4. Emit explicit omission markers for every dropped segment.
        """
        lines = text.splitlines()
        n     = len(lines)

        anchor_idx = _find_first_anchor(lines)

        # ── No anchor: pure tail ────────────────────────────────────────────
        if anchor_idx is None:
            tail_lines = _build_tail_slice(lines, budget)
            dropped    = n - len(tail_lines)
            if dropped:
                return f"[... {dropped} lines from startup omitted ...]\n" + "\n".join(tail_lines)
            return "\n".join(tail_lines)

        # ── Anchor found ────────────────────────────────────────────────────
        anchor_end   = min(anchor_idx + 1 + ANCHOR_CONTEXT_LINES, n)
        anchor_block = lines[anchor_idx:anchor_end]
        anchor_tokens = sum(_count_tokens(ln + "\n") for ln in anchor_block)

        tail_budget = budget - anchor_tokens

        if tail_budget <= 0:
            # Anchor block alone fills the budget — trim it to fit
            anchor_block = _trim_lines_to_budget(anchor_block, budget)
            dropped_head = anchor_idx
            result_parts = []
            if dropped_head:
                result_parts.append(f"[... {dropped_head} lines from startup omitted ...]")
            result_parts.extend(anchor_block)
            return "\n".join(result_parts)

        # Build tail from lines after the anchor block
        tail_source = lines[anchor_end:]
        tail_lines  = _build_tail_slice(tail_source, tail_budget)

        dropped_head    = anchor_idx
        dropped_between = len(tail_source) - len(tail_lines)

        parts: list[str] = []
        if dropped_head:
            parts.append(f"[... {dropped_head} lines from startup omitted ...]")
        parts.extend(anchor_block)
        if dropped_between:
            parts.append(f"[... {dropped_between} lines omitted ...]")
        parts.extend(tail_lines)

        return "\n".join(parts)

    def _truncate_head_keep(self, text: str, budget: int) -> str:
        """Keep the head of the text, drop the tail."""
        lines = text.splitlines()
        kept  = []
        used  = 0
        for line in lines:
            lt = _count_tokens(line + "\n")
            if used + lt > budget:
                break
            kept.append(line)
            used += lt
        dropped = len(lines) - len(kept)
        if not dropped:
            return text
        return "\n".join(kept) + f"\n[... {dropped} lines truncated ...]"

    def _truncate_text_blob(self, text: str, budget: int, keep_tail: bool) -> str:
        """Fallback for non-JSON output — treat entire output as free-text."""
        if keep_tail:
            return self._truncate_log_with_anchor(text, budget)
        return self._truncate_head_keep(text, budget)

    @staticmethod
    def _serialise_with_placeholder(data: object) -> str:
        """
        Return a JSON serialisation with FREE_TEXT_FIELDS replaced by empty
        strings. Used to measure the token cost of the structural envelope
        before budgeting the free-text content.
        """
        d = copy.deepcopy(data)
        _blank_free_text(d)
        return json.dumps(d, indent=2)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _find_first_anchor(lines: list[str]) -> Optional[int]:
    """Return the index of the first line matching any anchor pattern, or None."""
    for i, line in enumerate(lines):
        for pat in _COMPILED_ANCHORS:
            if pat.search(line):
                return i
    return None


def _build_tail_slice(lines: list[str], budget: int) -> list[str]:
    """Return the longest suffix of *lines* that fits within *budget* tokens."""
    kept = []
    used = 0
    for line in reversed(lines):
        lt = _count_tokens(line + "\n")
        if used + lt > budget:
            break
        kept.insert(0, line)
        used += lt
    return kept


def _trim_lines_to_budget(lines: list[str], budget: int) -> list[str]:
    """Return the longest prefix of *lines* that fits within *budget* tokens."""
    kept = []
    used = 0
    for line in lines:
        lt = _count_tokens(line + "\n")
        if used + lt > budget:
            break
        kept.append(line)
        used += lt
    return kept


def _blank_free_text(node: object) -> None:
    """Recursively replace FREE_TEXT_FIELDS string values with empty string in-place."""
    if isinstance(node, dict):
        for k in node:
            if k in FREE_TEXT_FIELDS:
                node[k] = ""
            else:
                _blank_free_text(node[k])
    elif isinstance(node, list):
        for item in node:
            _blank_free_text(item)
