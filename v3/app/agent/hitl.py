"""
HITL approval/denial detection — pure string matching, no infrastructure.
Imported by runner.py and testable without any LangGraph/Postgres setup.

Matching is two-tier so short *multi-word* replies are handled, not just exact
single tokens:
  1. the whole (stripped, lower-cased) message equals a known phrase, or
  2. the message's *leading word* is a decisive token — so "yes, fix it" reads
     as approval and "no, don't do that" reads as denial.

Denial is deliberately checked before approval in the resume path (runner.py),
and leading-token denial keeps a multi-word "no ..." from silently falling
through to an approval (fail-safe toward *not* mutating).
"""

import re

_APPROVAL_PHRASES = {
    "yes",
    "approve",
    "approved",
    "do it",
    "yes do it",
    "go ahead",
    "confirm",
    "ok",
    "okay",
    "sure",
    "proceed",
    "run it",
}
_DENIAL_PHRASES = {
    "no",
    "deny",
    "denied",
    "cancel",
    "abort",
    "stop",
    "nope",
    "don't",
    "dont",
}
_AUTO_APPROVE_PHRASES = {
    "approve all",
    "auto approve",
    "auto-approve",
    "yes to all",
    "approve everything",
    "skip approval",
    "bypass hitl",
    "/auto-approve",
}

# Leading words that decide a short reply even when followed by more text
# ("yes fix it", "no don't do that"). Kept narrow to avoid misreading a
# genuine query (e.g. "get all pods") as an approval/denial.
_APPROVAL_LEAD_TOKENS = {
    "yes",
    "yeah",
    "yep",
    "approve",
    "approved",
    "confirm",
    "ok",
    "okay",
    "sure",
    "proceed",
}
_DENIAL_LEAD_TOKENS = {
    "no",
    "nope",
    "deny",
    "denied",
    "cancel",
    "abort",
    "stop",
    "don't",
    "dont",
    "never",
}


def _lead_token(message: str) -> str:
    """Return the leading word (letters/apostrophe) of *message*, lower-cased."""
    match = re.match(r"[a-z']+", message.strip().lower())
    return match.group(0) if match else ""


def is_approval(message: str) -> bool:
    text = message.strip().lower()
    return text in _APPROVAL_PHRASES or _lead_token(message) in _APPROVAL_LEAD_TOKENS


def is_denial(message: str) -> bool:
    text = message.strip().lower()
    return text in _DENIAL_PHRASES or _lead_token(message) in _DENIAL_LEAD_TOKENS


def is_auto_approve_request(message: str) -> bool:
    """Return True if the user wants to enable session-wide HITL bypass."""
    return message.strip().lower() in _AUTO_APPROVE_PHRASES
