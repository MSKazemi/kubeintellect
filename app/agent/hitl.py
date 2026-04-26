"""
HITL approval/denial detection — pure string matching, no infrastructure.
Imported by workflow.py and testable without any LangGraph/Postgres setup.
"""

_APPROVAL_PHRASES = {
    "yes", "approve", "approved", "do it", "yes do it",
    "go ahead", "confirm", "ok", "okay", "sure", "proceed", "run it",
}
_DENIAL_PHRASES = {
    "no", "deny", "denied", "cancel", "abort",
    "stop", "nope", "don't", "dont",
}
_AUTO_APPROVE_PHRASES = {
    "approve all", "auto approve", "auto-approve", "yes to all",
    "approve everything", "skip approval", "bypass hitl", "/auto-approve",
}


def is_approval(message: str) -> bool:
    return message.strip().lower() in _APPROVAL_PHRASES


def is_denial(message: str) -> bool:
    return message.strip().lower() in _DENIAL_PHRASES


def is_auto_approve_request(message: str) -> bool:
    """Return True if the user wants to enable session-wide HITL bypass."""
    return message.strip().lower() in _AUTO_APPROVE_PHRASES
