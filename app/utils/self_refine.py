"""
Self-Refine loop and Safeguard heuristics for AI-generated Kubernetes tool code.

Self-Refine (arXiv 2303.17651): feedback → refine iterations before HITL.
Safeguard: purely heuristic checks for dangerous patterns (no LLM call).
"""

import json
import re
from typing import Tuple, List

from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


# ---------------------------------------------------------------------------
# Self-Refine loop
# ---------------------------------------------------------------------------

_FEEDBACK_SYSTEM = (
    "You are a senior Kubernetes platform engineer reviewing AI-generated Python tool code. "
    "Your job is to find issues before this code is shown to a human for approval. "
    "Be concise and specific."
)

_FEEDBACK_USER_TEMPLATE = """Review the following AI-generated Kubernetes tool code.

Tool description: {description}

Code:
```python
{code}
```

Evaluate for:
1. Safety issues — unbounded deletes (no namespace filter), missing timeout_seconds, operations that could affect the entire cluster.
2. Correctness issues — wrong kubernetes.client API method, missing required parameter (e.g. namespace not passed when required), incorrect API group (e.g. using CoreV1Api for Deployments).
3. Kubernetes best-practice violations — missing error handling, not returning a dict, hardcoded resource names.

Respond with a JSON object ONLY (no markdown, no explanation outside JSON):
{{"score": <integer 0-10>, "issues": [<string>, ...], "has_issues": <true|false>}}

"score" is the code quality (10 = perfect, 0 = unusable).
"has_issues" is true when score < 8 or issues list is non-empty.
"""

_REFINE_SYSTEM = (
    "You are a senior Kubernetes platform engineer. "
    "Fix all issues listed below in the Python function code. "
    "Return ONLY the fixed Python function code, no imports, no markdown fences, "
    "no explanation. The code must start with 'def '."
)

_REFINE_USER_TEMPLATE = """Issues found:
{issues}

Original code:
{code}

Return ONLY the corrected Python function code."""


def self_refine_code(
    code_str: str,
    tool_description: str,
    llm,
    max_iterations: int = 2,
) -> Tuple[str, List[str]]:
    """
    Run a self-refine loop on AI-generated code.

    Args:
        code_str: The raw generated function code.
        tool_description: Natural-language description of what the tool does.
        llm: An instantiated LangChain LLM (from get_code_gen_llm()).
        max_iterations: Maximum refine cycles (default 2).

    Returns:
        (refined_code, feedback_notes) where feedback_notes is a list of
        human-readable strings describing what was changed.
    """
    from app.utils.ast_validator import validate_k8s_api_calls, format_ast_error_message

    current_code = code_str
    feedback_notes: List[str] = []

    for iteration in range(1, max_iterations + 1):
        try:
            # --- AST validation first (fast, no LLM) ---
            unknown_calls = validate_k8s_api_calls(current_code)
            ast_issues: List[str] = []
            if unknown_calls:
                ast_msg = format_ast_error_message(unknown_calls)
                ast_issues.append(ast_msg)
                logger.warning(f"Self-refine iteration {iteration}: AST issues found: {unknown_calls}")

            # --- LLM feedback ---
            feedback_prompt = _FEEDBACK_USER_TEMPLATE.format(
                description=tool_description,
                code=current_code,
            )
            from langchain_core.messages import SystemMessage, HumanMessage
            feedback_response = llm.invoke([
                SystemMessage(content=_FEEDBACK_SYSTEM),
                HumanMessage(content=feedback_prompt),
            ])
            feedback_text = (
                feedback_response.content
                if hasattr(feedback_response, "content")
                else str(feedback_response)
            ).strip()

            # Parse JSON response
            score = 10
            llm_issues: List[str] = []
            has_issues = False
            try:
                # Strip markdown fences if present
                json_text = re.sub(r"```(?:json)?\s*|\s*```", "", feedback_text).strip()
                parsed = json.loads(json_text)
                score = int(parsed.get("score", 10))
                llm_issues = [str(i) for i in parsed.get("issues", [])]
                has_issues = bool(parsed.get("has_issues", False))
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Self-refine: could not parse feedback JSON (iteration {iteration}): {e}. Raw: {feedback_text[:200]}")
                # Treat unparseable response as no issues to avoid unnecessary refinement
                has_issues = False

            all_issues = ast_issues + llm_issues

            if not all_issues and (not has_issues or score >= 8):
                logger.info(f"Self-refine: code quality score={score}, no issues. Stopping after iteration {iteration}.")
                break

            # --- LLM refine ---
            issues_text = "\n".join(f"- {issue}" for issue in all_issues)
            refine_prompt = _REFINE_USER_TEMPLATE.format(
                issues=issues_text,
                code=current_code,
            )
            refine_response = llm.invoke([
                SystemMessage(content=_REFINE_SYSTEM),
                HumanMessage(content=refine_prompt),
            ])
            refined = (
                refine_response.content
                if hasattr(refine_response, "content")
                else str(refine_response)
            ).strip()

            # Strip accidental markdown fences
            refined = re.sub(r"^```python\s*", "", refined)
            refined = re.sub(r"\s*```$", "", refined).strip()

            if refined and refined != current_code:
                logger.info(f"Self-refine iteration {iteration}: code updated (score was {score}).")
                feedback_notes.append(
                    f"Iteration {iteration}: refined code (score={score}). "
                    f"Issues: {'; '.join(all_issues[:3])}"
                )
                current_code = refined
            else:
                logger.info(f"Self-refine iteration {iteration}: no change in code after refinement.")
                feedback_notes.append(f"Iteration {iteration}: no change produced.")
                break

        except Exception as e:
            logger.warning(f"Self-refine iteration {iteration} failed (non-fatal): {e}", exc_info=True)
            feedback_notes.append(f"Iteration {iteration}: skipped due to error: {e}")
            break

    return current_code, feedback_notes


# ---------------------------------------------------------------------------
# Safeguard Review (heuristic, no LLM)
# ---------------------------------------------------------------------------

# Patterns that indicate dangerous unbounded delete operations
_UNBOUNDED_DELETE_PATTERNS = [
    # delete_* function called with no namespace (or namespace=None as default)
    re.compile(r"def\s+\w*delete\w*\s*\([^)]*\)", re.IGNORECASE),
]

_CLUSTER_WIDE_DESTRUCTIVE = [
    "delete_namespace",
    "drain_node",
    "delete_cluster",
    "delete_all",
]

_REMOVE_RESOURCE_LIMITS_PATTERNS = [
    re.compile(r"resources\s*=\s*None"),
    re.compile(r"limits\s*=\s*None"),
    re.compile(r"requests\s*=\s*None"),
]

_PRIVILEGE_ESCALATION_KEYWORDS = [
    "cluster_admin",
    "clusteradmin",
    "cluster-admin",
    "*",  # wildcard verbs/resources in RBAC
    "escalate",
    "impersonate",
]


def safeguard_review(
    code_str: str,
    tool_description: str,
) -> Tuple[bool, List[str]]:
    """
    Heuristic safeguard check for dangerous patterns in generated code.

    Purely rule-based — no LLM call — so it is fast and deterministic.

    Args:
        code_str: Generated Python function code.
        tool_description: Natural-language description of what the tool does.

    Returns:
        (is_flagged, risk_annotations) where risk_annotations are strings that
        should be prepended to the HITL presentation when is_flagged=True.
    """
    annotations: List[str] = []
    code_lower = code_str.lower()
    desc_lower = tool_description.lower()

    # 1. Unbounded delete patterns
    for pattern in _UNBOUNDED_DELETE_PATTERNS:
        match = pattern.search(code_str)
        if match:
            func_sig = match.group(0)
            # Check if namespace is absent or has None default in the signature
            if "namespace" not in func_sig.lower():
                annotations.append(
                    "UNBOUNDED DELETE: delete function has no namespace parameter — "
                    "this could affect resources cluster-wide."
                )
            elif re.search(r"namespace\s*(?::\s*\w+)?\s*=\s*None", func_sig, re.IGNORECASE):
                annotations.append(
                    "UNBOUNDED DELETE: delete function has namespace=None default — "
                    "confirm this is intentional and safe."
                )

    # 2. Cluster-wide destructive operations
    for op in _CLUSTER_WIDE_DESTRUCTIVE:
        if op.replace("_", "") in code_lower.replace("_", ""):
            annotations.append(
                f"CLUSTER-WIDE DESTRUCTIVE OP: code contains `{op}` which is a "
                f"high-blast-radius operation. Verify scope before approving."
            )

    # 3. Removal of resource limits
    for pattern in _REMOVE_RESOURCE_LIMITS_PATTERNS:
        if pattern.search(code_str):
            annotations.append(
                "RESOURCE LIMITS REMOVAL: code sets resources/limits/requests to None — "
                "this removes resource constraints and could allow unbounded consumption."
            )
            break  # one annotation per category is enough

    # 4. Privilege escalation in description
    for kw in _PRIVILEGE_ESCALATION_KEYWORDS:
        if kw in desc_lower or kw in code_lower:
            # Avoid false positive for common substring "*" in imports
            if kw == "*" and "import *" in code_lower:
                continue
            annotations.append(
                f"PRIVILEGE ESCALATION: description or code contains `{kw}` — "
                f"review RBAC changes carefully to ensure least-privilege is maintained."
            )
            break  # one annotation per category

    is_flagged = len(annotations) > 0
    if is_flagged:
        logger.warning(
            f"Safeguard flagged code for tool '{tool_description[:60]}': "
            f"{len(annotations)} annotation(s)"
        )
    return is_flagged, annotations
