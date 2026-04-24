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


def is_approval(message: str) -> bool:
    return message.strip().lower() in _APPROVAL_PHRASES


def is_denial(message: str) -> bool:
    return message.strip().lower() in _DENIAL_PHRASES
