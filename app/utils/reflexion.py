"""
Verbal Reflection utility for KubeIntellect.

Generates structured self-reflections when a HITL action is rejected or
a tool fails. Based on the Reflexion framework (arXiv 2303.11366).
"""

from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")

_REFLECTION_SYSTEM = (
    "You are a Kubernetes AI agent. Generate a brief self-reflection in this exact format:\n"
    "'I attempted to [action]. The user rejected because [reason]. "
    "Next time I should [correction].'\n"
    "Keep it concise (2-3 sentences) and actionable. "
    "Focus on what to do differently, not on excuses."
)

_REFLECTION_USER_TEMPLATE = (
    "Failed action: {failed_action}\n"
    "Rejection reason: {rejection_reason}\n\n"
    "Generate the self-reflection."
)

_DEFAULT_REFLECTION = (
    "I attempted an action that was rejected by the user. "
    "The user indicated it was not what they wanted. "
    "Next time I should ask for more specific confirmation before proceeding with this type of operation."
)


def generate_reflection(
    failed_action: str,
    rejection_reason: str,
    llm,
) -> str:
    """
    Call the LLM to generate a structured self-reflection on a rejected action.

    Args:
        failed_action: Short description of what was attempted.
        rejection_reason: The reason the user gave for rejecting (may be empty).
        llm: A LangChain LLM instance.

    Returns:
        A 2-3 sentence reflection string. Returns a safe default if the LLM
        call fails for any reason.
    """
    if not rejection_reason:
        rejection_reason = "no explicit reason given"

    try:
        from langchain_core.messages import SystemMessage, HumanMessage

        user_content = _REFLECTION_USER_TEMPLATE.format(
            failed_action=failed_action[:500],
            rejection_reason=rejection_reason[:300],
        )

        response = llm.invoke([
            SystemMessage(content=_REFLECTION_SYSTEM),
            HumanMessage(content=user_content),
        ])

        reflection = (
            response.content
            if hasattr(response, "content")
            else str(response)
        ).strip()

        if reflection:
            logger.info(f"Generated reflection: {reflection[:120]}")
            return reflection

    except Exception as e:
        logger.warning(f"Reflection generation failed (non-fatal): {e}")

    return _DEFAULT_REFLECTION
