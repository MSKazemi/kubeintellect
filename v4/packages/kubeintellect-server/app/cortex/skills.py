"""Runbooks-as-skills v0 (v5 P2).

The 18 playbooks recast as on-demand ``SKILL.md``-style blocks. Instead of dumping every runbook
into the gather prompt, only the playbooks whose triggers actually fired for this snapshot are
rendered — a Claude-Code-skills-style "load on demand" that keeps the lead-context budget small
(a P2 responsiveness lever) while still giving the investigator the exact diagnostic sequence.

Pure functions over the existing playbook loader — no new source of truth, fully unit-testable.
"""

from __future__ import annotations

from app.agent.playbooks.loader import Playbook, get_playbook


def render_skill(pb: Playbook) -> str:
    """Render one playbook as a SKILL.md-style block: name, when-to-use, steps, evidence, fix."""
    lines = [f"### SKILL: {pb.name}"]
    if pb.investigation_steps:
        lines.append("**Diagnostic steps:**")
        lines.extend(f"{i + 1}. {s}" for i, s in enumerate(pb.investigation_steps))
    if pb.expected_evidence:
        lines.append("**Expected evidence:**")
        lines.extend(f"- {e}" for e in pb.expected_evidence)
    if pb.recommended_fix_template.strip():
        lines.append("**Recommended fix:**")
        lines.append(pb.recommended_fix_template.strip())
    return "\n".join(lines)


def render_matched_skills(names: list[str], *, max_skills: int = 5) -> str:
    """Render (up to ``max_skills``) matched playbooks as a single on-demand skills block.

    Unknown names are skipped. Returns "" when nothing matched — the caller injects nothing, so
    the prompt is unchanged. ``max_skills`` bounds the prompt cost even if many triggers fire.
    """
    blocks = []
    for name in names[:max_skills]:
        pb = get_playbook(name)
        if pb is not None:
            blocks.append(render_skill(pb))
    if not blocks:
        return ""
    header = ("## Runbook skills (loaded on demand — triggers fired for this snapshot)\n"
              "Follow the matching skill's diagnostic sequence before improvising.")
    return header + "\n\n" + "\n\n".join(blocks)
