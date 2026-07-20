"""lookup_playbook — on-demand failure-pattern playbook fetch.

Replaces v2's prompt injection of all matched playbooks. The agent calls
this when it suspects a known failure pattern (CrashLoopBackOff, OOMKilled,
ImagePullBackOff, ...) and gets back the canonical investigation steps,
expected evidence, and a fix template.

Match strategy (cheap, in order):
  1. Exact playbook-name match (case-insensitive).
  2. Substring match against playbook names.
  3. Trigger regex match — feed the symptom string as both the
     pod-status and event-message corpus to `match_playbooks`.
"""

from __future__ import annotations

from langchain_core.tools import tool

from app.agent.playbooks import get_playbook, list_playbooks, match_playbooks
from app.agent.playbooks.loader import Playbook
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _render(pb: Playbook) -> str:
    """Render a Playbook as markdown for the agent."""
    lines = [f"## Playbook: {pb.name}"]
    if pb.investigation_steps:
        lines.append("\n**Investigation steps**:")
        for step in pb.investigation_steps:
            lines.append(f"- {step}")
    if pb.expected_evidence:
        lines.append("\n**Expected evidence**:")
        for ev in pb.expected_evidence:
            lines.append(f"- {ev}")
    if pb.recommended_fix_template:
        lines.append(f"\n**Recommended fix**:\n{pb.recommended_fix_template}")
    return "\n".join(lines)


def _find_matches(symptom: str) -> list[Playbook]:
    """Resolve a free-text symptom to one or more playbooks."""
    needle = symptom.strip().lower()
    if not needle:
        return []

    # 1. Exact name
    for pb in list_playbooks():
        if pb.name.lower() == needle:
            return [pb]

    # 2. Substring on names
    name_hits = [pb for pb in list_playbooks() if needle in pb.name.lower()]
    if name_hits:
        return name_hits

    # 3. Trigger regex match — symptom feeds both corpora
    matched_names = match_playbooks(symptom, symptom)
    return [pb for name in matched_names if (pb := get_playbook(name)) is not None]


@tool
def lookup_playbook(symptom: str) -> str:
    """Look up the canonical playbook(s) for a Kubernetes failure pattern.

    Args:
        symptom: A free-text failure description. Examples:
            - "CrashLoopBackOff"
            - "OOMKilled exit 137"
            - "Pod stuck Pending — FailedScheduling Insufficient cpu"
            - "ImagePullBackOff ErrImagePull"

    Returns:
        Markdown with matched playbook name(s), investigation steps,
        expected evidence, and recommended fix template. Returns a short
        notice listing available playbooks if nothing matches.
    """
    matches = _find_matches(symptom)
    if not matches:
        names = ", ".join(sorted(pb.name for pb in list_playbooks()))
        return f"No playbook matched symptom={symptom!r}.\nAvailable playbooks: {names}"

    if len(matches) == 1:
        return _render(matches[0])
    return "\n\n---\n\n".join(_render(pb) for pb in matches)
